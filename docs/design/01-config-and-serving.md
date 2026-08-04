# Config、A2A serving、排程——設計與怎麼用

狀態：實作完成
範圍：`src/skr_agent/config/`、`src/skr_agent/serving/`

---

## 0. 這次做了什麼

1. **Config**：每個要對外部 IO 服務（LLM、DB、Minio...）的設定，統一成一個 `config/` 資料夾，每個服務一個檔案、一個 class、一個 env prefix，全部繼承你給的 `BaseConfig` 樣式。
2. **A2A server**：把任何一個 `DeepAgent`（現在是 report agent）serve 成一個 A2A 協定的 HTTP server，讓外部 agent 可以呼叫它。
3. **排程**：一個 in-process 的 cron-like scheduler，用來定期執行某個 skill（例如每週 BOM 掃描），跟 A2A server 在同一個 process、同一個 event loop 裡跑。

三者用 `examples/run_service.py` 串起來：一個 process，同時服務 A2A 請求、同時跑排程。

---

## 1. Config——照你的格式做的

你給的格式：

```python
class BaseConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

class Minio(BaseConfig):
    endpoint: str = '...'
    ...
    model_config = SettingsConfigDict(env_prefix='minio_')
```

這個格式在 pydantic v2 下有個關鍵行為（我有先寫小測試驗證過，不是憑印象）：**子類別的 `model_config` 會跟父類別合併，不是整個蓋掉**——所以 `Minio.model_config` 同時擁有 `env_file`（繼承自 `BaseConfig`）跟 `env_prefix`（自己設的）。這正是這個 pattern 能運作的原因。

現在的結構：

```
src/skr_agent/config/
  __init__.py    匯出所有 class + get_* 函式 + reset_settings_cache()
  base.py        BaseConfig
  llm.py         LLM（真的有接到 runtime.py）
  db.py          DB（佔位，還沒有東西在用）
  minio.py       Minio（照你貼的範例原封不動搬過來，佔位）
```

新增服務的步驟：複製 `db.py`，改 class 名字、改 `env_prefix`、加欄位、加 `get_*()` cached getter、在 `__init__.py` 匯出。不需要碰其他任何檔案。

**唯一規則**：這個資料夾外面的程式碼不准直接讀 `os.environ` 拿設定值。所有設定都經過這裡的某個 `get_*()`。這是讓「換公司環境只改 env var」這件事成立的前提。

---

## 2. LLM config——為什麼不能只是把 base_url 換掉

這是你這次要求裡最容易做錯的地方，值得說清楚。

**Claude Agent SDK 不是一個「任意 LLM」的 client。** 它背後是 `claude` CLI 這個子行程，講的是 Anthropic Messages API 這個特定的 wire protocol（request/response 的 JSON schema、串流事件格式，都是 Anthropic 自己定義的）。所以：

> `ANTHROPIC_BASE_URL` 指到哪裡，那裡就必須聽得懂 Anthropic Messages API，不是隨便一個「能回應 chat」的端點都行。

OpenRouter 自己的主要端點（`https://openrouter.ai/api/v1`）是 **OpenAI 相容**格式，把它接到 Claude Agent SDK 上會直接壞掉，因為兩邊的 JSON schema 對不上。

好消息是：OpenRouter 另外開了一個 **Anthropic 相容端點**在 `https://openrouter.ai/api`（注意，沒有 `/v1`），這個端點會把請求轉譯過去、也把任何你點名的 model（包含 Qwen）接進來——這才是讓 `provider="openrouter"` 可以拿來「模擬公司內部 host」的原因：它不只是「湊巧格式一樣」，是它真的做了協定轉換。

```python
# config/llm.py::LLM.to_cli_env()
env = {"ANTHROPIC_API_KEY": ""}          # 刻意設成空字串，見下面解釋
if base_url: env["ANTHROPIC_BASE_URL"] = base_url
if key:       env["ANTHROPIC_AUTH_TOKEN"] = key   # 不是 ANTHROPIC_API_KEY
```

兩個容易踩的坑，已經在程式碼裡處理掉：

1. **金鑰要放在 `ANTHROPIC_AUTH_TOKEN`，不是 `ANTHROPIC_API_KEY`。** 這是 OpenRouter Anthropic 相容端點指定的認證方式。
2. **`ANTHROPIC_API_KEY` 要明確設成空字串，不能不設。** 如果不設，`claude` CLI 會退回去找本機是否有 `ant auth login` 登入過的憑證，然後**悄悄把請求送到真正的 Anthropic API**，跟你以為在打 OpenRouter/Qwen 完全是兩回事，而且不會報錯——這是我在 `claude-api` skill 文件裡看到的「auth trap」，這裡完整重現同一個陷阱，所以刻意用空字串把這條回退路徑堵死。

`provider="custom"` 用同一套邏輯，差別只是 `base_url`/`model` 完全由你填——這是接公司真正內部 host 的位置，**前提是那個 host 本身講 Anthropic Messages API**。如果公司內部服務只講 OpenAI 格式，那就不是改這裡的設定能解決的，中間需要一個做協定轉換的 proxy（例如 `y-router`、`claude-code-router` 這類專案），這個 config 只負責告訴 SDK「協定相容的端點在哪」，沒辦法把不相容的協定變相容。

---

## 3. 你現在該怎麼設定環境變數

```bash
cp .env.example .env
```

打開 `.env`，填這一行（其他 `LLM_*` 都已經預設指向 OpenRouter + Qwen3.5，不用動）：

```bash
LLM_API_KEY=sk-or-v1-你的OpenRouter金鑰
```

金鑰去 https://openrouter.ai/keys 拿。就這樣——`LLM_PROVIDER` 預設就是 `openrouter`，`LLM_MODEL` 預設是 `qwen/qwen3.5-plus-02-15`（我有拿 OpenRouter 的實際 model 列表查證過這個 slug 是真的存在，不是編的；便宜/快一點的選擇是 `qwen/qwen3.5-flash-02-23`，改 `LLM_MODEL` 就好）。

之後要換成公司內部 host：

```bash
LLM_PROVIDER=custom
LLM_BASE_URL=https://llm.internal.corp/anthropic   # 內部 host 的 Anthropic-compatible 端點
LLM_API_KEY=內部發的憑證
LLM_MODEL=內部 host 用的 model 名稱
```

不用改任何一行程式碼。

驗證設定有生效（不需要真的打 API）：

```bash
uv run python -c "from skr_agent.config import get_llm; print(get_llm().to_cli_env())"
```

---

## 4. A2A server——寫的時候發現的現實

我在寫這塊之前，先把 `a2a-sdk` 裝進 venv 用 `inspect` 實際查過裝到的版本（1.1.2）的 API，沒有直接照網路上的教學抄，理由是：**網路上幾乎所有 A2A 教學用的 `A2AStarletteApplication` 這個 class，在目前這個版本已經不存在了。** 現在的寫法是用一組 `create_*_routes()` 函式手動組裝到你自己的 `FastAPI` app 上。另外一個容易踩的坑：教學裡常見的 `DefaultRequestHandler` 這個 class，在這個版本改名叫 `LegacyRequestHandler`（同一個東西，只是名字換了）。

這些都是實際 import 進來查出來的，寫完後我也真的用 `TestClient` 發了一次完整的 HTTP JSON-RPC 請求（`SendMessage` method）跑過整條路徑（HTTP → JSON-RPC dispatch → `LegacyRequestHandler` → 我們的 `DeepAgentExecutor` → 一個 stub agent），確認會回 200 且內容正確，不是只有型別檢查通過。

### 用法

```bash
uv run python examples/run_service.py                 # port 8000
curl http://localhost:8000/.well-known/agent-card.json | jq
```

送任務進去（**一定要帶 `A2A-Version: 1.0` header**，這個是我實測發現的——沒帶會被拒絕，錯誤訊息是 `VERSION_NOT_SUPPORTED`，這不是文件上寫的，是實際發請求試出來的）：

```bash
curl http://localhost:8000/ \
  -H 'Content-Type: application/json' -H 'A2A-Version: 1.0' \
  -d '{"jsonrpc":"2.0","id":"1","method":"SendMessage","params":{
        "message":{"messageId":"m1","role":"ROLE_USER",
                   "parts":[{"text":"what is our exposure on the ASC-4400?"}]}}}'
```

### 這次沒做、刻意留白的兩件事

**串流 task 進度。** 現在每個請求都是「一次跑完、回一個完整結果」，A2A 協定其實支援邊跑邊回報進度（`TaskArtifactUpdateEvent`）。Report agent 一次跑好幾分鐘，UI 想要顯示進度的話值得做；純 agent 對 agent 呼叫（輪詢或直接等）目前夠用。

**真正的呼叫者身分驗證。** 目前沒有 authorizer 時，所有呼叫者都用同一個唯讀、`shared` namespace 的 default principal 執行——這是為了讓你先跑得起來，**不是給生產環境用的**，啟動時會印一行警告。要接真正的驗證，實作一個 `Authorizer.verify(token)`（介面在 `protocol.py` 裡已經有），從 A2A 訊息的 `metadata.token` 拿 token 驗證，傳給 `build_a2a_app(authorizer=...)` 就行——這條路徑本身已經接好、有測試覆蓋，只是驗證邏輯要接真的身分系統。

---

## 5. 排程——為什麼不是 Managed Agents 的 scheduled deployment

Claude API 本身有 Managed Agents 的排程功能（`deployments.create()` + cron），但那是另外一個托管平台，要另外建 agent/environment/session 資源。你要的是「這支程式自己能跑 cronjob」，所以這裡做的是**跑在同一個 process 裡的 scheduler**（`src/skr_agent/serving/scheduler.py`），沒有依賴外部排程服務。

用法：

```python
from skr_agent.serving import ScheduledJob, Scheduler
from skr_agent.principals import service_principal
from skr_agent.protocol import Budget

job = ScheduledJob(
    name="weekly-bom-sweep",
    cron="0 8 * * 1",              # 週一 08:00 UTC
    agent=mesh.report_agent,
    task="Run the weekly supply-chain incident sweep...",
    principal=service_principal,    # 每次觸發都重新產生一個 principal
    budget=Budget(max_turns=80),
)
scheduler = Scheduler([job])
await scheduler.run_forever(poll_interval=30)
```

`principal` 可以是固定值，也可以是一個零參數 callable——用 callable 是為了每次觸發都拿一份新的憑證，而不是整個 process 生命週期共用同一份。`examples/run_service.py` 預設就是這樣接的：`principal=service_principal`（傳函式本身，不是呼叫結果）。

`run_forever` 支援 `max_iterations`（測試用，跑幾輪就停）和自訂 `sleep`（測試用，可以塞假的 sleep 進去不用真的等）——`tests/test_scheduler.py` 就是這樣測整個 loop 的行為，不用真的等一分鐘。

失敗處理：一個 job 出錯不會讓其他 job 跟著不跑（`run_forever` 裡 catch 住單一 job 的例外，記 log，繼續下一個），但**同一輪內的 job 是依序執行，不是平行**——理由寫在程式碼註解裡：排程器不該是兩個 BOM 掃描互相搶著發布同一份 wiki 頁面的地方；真的要平行，該用 agent 內部的 subagent 機制（已經有），不是讓排程器同時發兩個頂層 run。

---

## 6. 三者合起來怎麼跑

```bash
uv run python examples/run_service.py --port 8000 --cron "0 8 * * 1"
```

這個 process 裡，`asyncio.gather` 同時跑兩件事：uvicorn serve A2A 請求、scheduler 輪詢排程。兩邊打的是同一個 `mesh`（同一個 wiki backend、同一個 authz），所以排程觸發跟 A2A 呼叫觸發，最終走的是同一條程式碼路徑，只差在 principal 不同——跟 `docs/design/00-architecture.md` §5 講的「同一個 agent、不同 principal」是同一件事，這裡只是多了「誰觸發」的兩種方式（HTTP 呼叫 vs. cron），不是第三種模型。

---

## 7. 已知限制

1. **A2A 身分驗證是佔位。** 見 §4。
2. **A2A 沒有串流進度。** 見 §4。
3. **排程 job 目前寫死在 `serving/service.py::default_jobs()`。** 只有一個範例 job（全 BOM 掃描）。要多個 job、要從設定檔案讀 job 清單，這裡沒有做——現在的形狀是「程式碼定義 job」，不是「設定驅動 job」，多一個 job 就是多寫一個 `ScheduledJob(...)`。
4. **`InMemoryTaskStore` 會在 process 重啟時掉光所有 task 歷史。** a2a-sdk 有其他 `TaskStore` 實作（例如 database-backed 的），真要上線需要換掉；目前用記憶體版本是為了先跑得起來。
5. **`a2a-sdk` 版本沒有鎖死到 patch 版本。** 這次的實作是照 1.1.2 的 API 寫的，這個套件顯然還在快速變動（連 `DefaultRequestHandler` 都改名成 `LegacyRequestHandler` 了），建議在 `pyproject.toml` 裡把版本釘死到目前測過的版本，不要讓它自動升級跳掉。
