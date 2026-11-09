#!/usr/bin/env python3
"""Resume the existing sandbox auditor and publish a dated Markdown review."""

import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
from itertools import count
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
import uuid


ROOT = Path(__file__).resolve().parents[1]
# sbx 識別資訊（sandbox 名稱／ID、Codex session、工作目錄）只放在未追蹤的
# sbx.local.json，不寫進版本控制。檔案缺漏時以占位值讓測試仍可執行，
# 真實執行前由 require_local_config() 擋下。
LOCAL_CONFIG = ROOT / "sbx.local.json"
LOCAL_KEYS = ("sandbox", "sandbox_id", "session", "workspace")
_local = (
    json.loads(LOCAL_CONFIG.read_text(encoding="utf-8"))
    if LOCAL_CONFIG.is_file()
    else {}
)
SANDBOX = _local.get("sandbox", "example-sandbox")
SANDBOX_ID = _local.get("sandbox_id", "example-sandbox-id")
SESSION = _local.get("session", "example-session")
WORKSPACE = _local.get("workspace", "/example/workspace")
REFERENCES = (
    "claude-code-game-studios-review.md",
    "gamedev-agent-skills-review.md",
    "threejs-skills-security-audit.md",
)
START = "<!-- skill-auditor:reviews:start -->"
END = "<!-- skill-auditor:reviews:end -->"

PROBE = r"""
import json, pathlib
session = __import__('sys').argv[1]
paths = list((pathlib.Path.home()/'.codex/sessions').rglob('*'+session+'.jsonl'))
if len(paths) != 1:
    raise SystemExit('Required existing Codex session not found or ambiguous')
with paths[0].open() as f:
    item = json.loads(f.readline())
meta = item.get('payload', {})
if item.get('type') != 'session_meta' or meta.get('id') != session:
    raise SystemExit('Session metadata mismatch')
print(json.dumps({'session': meta['id'], 'cwd': meta['cwd']}))
"""

PREPARE = r"""
import json, os, pathlib, subprocess, sys
run, url = sys.argv[1:]
root = pathlib.Path(run)
root.mkdir(parents=True, exist_ok=False)
(root/'references').mkdir()
env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1',
           GIT_CONFIG_GLOBAL='/dev/null', GIT_LFS_SKIP_SMUDGE='1')
git = ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'protocol.file.allow=never',
       '-c', 'protocol.ext.allow=never']
def call(args):
    return subprocess.check_output(git+args, env=env, text=True).strip()
repo = root/'repository'
call(['clone', '--quiet', '--depth', '1', '--no-checkout', '--no-recurse-submodules',
      '--', url, str(repo)])
sha = call(['-C', str(repo), 'rev-parse', 'HEAD'])
call(['-C', str(repo), 'checkout', '--quiet', '--detach', sha])
print(json.dumps({'commit': sha}))
"""

READ_REPORT = r"""
import pathlib, stat, sys
p = pathlib.Path(sys.argv[1])
s = p.lstat()
if not stat.S_ISREG(s.st_mode) or not 0 < s.st_size <= 2_000_000:
    raise SystemExit('Report must be a nonempty regular file <= 2 MB')
sys.stdout.write(p.read_text(encoding='utf-8'))
"""


def repository(value):
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        value = "https://github.com/" + value
    parsed = urlsplit(value)
    parts = parsed.path.strip("/").split("/")
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not re.fullmatch(r"[A-Za-z0-9.-]+", parsed.netloc)
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or len(parts) < 2
        or any(
            not re.fullmatch(r"[A-Za-z0-9_.-]+", p) or p in (".", "..") for p in parts
        )
        or parsed.path != "/" + "/".join(parts)
    ):
        raise ValueError(
            "請提供 OWNER/REPO 或不含憑證、query、fragment 的 HTTPS repo URL"
        )
    if parts[-1].endswith(".git"):
        parts[-1] = parts[-1][:-4]
    if not parts[-1] or parts[-1] in (".", ".."):
        raise ValueError("無效的 repo 名稱")
    return f"https://{parsed.netloc}/{'/'.join(parts)}"


def sbx(*args, input=None, output_path=None):
    result = subprocess.run(
        ["sbx", "--no-input", *args],
        input=input,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if output_path is not None:
        output_path.write_text(result.stdout, encoding="utf-8")
        output_path.with_suffix(".stderr").write_text(result.stderr, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(
            f"sbx {args[0]} 失敗 ({result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def check_sandbox():
    sandboxes = json.loads(sbx("ls", "--json"))
    match = [s for s in sandboxes if s.get("name") == SANDBOX]
    if (
        len(match) != 1
        or match[0].get("id") != SANDBOX_ID
        or match[0].get("agent") != "codex"
    ):
        raise RuntimeError("指定的既有 sandbox 不存在或身分不符；不會建立替代 sandbox")
    meta = json.loads(sbx("exec", SANDBOX, "python3", "-c", PROBE, SESSION))
    if meta != {"session": SESSION, "cwd": WORKSPACE}:
        raise RuntimeError("既有 Codex session 身分或工作目錄不符")
    return meta


@contextmanager
def audit_lock(root):
    state = root / ".audit-runs"
    state.mkdir(exist_ok=True)
    with (state / "audit.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "此目錄已有審查執行中，請勿同時使用同一個 Codex session"
            ) from exc
        yield state


def validate_events(events):
    items = [json.loads(line) for line in events.splitlines() if line.strip()]
    threads = [i.get("thread_id") for i in items if i.get("type") == "thread.started"]
    if threads != [SESSION]:
        raise RuntimeError("Codex 沒有續用指定 session；報告不予發布")
    if any(i.get("type") in ("error", "turn.failed") for i in items):
        raise RuntimeError("Codex 回報錯誤；報告不予發布，請查看 events.jsonl")
    if not any(i.get("type") == "turn.completed" for i in items):
        raise RuntimeError("Codex 未完成審查；報告不予發布")


def validate_report(body, url, day, sha):
    required = (
        f"- 儲存庫：{url}",
        f"- 審查日期：{day}",
        f"- 審查版本：`{sha}`",
        "## 結論",
        "## 範圍、方法與限制",
        "## 技能與執行面盤點",
        "## 發現與證據",
        "## 實測結果",
        "## 採用建議",
    )
    if not body.startswith("# ") or any(s not in body.splitlines() for s in required):
        raise RuntimeError(
            "報告缺少必要欄位或日期／版本不符；保留執行紀錄，不更新 README"
        )


def index_text(original, rows):
    if original.count(START) != original.count(END) or original.count(START) > 1:
        raise ValueError("README 的自動索引標記損壞")
    block = f"{START}\n\n| 日期 | 專案 | 版本 | 報告 |\n| --- | --- | --- | --- |\n"
    block += "\n".join(rows) + f"\n\n{END}"
    if START in original:
        start, end = original.index(START), original.index(END)
        if end < start:
            raise ValueError("README 的自動索引標記順序錯誤")
        return original[:start] + block + original[end + len(END) :]
    return original.rstrip() + "\n\n" + block + "\n"


def update_indexes(root, filename, url, day, sha):
    for path, prefix in (
        (root / "README.md", "reviews/"),
        (root / "reviews/README.md", ""),
    ):
        original = (
            path.read_text(encoding="utf-8") if path.exists() else "# Skill Auditor\n"
        )
        row = f"| {day} | {url.removeprefix('https://')} | `{sha[:12]}` | [審查報告]({prefix}{filename}) |"
        old_rows = []
        if START in original and END in original:
            old_rows = [
                line
                for line in original.split(START, 1)[1].split(END, 1)[0].splitlines()
                if line.startswith("| ") and "](" in line
            ]
        content = index_text(original, [row, *old_rows])
        # 以同一目錄的暫存檔替換，避免中斷時留下半份索引。
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as f:
            f.write(content)
        Path(f.name).replace(path)


def audit(value, root=ROOT):
    url = repository(value)
    with audit_lock(root) as state:
        check_sandbox()
        run_id = uuid.uuid4().hex
        local = state / run_id
        local.mkdir()
        remote = f"{WORKSPACE}/.skill-auditor/{run_id}"
        day = datetime.now().astimezone().date().isoformat()
        for name in REFERENCES:
            if not (root / "reviews" / name).is_file():
                raise RuntimeError(f"缺少參考報告：{name}")
        # 先檢查索引格式，再進行有成本的模型審查。
        for path in (root / "README.md", root / "reviews/README.md"):
            index_text(path.read_text(encoding="utf-8") if path.exists() else "", [])
        print(f"使用 {SANDBOX} / session {SESSION}\n下載 {url}", flush=True)
        prepared = json.loads(
            sbx("exec", "-i", SANDBOX, "python3", "-", remote, url, input=PREPARE)
        )
        sha = prepared["commit"]
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
            raise RuntimeError("無效的 repository commit SHA")
        for name in REFERENCES:
            sbx(
                "cp",
                str(root / "reviews" / name),
                f"{SANDBOX}:{remote}/references/{name}",
            )
        template = (root / "prompts/security-review.md").read_text(encoding="utf-8")
        context = json.dumps(
            {
                "repository_url": url,
                "audit_date": day,
                "commit": sha,
                "sandbox": SANDBOX,
                "session": SESSION,
                "repository_path": remote + "/repository",
                "references_path": remote + "/references",
            },
            ensure_ascii=False,
        )
        prompt = (
            template
            + "\n\n本次審查參數（可信的流程輸入）：\n```json\n"
            + context
            + "\n```\n"
        )
        (local / "prompt.md").write_text(prompt, encoding="utf-8")
        print(f"審查版本 {sha}；由既有 Codex agent 執行 /security-review…", flush=True)
        events = sbx(
            "exec",
            "-i",
            "-w",
            WORKSPACE,
            SANDBOX,
            "codex",
            "exec",
            "--skip-git-repo-check",
            "resume",
            SESSION,
            "--json",
            "-o",
            remote + "/report.md",
            "-",
            input=prompt,
            output_path=local / "events.jsonl",
        )
        validate_events(events)
        body = sbx("exec", SANDBOX, "python3", "-c", READ_REPORT, remote + "/report.md")
        (local / "report.md").write_text(body, encoding="utf-8")
        validate_report(body, url, day, sha)
        name = urlsplit(url).path.rsplit("/", 1)[-1].lower()
        # 檔名為「日期-skill名稱-review.md」；同日同名已存在時加 -2、-3…，絕不覆蓋既有報告。
        # "x" 模式讓「檢查存在」與「建立」成為單一原子操作。
        # README 只索引已通過格式與 session 檢查的結果。
        for n in count(1):
            filename = f"{day}-{name}{f'-{n}' if n > 1 else ''}-review.md"
            report = root / "reviews" / filename
            try:
                f = report.open("x", encoding="utf-8")
            except FileExistsError:
                continue
            break
        with f:
            f.write(body)
        update_indexes(root, filename, url, day, sha)
        print(
            f"報告：{report}\n已更新 README.md 與 reviews/README.md\n執行紀錄：{local}"
        )
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", nargs="?", help="OWNER/REPO 或 HTTPS repository URL")
    parser.add_argument(
        "--check", action="store_true", help="只確認既有 sandbox 與 Codex session"
    )
    args = parser.parse_args()
    if args.check and args.repo:
        parser.error("--check 不接受 repo")
    if not args.check and not args.repo:
        parser.error("請指定 repo，或使用 --check")
    try:
        if any(k not in _local for k in LOCAL_KEYS):
            raise RuntimeError(
                f"缺少 sbx.local.json 或欄位不全（需要 {', '.join(LOCAL_KEYS)}），格式見 README"
            )
        if args.check:
            print(json.dumps(check_sandbox(), ensure_ascii=False, indent=2))
        else:
            audit(args.repo)
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
