# Skill Auditor

提供一個 skill repo，固定續用既有的 sbx sandbox 與其 Codex 審查 session，產出繁體中文安全審查報告，存入本目錄的 `reviews/`，並自動更新本頁與 [報告索引](reviews/README.md)。

```text
repo → 檢查既有 sandbox / session → sandbox 內下載固定版本
     → Codex resume：/security-review → 驗證完成事件與報告欄位
     → reviews/日期-skill名稱-review.md → 更新 README
```

## 使用方式

需求：macOS／Linux、Python 3.9 以上的標準函式庫、已登入且可使用的 `sbx`。不需安裝 Python 套件。

```bash
# 檢查既有 sandbox 與 session；停止中的 sandbox 會由 sbx 自動啟動
python3 scripts/audit_skill.py --check

# GitHub 簡寫
python3 scripts/audit_skill.py CloudAI-X/threejs-skills

# 或 HTTPS repo URL（支援 GitLab 群組路徑；不接受憑證與 URL 參數）
python3 scripts/audit_skill.py https://github.com/OWNER/REPO
```

也可以在這個專案中直接要求 agent「審查 https://github.com/OWNER/REPO」，專案 [AGENTS.md](AGENTS.md) 會指向同一流程。

每次下載預設分支當下的版本，在報告標示完整 SHA；不抓 submodules、不下載 Git LFS 實體內容。需另外審查這些內容時，報告必須說明限制。下載或存取被拒時會停止，不會自動放寬網路政策或要求提供憑證。

## 固定重用對象

sandbox 名稱、ID、Codex session ID 與 agent 工作目錄屬於本機環境資訊，不納入版本控制。請在專案根目錄建立未追蹤的 `sbx.local.json`（已列入 `.gitignore`），agent 必須是 `codex`：

```json
{
  "sandbox": "<sandbox 名稱>",
  "sandbox_id": "<sandbox ID>",
  "session": "<既有 Codex session ID>",
  "workspace": "<agent 工作目錄>"
}
```

缺少此檔或欄位不全時，腳本會直接報錯停止。

只呼叫 `sbx ls`、`sbx exec`、`sbx cp`；不建立、重建或移除 sandbox，不使用 `resume --last`、fork 或全新 session。固定 session 是既有 sandbox 最近一次產出／整理報告的對話。原本的互動程序目前未執行；每次以 `codex exec resume <ID>` 續接同一對話，不是維持同一個 OS 程序。

身分或 session 不符即停止；sandbox 被重建時不會自動改綁。請勿在其他終端同時操作此 session；同一專案的流程以檔案鎖防止並行執行。

## 審查與發布規則

`/security-review` 的批次審查定義位於 [prompts/security-review.md](prompts/security-review.md)，會作為受信任的 prompt 傳給既有 Codex agent。這是本流程的路由名稱，不假設 Codex CLI 內建此 slash command。

三份 [既有報告](reviews/README.md) 會複製到每次 sandbox 審查目錄作為格式參考。審查涵蓋指示、腳本、hooks、依賴、權限、網路、秘密資料與安裝流程。預設只做靜態審查，不安裝或執行待審 repo；測試、lint 與動態驗證未執行時，報告需明說。

本機負責驗證 session ID、完成事件、必要章節、日期與 SHA，再寫入新報告及索引；格式檢查不能證明模型審查內容正確。既有報告與手寫 README 內容會保留，自動索引使用 HTML 註解標記。

每次執行的 prompt、事件與取得的報告保留在 `.audit-runs/<run-id>/`；sandbox 原始碼與報告保留在 `<workspace>/.skill-auditor/<run-id>/`。失敗不會登錄索引，也不會自動重新呼叫模型；修正原因後再執行。這些目錄不自動清除，長期使用需自行整理。

## 驗證本流程

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts tests
```

沒有另設 lint 工具；`compileall` 是 Python 語法檢查。測試使用假的 sbx 回覆檢查管線與失敗情境，不會呼叫模型或安裝待審技能。

## 既有報告

`reviews/` 內含第三方 skill 的審查報告與本機環境資訊，已列入 `.gitignore`、不納入版本控制；下方與自動索引的連結只在本機有效。審查流程需要 `reviews/` 內有下列三份參考報告，clone 後請自備同名檔案。

| 日期 | 專案 | 報告 |
| --- | --- | --- |
| 2026-11-05 | CloudAI-X/threejs-skills | [完整安全審查（英文）](reviews/threejs-skills-security-audit.md) |
| 2026-11-06 | gamedev-skills/awesome-gamedev-agent-skills | [技能庫審查](reviews/gamedev-agent-skills-review.md) |
| 2026-11-06 | Donchitos/Claude-Code-Game-Studios | [工作室框架審查](reviews/claude-code-game-studios-review.md) |

三份既有報告的方法與覆蓋範圍不同，不能直接比較風險等級。

## 自動產出的報告

<!-- skill-auditor:reviews:start -->

| 日期 | 專案 | 版本 | 報告 |
| --- | --- | --- | --- |
| 2026-11-09 | github.com/Bomx/super-video-maker-skill | `3fec4aa66a1d` | [審查報告](reviews/2026-11-09-super-video-maker-skill-review.md) |
| 2026-11-09 | github.com/CloudAI-X/threejs-skills | `0e181866f095` | [審查報告](reviews/2026-11-09-threejs-skills-review.md) |

<!-- skill-auditor:reviews:end -->
