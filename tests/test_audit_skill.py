from contextlib import redirect_stdout
from datetime import datetime
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import audit_skill as auditor


SHA = "a" * 40
URL = "https://github.com/example/skills"


def report(url=URL, day=None, sha=SHA):
    day = day or datetime.now().astimezone().date().isoformat()
    return f"""# skills 審查報告

- 儲存庫：{url}
- 審查日期：{day}
- 審查版本：`{sha}`

## 結論
僅為測試資料。
## 範圍、方法與限制
## 技能與執行面盤點
## 發現與證據
## 實測結果
## 採用建議
"""


def events(session=auditor.SESSION, final="turn.completed"):
    return (
        "\n".join(
            json.dumps(item)
            for item in (
                {"type": "thread.started", "thread_id": session},
                {"type": final},
            )
        )
        + "\n"
    )


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "reviews").mkdir()
        (self.root / "prompts").mkdir()
        (self.root / "README.md").write_text("# Usage\n\nKeep manual content.\n")
        (self.root / "reviews/README.md").write_text(
            "# Existing reviews\n\nKeep old links.\n"
        )
        for name in auditor.REFERENCES:
            (self.root / "reviews" / name).write_text("Original reference: " + name)
        (self.root / "prompts/security-review.md").write_text(
            "# /security-review\nTrusted instructions."
        )
        self.calls = []

    def fake_sbx(self, *args, input=None, output_path=None):
        self.calls.append((args, input))
        if args == ("ls", "--json"):
            return json.dumps(
                [{"name": auditor.SANDBOX, "id": auditor.SANDBOX_ID, "agent": "codex"}]
            )
        if auditor.PROBE in args:
            return json.dumps({"session": auditor.SESSION, "cwd": auditor.WORKSPACE})
        if input == auditor.PREPARE:
            self.assertEqual(args[-1], URL)
            return json.dumps({"commit": SHA})
        if args[0] == "cp":
            return ""
        if "resume" in args:
            self.assertEqual(args[args.index("resume") + 1], auditor.SESSION)
            self.assertEqual(args[-1], "-")
            self.assertEqual(args[args.index("-w") + 1], auditor.WORKSPACE)
            self.assertIn('"repository_url": "' + URL + '"', input)
            self.assertIn('"commit": "' + SHA + '"', input)
            output = events()
            output_path.write_text(output)
            return output
        if auditor.READ_REPORT in args:
            return report()
        self.fail(f"Unexpected sbx call: {args}")

    def run_audit(self):
        with (
            patch.object(auditor, "sbx", side_effect=self.fake_sbx),
            redirect_stdout(io.StringIO()),
        ):
            return auditor.audit("example/skills", self.root)

    def test_repository_input(self):
        self.assertEqual(auditor.repository("example/skills.git"), URL)
        self.assertEqual(auditor.repository(URL), URL)
        self.assertEqual(
            auditor.repository("https://gitlab.com/group/sub/skills.git"),
            "https://gitlab.com/group/sub/skills",
        )
        for bad in (
            "--help",
            "https://u:token@github.com/a/b",
            "http://github.com/a/b",
            URL + "?token=secret",
            URL + "#main",
            URL + "/",
            "a/..",
            "https://github.com/a/../b",
            "https://github.com/a/$(touch-x)",
            "ssh://git@github.com/a/b",
            "https://github.com/a/%2e%2e",
            "a/.git",
        ):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                auditor.repository(bad)

    def test_complete_pipeline_preserves_references_and_manual_content(self):
        path = self.run_audit()
        self.assertEqual(path.parent, self.root / "reviews")
        self.assertEqual(
            path.name,
            f"{datetime.now().astimezone().date().isoformat()}-skills-review.md",
        )
        self.assertEqual(path.read_text(), report())
        for name in auditor.REFERENCES:
            self.assertEqual(
                (self.root / "reviews" / name).read_text(),
                "Original reference: " + name,
            )
        self.assertEqual(sum(args[0] == "cp" for args, _ in self.calls), 3)
        self.assertFalse(
            any(args[0] in ("run", "create", "rm", "stop") for args, _ in self.calls)
        )
        self.assertIn(f"](reviews/{path.name})", (self.root / "README.md").read_text())
        self.assertIn("Keep manual content.", (self.root / "README.md").read_text())
        self.assertIn(f"]({path.name})", (self.root / "reviews/README.md").read_text())
        self.assertIn("Keep old links.", (self.root / "reviews/README.md").read_text())
        self.assertEqual(
            len(list((self.root / ".audit-runs").glob("*/events.jsonl"))), 1
        )

    def test_repeat_runs_keep_history(self):
        first = self.run_audit()
        second = self.run_audit()
        self.assertNotEqual(first, second)
        # 同日同名時第二份加序號，第一份保持原樣。
        self.assertTrue(second.name.endswith("-skills-2-review.md"))
        self.assertEqual(first.read_text(), report())
        body = (self.root / "README.md").read_text()
        self.assertEqual(body.count(auditor.START), 1)
        self.assertEqual(body.count(f"](reviews/{first.name})"), 1)
        self.assertEqual(body.count(f"](reviews/{second.name})"), 1)

    def test_missing_or_recreated_sandbox_fails_without_create(self):
        for sandboxes in (
            [],
            [{"name": auditor.SANDBOX, "id": "different", "agent": "codex"}],
            [{"name": auditor.SANDBOX, "id": auditor.SANDBOX_ID, "agent": "claude"}],
        ):
            with patch.object(
                auditor, "sbx", return_value=json.dumps(sandboxes)
            ) as mock:
                with self.assertRaises(RuntimeError):
                    auditor.check_sandbox()
                mock.assert_called_once_with("ls", "--json")

    def test_missing_session_fails_without_resume(self):
        with patch.object(
            auditor,
            "sbx",
            side_effect=[
                json.dumps(
                    [
                        {
                            "name": auditor.SANDBOX,
                            "id": auditor.SANDBOX_ID,
                            "agent": "codex",
                        }
                    ]
                ),
                RuntimeError("Required existing Codex session not found"),
            ],
        ) as mock:
            with self.assertRaises(RuntimeError):
                auditor.check_sandbox()
            self.assertEqual(mock.call_count, 2)

    def test_failed_or_wrong_session_events_rejected(self):
        for output in (
            events("another-session"),
            events(final="turn.failed"),
            events(final="turn.started"),
            events() + '{"type":"error"}\n',
            "",
        ):
            with self.subTest(output=output), self.assertRaises(RuntimeError):
                auditor.validate_events(output)

    def test_invalid_report_does_not_publish(self):
        original_sbx = self.fake_sbx

        def incomplete(*args, **kwargs):
            if auditor.READ_REPORT in args:
                return "# Incomplete review\n"
            return original_sbx(*args, **kwargs)

        with (
            patch.object(auditor, "sbx", side_effect=incomplete),
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(RuntimeError):
                auditor.audit("example/skills", self.root)
        self.assertEqual(len(list((self.root / "reviews").glob("*.md"))), 4)
        self.assertNotIn(auditor.START, (self.root / "README.md").read_text())
        self.assertTrue(list((self.root / ".audit-runs").glob("*/report.md")))

    def test_report_metadata_must_match(self):
        auditor.validate_report(report(day="2026-11-09"), URL, "2026-11-09", SHA)
        for url, day, sha in (
            (URL + "-other", "2026-11-09", SHA),
            (URL, "2026-11-08", SHA),
            (URL, "2026-11-09", "b" * 40),
        ):
            with self.assertRaises(RuntimeError):
                auditor.validate_report(report(day="2026-11-09"), url, day, sha)
        for body in (
            report(url=URL + "-other", day="2026-11-09"),
            report(day="2026-11-09").replace("## 結論", "## 結論尚未完成"),
        ):
            with self.assertRaises(RuntimeError):
                auditor.validate_report(body, URL, "2026-11-09", SHA)

    def test_broken_index_stops_before_clone(self):
        (self.root / "README.md").write_text(auditor.START)
        with (
            patch.object(auditor, "sbx", side_effect=self.fake_sbx),
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(ValueError):
                auditor.audit("example/skills", self.root)
        self.assertFalse(any(input == auditor.PREPARE for _, input in self.calls))
        for body in (auditor.END + auditor.START, auditor.START * 2 + auditor.END * 2):
            with self.assertRaises(ValueError):
                auditor.index_text(body, [])

    def test_concurrent_run_is_rejected(self):
        with auditor.audit_lock(self.root):
            with self.assertRaises(RuntimeError):
                with auditor.audit_lock(self.root):
                    self.fail("Second lock was acquired")
        with auditor.audit_lock(self.root):
            pass

    def test_subprocess_uses_argument_array_and_preserves_failure_logs(self):
        output_path = self.root / "events.jsonl"
        result = subprocess.CompletedProcess(
            [], 1, '{"type":"turn.failed"}\n', "failure"
        )
        with patch.object(auditor.subprocess, "run", return_value=result) as mock:
            with self.assertRaises(RuntimeError):
                auditor.sbx(
                    "exec",
                    auditor.SANDBOX,
                    "echo",
                    "$(touch /tmp/nope)",
                    input="prompt",
                    output_path=output_path,
                )
        self.assertEqual(
            mock.call_args.args[0],
            [
                "sbx",
                "--no-input",
                "exec",
                auditor.SANDBOX,
                "echo",
                "$(touch /tmp/nope)",
            ],
        )
        self.assertNotIn("shell", mock.call_args.kwargs)
        self.assertEqual(output_path.read_text(), result.stdout)
        self.assertEqual(output_path.with_suffix(".stderr").read_text(), "failure")


if __name__ == "__main__":
    unittest.main()
