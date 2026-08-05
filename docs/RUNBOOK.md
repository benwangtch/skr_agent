# Runbook — 怎麼跑起來、怎麼驗證

這份文件回答兩個問題：「怎麼執行這支程式」和「怎麼確認它真的動」。架構決策不在這裡——那些在 `docs/design/03-agent-architecture-and-serving.md`（現行架構：deepagents、A2A、排程）。這裡只講操作。

目前的測試套件（`uv run pytest`）**完全不需要 API 金鑰**，因為它測的是接縫（授權規則、principal 解析、排程時序、A2A 訊息轉換），不是叫真的模型。所以「真的跑起來看它動」跟「跑測試」是兩件不同的事，這份文件把兩者都寫清楚，中間那段 §3 是實際會呼叫 LLM、花錢、需要金鑰的部分。

---

## 0. 專案現狀速覽

```
src/skr_agent/
  protocol.py, mesh.py, runtime.py     契約層 + agent-as-tool + deepagents 執行殼
  principals.py                        service_principal() vs user_principal()
  assembly.py                          build_mesh() —— 組裝整個系統的唯一入口
  copilot.py                           路由層（非重點，見 README）
  config/                              env-driven 設定（llm.py 有真的接上；db.py / minio.py 是佔位）
  wiki/                                三個資料來源之一，唯一有授權模型的那個
  report/                              skr agent 本體 + BOM/news 資料來源
  serving/                             A2A server（streaming）+ cron-like scheduler
fixtures/                              4 家公司、5 頁 wiki（namespace: supply / platform / shared）、4 份原始週報
examples/
  run_report.py                        單次執行 skr agent 或 copilot
  run_service.py                       同時跑 A2A server + 排程
tests/                                 135 個測試，6 個檔案，全部不需要金鑰
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
uv run python -c "from skr_agent.config import get_llm; l=get_llm(); print(l.provider, l.resolved_base_url(), l.resolved_model())"
# 應該看到 openrouter https://openrouter.ai/api/v1 qwen/qwen3.5-plus-02-15
```

換成別的 provider（OpenAI / Anthropic，或公司內部 gateway）：見 `docs/design/03-agent-architecture-and-serving.md` §2.2，只改 `.env`，不改程式碼。內部 gateway 只要有 OpenAI-compatible 的 `/v1/chat/completions` 就能直接接，設 `LLM_PROVIDER=custom` + `LLM_BASE_URL` + `LLM_MODEL`。

---

## 2. 跑單元測試（不花錢、不用金鑰）

```bash
uv run pytest              # 135 個測試，約 3 秒
uv run pytest -q tests/test_wiki_authz.py     # 只跑某個檔案
uv run pytest -k aggregation                  # 只跑名字符合的測試
```

| 檔案 | 測什麼 |
|---|---|
| `test_wiki_authz.py`（32） | namespace 授權、clearance-gated namespace、aggregation leak 檢查 |
| `test_mesh.py`（15） | agent-as-tool 的 principal 綁定、citation 傳遞、拒絕處理 |
| `test_wiring.py`（21） | 每個 agent 的 tool 清單、subagent 拿不到 write tool、報告規範真的進 prompt |
| `test_config.py`（22） | LLM config 的 provider 預設值、env var 覆蓋、chat model 建構 |
| `test_scheduler.py`（19） | cron 排程時序、job 失敗互不影響、hook 觸發 |
| `test_a2a_server.py`（26） | A2A executor 的 principal 解析、streaming 進度、task 生命週期、file artifact |

**這些測試在改動任何一行程式碼後都該先過。** 它們用 stub agent（假的、瞬間回應、不呼叫模型）驗證系統的接線邏輯——如果這裡壞了，接上真的模型也不會變好，先把這層修好再往下走。

---

## 3. End-to-end：真的跑一次，會呼叫模型、會花 token

這一段需要 §1 設好的 `LLM_API_KEY`。每一步都可以獨立驗證，建議照順序做一次，確認整條鏈路真的通。

### 3.1 最小驗證：問一句話，不動 wiki

```bash
uv run python examples/run_report.py --dry-run --tier critical
```

預期看到：
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

### 3.5 走 copilot 路由

```bash
uv run python examples/run_report.py --ask "What is our exposure on the ASC-4400 controller?"
```

預期：copilot 會判斷這個問題該用 `wiki_search`（內部已知資訊，fixtures 裡 `supply/acme-semiconductor` 頁面本來就提到 ASC-4400 是單一供應商），**不需要**觸發整個 report agent 的外部新聞掃描——如果它每次都觸發完整 report agent，代表 copilot 的路由 prompt 需要調整。

```bash
uv run python examples/run_report.py --ask "Run a full incident sweep and tell me what happened with our suppliers this week"
```

這句話應該會讓 copilot 呼叫 `skr_agent`（因為問題需要外部研究），可以對照看兩種問法觸發的路徑是否符合預期。

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

預期看到 `"name": "skr_agent"`、`skills` 陣列有 `skr_agent`、`capabilities.streaming` 是 `true`。

```bash
curl -s http://localhost:8000/ -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":"1","method":"message/send","params":{
        "message":{"messageId":"m1","role":"user",
                   "parts":[{"kind":"text","text":"What incidents affected Acme Semiconductor?"}]}}}' \
  | python3 -m json.tool
```

預期 `result.status.state` 是 `completed`，`result.history` 裡有實際內容。

想看串流進度（長時間的 sweep 建議用這個），把 method 換成 `message/stream`，回應是 SSE：

```bash
curl -N -s http://localhost:8000/ -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":"1","method":"message/stream","params":{
        "message":{"messageId":"m2","role":"user",
                   "parts":[{"kind":"text","text":"Sweep the critical tier"}]}}}'
```

預期先看到一連串 `"state":"working"` 的事件（`Working: search_news`、`Finished: search_news` 之類），最後才是 `"state":"completed"`、`"final":true`。

排程那邊：等到 cron 觸發的分鐘數（用 `*/5` 的話最多等 5 分鐘），觀察第一個 terminal 的 log，應該看到：

```
scheduler.fire job=weekly-bom-sweep agent=skr_agent trace=...
```

跑完後可以再打一次 agent-card 或直接看 log 裡 `wiki.write` 那行，確認排程觸發的這次也走了同一套授權邏輯（`service_principal()`，寫進 `exec`）。

按 `Ctrl+C` 結束（同時停掉 A2A server 跟排程，兩者在同一個 process）。

---

## 4. 這樣算「測過」了嗎？

跑完 §3 全部六步，代表確認過：

- LLM config 真的連得上、真的能拿到回應（3.1）
- 使用者觸發的報告寫進正確的 namespace（3.2）
- 沒有寫入權限時系統會擋下來、且 agent 照規矩回報而不是硬幹（3.3）
- 排程帳號的跨部門彙整寫進 clearance 夠的 namespace，不會外洩（3.4）
- Copilot 的路由邏輯會依問題類型選對 tool（3.5）
- A2A server 跟排程可以在同一個 process 穩定跑，外部呼叫走得通（3.6）

這六步涵蓋了 `docs/design/03-agent-architecture-and-serving.md` §8 列出的「已知限制」之外的核心行為。**沒有自動化的 e2e 測試**（§2 的 135 個測試都用 stub，不叫真的模型）——這是刻意的，因為每次 CI 跑都花 token、還會因為模型輸出的隨機性讓測試不穩定。真要把 §3 這幾步自動化，做法是寫一支跑在 CI 之外（例如手動觸發或排程跑一次）的 smoke test script，判準改成寬鬆的（例如「有沒有 citations」而不是「內容逐字符合」）——這份文件目前先提供人工跑過一遍的步驟，還沒做那支腳本。

## 5. 常見卡住的地方

| 現象 | 大概率原因 | 對應章節 |
|---|---|---|
| 一啟動就報 missing credentials | `LLM_API_KEY` 沒填——現在是建構時就擋，不是跑到一半 | §1 |
| `run_report.py` 卡住很久或連線類錯誤 | `LLM_BASE_URL` 指向連不到的 gateway | §1 |
| 報告寫進錯的 namespace | 檢查是不是漏帶 `--scheduled`（會用不同 principal） | §3.2 / §3.4 |
| A2A `curl` 回 method not found | a2a-sdk 0.3.x 的 method 名是 `message/send` / `message/stream`（不是 1.x 的 `SendMessage`） | §3.6 |
| 排程一直不觸發 | cron 表達式算的下一次時間比想像中晚——`*/5 * * * *` 最多等 5 分鐘，不是啟動時立刻跑（這是真正的 cron 語意，見 design doc §5） | §3.6 |
| 想確認 wiki 真的被寫入，但 `list_namespaces()` 看不出新舊 | 用 `-v` 開 log，找 `wiki.write` 那行 | §3.2 |
