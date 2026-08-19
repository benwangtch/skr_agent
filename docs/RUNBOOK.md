# Runbook — 怎麼跑起來、怎麼驗證

這份文件回答兩個問題：「怎麼執行這支程式」和「怎麼確認它真的動」。架構決策不在這裡——那些在 [`docs/design/DESIGN.md`](design/DESIGN.md)。這裡只講操作。

目前的測試套件（`uv run pytest`）**完全不需要 API 金鑰**，因為它測的是接縫（授權規則、principal 解析、排程時序、A2A 訊息轉換），不是叫真的模型。所以「真的跑起來看它動」跟「跑測試」是兩件不同的事，這份文件把兩者都寫清楚，中間那段 §3 是實際會呼叫 LLM、花錢、需要金鑰的部分。

---

## 0. 專案現狀速覽

```
src/deep_research_agent/
  protocol.py, mesh.py, runtime.py     契約層 + agent-as-tool + deepagents 執行殼
  principals.py                        service_principal() vs user_principal()
  assembly.py                          build_mesh() —— 組裝整個系統的唯一入口
  config/                              env-driven 設定（llm.py 有真的接上；db.py / minio.py 是佔位）
  wiki/                                三個資料來源之一，唯一有授權模型的那個
  report/                              agent 本體 + BOM/news 資料來源
  serving/                             A2A server（streaming）+ cron-like scheduler
fixtures/                              4 家公司、5 頁 wiki（namespace: supply / platform / shared）、4 份原始週報
examples/
  run_report.py                        單次執行 agent（掃描或單一提問）
  run_service.py                       同時跑 A2A server + 排程
tests/                                 167 個測試，6 個檔案，全部不需要金鑰
```

---

## 1. 環境設置

需要 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync --group dev      # 建 .venv，照 uv.lock 裝套件（可重現）
cp .env.example .env
```

打開 `.env`，填一行就能跑（其他都有預設值，指向 OpenRouter + Qwen3.5，模擬公司內部 host）：

```bash
LLM_API_KEY=sk-or-v1-你的OpenRouter金鑰    # 去 https://openrouter.ai/keys 拿
```

驗證設定有生效，不用花錢：

```bash
uv run python -c "from deep_research_agent.config import get_llm; l=get_llm(); print(l.provider, l.resolved_base_url(), l.resolved_model())"
# 應該看到 openrouter https://openrouter.ai/api/v1 qwen/qwen3.5-plus-02-15
```

換成別的 provider（OpenAI / Anthropic，或公司內部 gateway）：見 `docs/design/DESIGN.md` §6，只改 `.env`，不改程式碼。內部 gateway 只要有 OpenAI-compatible 的 `/v1/chat/completions` 就能直接接，設 `LLM_PROVIDER=custom` + `LLM_BASE_URL` + `LLM_MODEL`。

---

## 2. 跑單元測試（不花錢、不用金鑰）

```bash
uv run pytest              # 167 個測試，約 3 秒
uv run pytest -q tests/test_wiki_authz.py     # 只跑某個檔案
uv run pytest -k aggregation                  # 只跑名字符合的測試
```

| 檔案 | 測什麼 |
|---|---|
| `test_wiki_authz.py`（32） | namespace 授權、clearance-gated namespace、aggregation leak 檢查 |
| `test_mesh.py`（15） | agent-as-tool 的 principal 綁定、citation 傳遞、拒絕處理 |
| `test_wiring.py`（30） | 每個 agent 的 tool 清單、沒有 subagent 能發布、fact-checker 沒有 search tool、scratchpad/停止條件/矛盾處理有進 prompt、import 風格 |
| `test_config.py`（22） | LLM config 的 provider 預設值、env var 覆蓋、chat model 建構 |
| `test_scheduler.py`（19） | cron 排程時序、job 失敗互不影響、hook 觸發 |
| `test_mcp.py`（17） | MCP 設定解析、連不上時的降級、對真的 MCP server 載 tool／呼叫／記 citation |
| `test_a2a_server.py`（32） | A2A executor 的 principal 解析、streaming 進度、task 生命週期、file artifact，加 6 個走真的 handler + HTTP 的整合測試 |

**這些測試在改動任何一行程式碼後都該先過。** 它們用 stub agent（假的、瞬間回應、不呼叫模型）驗證系統的接線邏輯——如果這裡壞了，接上真的模型也不會變好，先把這層修好再往下走。

---

## 3. End-to-end：真的跑一次，會呼叫模型、會花 token

這一段需要 §1 設好的 `LLM_API_KEY`。每一步都可以獨立驗證，建議照順序做一次，確認整條鏈路真的通。

### 3.1 最小驗證：問一句話，不動 wiki

```bash
uv run python examples/run_report.py --dry-run --tier critical
```

預期看到（`-v` 開 log 更清楚）：
- **委派 → 寫檔 → 讀檔 → 查核 → 發布**這條鏈：lead 呼叫 `task`（company-investigator），investigator 呼叫 `write_file` 寫 `/findings/<company>.md`，lead 用 `read_file` 讀回來，然後呼叫 `task`（fact-checker），最後才 `wiki_write_page`
- `Acme Semiconductor Ltd`（fixtures 裡唯一 tier=critical 的兩家之一）被掃到、且有找到新聞事件（fixtures 裡真的有一篇火災新聞）
- 輸出最後有 `--- citations ---` 區塊，列出至少一個 `external_url`（新聞）和一個 `wiki_page`/`raw_report`（因為 agent 應該會去 wiki 交叉比對既有記錄）
- 因為 `--dry-run`，不會真的寫 wiki，最後不會印 `wiki namespaces now: ...`

若這步失敗（例如 `LLM_API_KEY` 沒填或 gateway 連不上），錯誤會是 provider client 的連線／認證訊息。注意 key 沒填的話會在**建立 agent 的當下**就報錯，不是跑到一半才失敗——這代表問題在 §1 的 config，不在後面的邏輯。

### 3.2 使用者觸發：驗證權限範圍

```bash
uv run python examples/run_report.py --division supply --tier critical
```

跟 3.1 的差別：這次不帶 `--dry-run`，agent 會真的呼叫 `wiki_write_page` 把報告發布出去。因為觸發者是 `user_principal("demo.user@supply", "supply", roles={wiki.reader, wiki.writer})`，報告應該被寫進 `supply` namespace（不是 `exec`）。跑完後確認：

```
wiki namespaces now: platform, shared, supply
```

（這是印出 fixture 裡目前所有 namespace，`supply` 本來就有頁面，看不出新頁面是否真的寫入——想確認真的寫進去了，用 `-v` 開 log，會看到 `wiki.write page=supply/incident-report-... subject=demo.user@supply` 這行。）

```bash
uv run python examples/run_report.py --division supply --tier critical -v 2>&1 | grep "wiki.write"
```

### 3.3 驗證權限真的有擋：唯讀使用者不能發布

```bash
uv run python examples/run_report.py --reader-only
```

預期：agent 會嘗試發布，被 `wiki_write_page` 拒絕（因為這個 principal 沒有 `wiki.writer` role），然後**agent 會照 prompt 指示回報「內容做完了但沒發布，因為權限不足」，而不是想辦法繞過去**。這是 `report/agent.py` 系統提示裡明講的行為，值得在這步實際確認 agent 真的照做，而不是只看程式碼寫了什麼。

### 3.4 排程帳號：驗證跨部門彙整 + exec namespace

```bash
uv run python examples/run_report.py --scheduled -v 2>&1 | grep -E "wiki.write|wiki namespaces"
```

預期：`service_principal()` 掃全部 BOM（不只 critical tier），如果報告內容橫跨多個 division，應該寫進 `exec` namespace，不是 `shared` 或任何單一 division——這是 `WikiAuthorizer.check_aggregation` 在擋的東西（design doc §5）。如果看到它寫進了 `shared`，那是真的 bug，不是預期行為。

### 3.5 單一提問：驗證 agent 自己會挑對工具

```bash
uv run python examples/run_report.py --ask "What is our exposure on the ASC-4400 controller?"
```

預期：agent 判斷這題用 `wiki_search` / `wiki_read_page` 就夠（fixtures 裡 `supply/acme-semiconductor` 本來就提到 ASC-4400 是單一供應商），**不需要**把整份 BOM 掃一遍、也不需要外部新聞搜尋。用 `-v` 看它實際呼叫了哪些 tool：

```bash
uv run python examples/run_report.py --ask "..." -v 2>&1 | grep -i "tool"
```

如果它對這種純內部查詢也去跑完整 sweep，代表 `report/agent.py` 的 system prompt 需要調整——這是驗證「deep research agent 會不會把力氣花在不需要的地方」的地方。對照組：

```bash
uv run python examples/run_report.py --ask "What happened with our suppliers this week?"
```

這句需要外部研究，應該會看到 `search_news` / `fetch_article` 出現。

### 3.6 A2A server + 排程：兩個一起跑

開一個 terminal：

```bash
uv run python examples/run_service.py --port 8000 --cron "*/5 * * * *" -v
```

（`--cron "*/5 * * * *"` 是為了測試用，每 5 分鐘跑一次；正式環境用預設的 `0 8 * * 1`，週一早上 8 點 UTC。）

另一個 terminal 驗證 A2A server：

```bash
curl -s http://localhost:8000/.well-known/agent-card.json | python3 -m json.tool
```

預期看到 `"name": "deep_research_agent"`、`skills` 陣列有 `deep_research_agent`、`capabilities.streaming` 是 `true`。

```bash
curl -s http://localhost:8000/ \
  -H 'Content-Type: application/json' -H 'A2A-Version: 1.0' \
  -d '{"jsonrpc":"2.0","id":"1","method":"SendMessage","params":{
        "message":{"messageId":"m1","role":"ROLE_USER",
                   "parts":[{"text":"What incidents affected Acme Semiconductor?"}]}}}' \
  | python3 -m json.tool
```

預期 `result.task.status.state` 是 `TASK_STATE_COMPLETED`，`result.task.status.message.parts[0].text` 有實際內容（答案在**終態事件**上，不是中途的 working 事件——非串流呼叫方只看得到終態）。

**`A2A-Version: 1.0` header 跟 body 格式必須配套**，這是 a2a-sdk 1.x 最容易踩的地方：header 沒帶時 SDK 會當成 `0.3`，不是當成「沒有版本」。實測三種組合：

| header | body | 結果 |
|---|---|---|
| `A2A-Version: 1.0` | 1.0（`SendMessage`、`ROLE_USER`） | 正常 |
| 不帶 | 0.3（`message/send`、`user`、`kind":"text"`） | 正常，走 v0.3 compat |
| 不帶 | 1.0 | `VERSION_NOT_SUPPORTED`（版本不符，不是「缺 header」） |

所以舊的 0.3 client 不用改也能繼續打（第二列），但要走 1.0 就 header 跟 body 都得換。

想看串流進度（長時間的 sweep 建議用這個），method 換成 `SendStreamingMessage`，回應是 SSE：

```bash
curl -N -s http://localhost:8000/ \
  -H 'Content-Type: application/json' -H 'A2A-Version: 1.0' \
  -d '{"jsonrpc":"2.0","id":"1","method":"SendStreamingMessage","params":{
        "message":{"messageId":"m2","role":"ROLE_USER",
                   "parts":[{"text":"Sweep the critical tier"}]}}}'
```

預期先看到 `TASK_STATE_SUBMITTED`，接著一連串 `TASK_STATE_WORKING`（`Working: search_news`、`Finished: search_news` 之類），中間夾帶 `artifactUpdate`（如果報告產了檔案），最後是 `TASK_STATE_COMPLETED`。注意 1.x 的事件包在 `result.statusUpdate` / `result.artifactUpdate` 底下，不像 0.3 直接放在 `result`。

排程那邊：等到 cron 觸發的分鐘數（用 `*/5` 的話最多等 5 分鐘），觀察第一個 terminal 的 log，應該看到：

```
scheduler.fire job=weekly-bom-sweep agent=deep_research_agent trace=...
```

跑完後可以再打一次 agent-card 或直接看 log 裡 `wiki.write` 那行，確認排程觸發的這次也走了同一套授權邏輯（`service_principal()`，寫進 `exec`）。

按 `Ctrl+C` 結束（同時停掉 A2A server 跟排程，兩者在同一個 process）。

---

## 3.7 接上你自己的 MCP service

設定在 `.env`（細節見 `docs/design/DESIGN.md` §6）：

```bash
MCP_URL=https://mcp.internal.corp/mcp
MCP_TOKEN=內部服務發的憑證
```

**先確認 tool 真的載得到，不必叫模型、不花 token：**

```bash
uv run python -c "
import asyncio; from deep_research_agent.mcp import load_mcp_tools
for s, t in asyncio.run(load_mcp_tools()):
    print(f'{s}: {t.name} — {t.description[:70]}')
"
```

沒有輸出代表兩種可能，要分清楚：

| 現象 | 意思 | 怎麼辦 |
|---|---|---|
| 完全沒輸出，也沒有 log | `MCP_*` 沒設到，`connections()` 是空的 | 檢查 `.env` 有沒有被讀到：`uv run python -c "from deep_research_agent.config import get_mcp; print(get_mcp().connections())"` |
| log 有 `mcp.load_failed` | 有設定但連不上／認證失敗 | 看那行 exception。連不上是**跳過**不是致命錯，所以 agent 還是會起來，只是少了那些 tool |

**接著跑一次真的 agent**，確認模型看得到、也會用：

```bash
uv run python examples/run_report.py --ask "<一個需要用到你 MCP tool 的問題>" -v
```

預期：開頭印出 `→ MCP tools loaded from configured server(s)`，最後的 `--- citations ---` 區塊裡有 `mcp://<server>/<tool>` 這種 ref——**那就是 MCP 真的被呼叫過的證據**，模型只是嘴上說有用不會產生這個 citation。

如果 tool 載進來了但模型不用它，問題在描述不在接線：MCP server 給的 tool description 就是模型用來決定要不要呼叫的唯一依據，太模糊就不會被選中。

`--- citations ---` 沒有 `mcp://` 但你認為應該有的時候，用 `-v` 看 log 裡有沒有 `mcp.loaded server=... tools=[...]`，先確認那個 tool 到底有沒有進到 agent 的 tool 清單。

### 一個上線前必須知道的限制

**MCP 呼叫不會帶上觸發者的身份。** `MCP_TOKEN` 是連線層級的服務憑證，所以不管是誰觸發這次執行（一般使用者 / 排程帳號 / A2A 呼叫方），MCP server 看到的都是同一個身份。

也就是說：**只接「這個 agent 權限最低的使用者也可以看到全部內容」的 MCP server。** 如果那個 service 內部有 per-user 權限，現在這個接法會繞過它。細節見 `docs/design/DESIGN.md` §5.5。

---

## 3.8 放進你自己的 skill

Skill 是「報告格式、判準」這類**每次跑都必須遵守**的規範。加一個新的：

```bash
mkdir -p .claude/skills/your-skill
$EDITOR .claude/skills/your-skill/SKILL.md
```

`SKILL.md` 的格式（YAML frontmatter + markdown 本文）：

```markdown
---
name: your-skill
description: 一句話講這個 skill 管什麼
---

# 標題

實際的規範內容。
```

然後在 `src/deep_research_agent/report/agent.py` 的 `DEFAULT_SKILLS` 加上名字：

```python
DEFAULT_SKILLS = ("incident-report", "your-skill")
```

驗證它真的進到 prompt（不花錢）：

```bash
uv run python -c "
from pathlib import Path; from deep_research_agent import build_mesh
m = build_mesh(fixtures='fixtures', project_root='.')
p = m.report_agent._full_system_prompt()
print('your-skill 在 prompt 裡:', 'your-skill' in p)
print('prompt 長度:', len(p))
"
```

**frontmatter 會被丟掉，只有本文進 prompt**——frontmatter 是給 skill catalog 用的 metadata，inline 進 prompt 只是浪費 token。

**這些 skill 是整份塞進 system prompt 的，不是 progressive disclosure**（理由見 design doc §2.3：必須遵守的規範不該靠模型自己記得去讀檔）。代價是每一份 skill 的全文都佔 context——**skill 多到十幾份、而且大部分情況只有一兩份相關的時候，這個取捨就該翻轉**，改用 `create_deep_agent(skills=...)` 的按需載入。目前一兩份的規模，inline 是對的。

---

## 4. 這樣算「測過」了嗎？

跑完 §3 全部六步，代表確認過：

- LLM config 真的連得上、真的能拿到回應（3.1）
- 使用者觸發的報告寫進正確的 namespace（3.2）
- 沒有寫入權限時系統會擋下來、且 agent 照規矩回報而不是硬幹（3.3）
- 排程帳號的跨部門彙整寫進 clearance 夠的 namespace，不會外洩（3.4）
- Agent 會依問題類型選對 tool，不會對純內部查詢做整份掃描（3.5）
- A2A server 跟排程可以在同一個 process 穩定跑，外部呼叫走得通（3.6）
- （若有設定）MCP tool 載得到、模型會用、而且留下 `mcp://` citation（3.7）

這六步涵蓋了 `docs/design/DESIGN.md` §9 列出的「已知限制」之外的核心行為。**沒有自動化的 e2e 測試**（§2 的 167 個測試都用 stub，不叫真的模型）——這是刻意的，因為每次 CI 跑都花 token、還會因為模型輸出的隨機性讓測試不穩定。真要把 §3 這幾步自動化，做法是寫一支跑在 CI 之外（例如手動觸發或排程跑一次）的 smoke test script，判準改成寬鬆的（例如「有沒有 citations」而不是「內容逐字符合」）——這份文件目前先提供人工跑過一遍的步驟，還沒做那支腳本。

## 5. 常見卡住的地方

| 現象 | 大概率原因 | 對應章節 |
|---|---|---|
| 一啟動就報 missing credentials | `LLM_API_KEY` 沒填——現在是建構時就擋，不是跑到一半 | §1 |
| `run_report.py` 卡住很久或連線類錯誤 | `LLM_BASE_URL` 指向連不到的 gateway | §1 |
| 報告寫進錯的 namespace | 檢查是不是漏帶 `--scheduled`（會用不同 principal） | §3.2 / §3.4 |
| A2A `curl` 回 `VERSION_NOT_SUPPORTED` | 帶了 1.0 的 body 但沒帶 `A2A-Version: 1.0` header——沒帶會被當成 0.3 | §3.6 |
| A2A `curl` 回 method not found | 1.x 的 method 名是 `SendMessage` / `SendStreamingMessage`；`message/send` 是 0.3 的名字（不帶 header 時才走那條） | §3.6 |
| 排程一直不觸發 | cron 表達式算的下一次時間比想像中晚——`*/5 * * * *` 最多等 5 分鐘，不是啟動時立刻跑（這是真正的 cron 語意，見 design doc §5） | §3.6 |
| 想確認 wiki 真的被寫入，但 `list_namespaces()` 看不出新舊 | 用 `-v` 開 log，找 `wiki.write` 那行 | §3.2 |
| MCP tool 沒出現在 agent 身上 | 分辨「沒設定」跟「連不上」——看 log 有沒有 `mcp.load_failed` | §3.7 |
| MCP tool 載到了但模型不呼叫它 | tool description 太模糊；那是模型唯一的判斷依據 | §3.7 |
| 改了 SKILL.md 但行為沒變 | 確認 skill 名字有加進 `DEFAULT_SKILLS`；沒加就不會被載 | §3.8 |
