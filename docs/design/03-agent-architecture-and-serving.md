# skr agent 框架設計——執行框架、A2A serving、排程、跨 agent 溝通

狀態：實作完成
範圍：`protocol.py`、`mesh.py`、`runtime.py`、`report/`、`serving/`

**這份文件講 skr agent（`report/agent.py`）本身的框架**：它跑在什麼執行框架上、怎麼被 serve 成 A2A server、排程怎麼跑、外部怎麼跟它溝通。它的資料來源和授權模型在 [`00-architecture.md`](00-architecture.md)，設定層在 [`01-config.md`](01-config.md)。

skr agent 是一個 deep research agent：它接開放式問題，自己決定要挖多深，跨多個資料來源交叉比對，最後產出有出處的報告。**BOM、外部新聞、內部 wiki 是它的三個資料來源，地位相同**——wiki 只是其中一個（它另外有授權模型，所以程式碼上獨立成一個模組，但那是權限的關係，不是它比較重要）。

---

## 0. 決策摘要

| 問題 | 決定 | 一句話理由 |
|---|---|---|
| 執行框架用什麼？ | **LangChain `deepagents`**（LangGraph 之上的 deep agent harness） | 內建 planning/todo、context 摘要、subagent 委派、virtual filesystem，這些自己寫都要錢——見 §2 |
| 模型怎麼選？ | LangChain `BaseChatModel`，由 `config/llm.py` 建 | 換成 LangChain 之後不再綁特定 wire protocol，內部 gateway 只要有 OpenAI-compatible endpoint 就能接——見 §2.2 |
| Skill（報告規範）怎麼載入？ | **直接 inline 進 system prompt**，不用 `create_deep_agent(skills=...)` | 那個參數是 progressive disclosure（模型自己決定要不要 `read_file` 去讀），對「每次都必須遵守的規範」是錯的取捨——見 §2.3 |
| 外部怎麼呼叫 skr agent？ | 兩種機制，依「是否跨 process」二選一 | 同 process 用 `agent_as_tool`；跨 process 用 A2A——見 §3 |
| A2A SDK 用哪個版本？ | **`a2a-sdk` 0.3.x**（釘死，不是最新的 1.x） | 對齊團隊自己 reference server 用的世代；1.x 幾乎全部改名且不相容——見 §4 |
| 排程怎麼實作？ | 自寫的 in-process `Scheduler` | 排程要跟 A2A server 共用同一份資料來源連線與 principal 邏輯，接第二個代管平台換不到什麼——見 §5 |
| A2A server 跟排程是不是分開部署？ | 不是，同一個 process、同一個 `asyncio.gather` | 兩者都只是「誰觸發 `skr_agent.run()`」的不同入口——見 §6 |

---

## 1. skr agent 的組成

```
                              外部呼叫方
                （另一個 process 的 agent／client／使用者）
                                   │
                   ┌───────────────┴────────────────┐
                   │                                  │
       同 process：包成 tool                跨 process：A2A
       （mesh.agent_as_tool）              （serving/a2a.py）
                   │                                  │
                   ▼                                  ▼
        ┌────────────────────────────────────────────────────────┐
        │                 skr agent（report/agent.py）              │
        │        runtime.DeepAgent → deepagents.create_deep_agent   │
        │                                                           │
        │  資料來源（地位相同的三個）：                                  │
        │   ├─ BOM        list_bom_companies / get_bom_company       │
        │   ├─ 外部新聞    search_news / fetch_article                 │
        │   └─ 內部 wiki   wiki_search / wiki_read_page /             │
        │                 wiki_write_page   ← 唯一有授權模型的來源       │
        │                                                           │
        │  framework 內建：planning/todo、context 摘要、               │
        │                virtual filesystem、task（subagent 委派）     │
        │  inline 的規範：.claude/skills/incident-report/SKILL.md      │
        │  subagent：company-investigator（唯讀，拿不到 write tool）    │
        └───────────────────────┬───────────────────────────────────┘
                                 │
                    ┌────────────┴────────────┐
                    ▼                          ▼
        ┌─────────────────────┐   ┌─────────────────────────┐
        │  serving/a2a.py       │   │  serving/scheduler.py     │
        │  DeepAgentExecutor    │   │  cron 觸發                │
        │  ──▶ A2A 呼叫方         │   │  (service_principal)      │
        └─────────────────────┘   └─────────────────────────┘
                    同一個 process（serving/service.py）
```

三層職責：

1. **`protocol.py`** —— 契約層。`Principal` / `AgentRequest` / `AgentResponse` / `AgentSpec` / `Budget` / `Citation`。**不 import 任何 agent framework**。這一層是這次從 Claude Agent SDK 換成 deepagents 時，唯一一行都沒改的東西——A2A serving、排程、授權模型因此全部原封不動地沿用。這不是巧合，是當初刻意把 framework 擋在契約外面換來的。
2. **`runtime.py`** —— 執行殼。`DeepAgent` 把 `create_deep_agent` 包成一個吃 `AgentRequest`、吐 `AgentResponse` 的東西。
3. **`serving/`** —— skr agent 怎麼被外部觸發到。

---

## 2. 執行框架：LangChain deepagents

### 2.1 為什麼是 deepagents

`deepagents` 是 LangChain 在 LangGraph 之上做的 deep agent harness。選它的實際理由是它內建了一個 deep research agent 本來就需要、自己寫又很花時間的東西：

- **planning / todo 狀態**——掃 20 家公司時不會默默漏掉幾家。
- **context 摘要（`SummarizationMiddleware`）**——長時間研究一定會撐爆 context，這件事自己做很煩。
- **subagent 委派（`task` tool）**——每家公司一個獨立 context window 去查，主 agent 只收結論，這是 deep research 最實際的省 context 手段。
- **virtual filesystem**——研究過程的中間筆記有地方放，不必全塞在對話裡。

`DeepAgent`（`runtime.py`）刻意只是薄薄一層殼：把 `AgentRequest` 轉成 graph 的輸入、把 graph 的輸出轉回 `AgentResponse`、把 principal 綁進 tools。framework 提供的東西一個都沒有重做。

### 2.2 模型怎麼接——換框架換掉的一個實際限制

之前跑在 Claude Agent SDK 上時，模型端有一個硬限制：**SDK 只講 Anthropic Messages wire protocol**，所以任何要接的 endpoint 都必須也講那個協定。OpenRouter 剛好在 `/api` 提供 Anthropic-compatible endpoint（不是 `/api/v1`，那是 OpenAI-compatible 的），所以才能拿來模擬內部 host；如果公司內部 gateway 只講 OpenAI 格式，就得在前面架一個翻譯 proxy。

**換到 LangChain 之後這個限制沒了。** 模型是一個 `BaseChatModel`，protocol 由 client 決定，所以：

```python
# config/llm.py::LLM.build_chat_model()
provider = "openrouter"  # → ChatOpenAI(base_url="https://openrouter.ai/api/v1")
provider = "custom"      # → ChatOpenAI(base_url=<你的內部 gateway>/v1)
provider = "anthropic"   # → ChatAnthropic
```

`provider="custom"` 現在指向的是 OpenAI-compatible 的 `/v1/chat/completions`——**這正是絕大多數內部 LLM gateway（vLLM、LiteLLM、公司 proxy）本來就會開的介面**，不用翻譯層。這是這次換框架在「搬進公司環境」這件事上換到的實際好處。

另一個順帶修掉的坑：以前 `ANTHROPIC_API_KEY` 沒設會**默默** fallback 到機器上登入過的 `claude` session 憑證，把請求送去真的 Anthropic API 而不是你設定的 endpoint（所以舊 config 要刻意送空字串把它堵掉）。現在沒設 key 的話 provider client 直接在**建構時**就報錯——問題在 agent 建起來的當下就炸，不是跑到一半才發現連錯地方。

### 2.3 Skill 為什麼是 inline，不是用 framework 的 `skills=`

`create_deep_agent` 有一個 `skills=` 參數，吃 SKILL.md 目錄，格式跟 `.claude/skills/` 完全相容。但**它的載入機制是 progressive disclosure**：system prompt 裡只放 skill 的名字和描述，模型要自己判斷「這個 skill 跟現在的任務有關」然後呼叫 `read_file` 去把內容讀出來。

這是我當初排除 nanobot 時，用來反對它的同一個理由——「靠模型記得去讀檔」少了一層確定性。所以照著自己的判斷走：

`.claude/skills/incident-report/SKILL.md`（報告格式 + 嚴重度分級規則）是 skr agent **每次跑都必須遵守**的規範，不是「碰巧有用就查」的參考資料。所以 `runtime.py::load_skill()` 直接把檔案內容讀出來（去掉 YAML frontmatter，那是給 catalog 用的 metadata，inline 進 prompt 只是浪費 token）接到 system prompt 後面，模型必定看得到。

**這不代表 `skills=` 是錯的設計**——等到 skill 有十幾二十個、大部分情況下只有一兩個相關時，progressive disclosure 才是對的取捨（不然每次都把全部規範塞進 prompt）。現在只有一份、而且是必要的，所以 inline 是對的。這個判斷會隨 skill 數量改變，寫在這裡是為了之後有人要加第二、第三個 skill 時知道界線在哪。

---

## 3. 呼叫方怎麼跟 skr agent 溝通

兩種機制，判斷依據是**呼叫方跟 skr agent 是不是同一個 process**。

### 3.1 同 process——`agent_as_tool`

`mesh.py::agent_as_tool`（`mesh.py:100`）把 skr agent 的 `AgentSpec` 包成一個 LangChain tool，讓同 process 裡任何一個模型可以直接把它當工具呼叫。

- **Principal 用 closure 綁定，不是 tool 參數。** 建 `AgentRequest` 時 `principal=principal` 讀的是外層函式參數，不是模型傳進來的 args——呼叫方的模型**沒有辦法**在 tool call 裡寫一個不同的 division 來冒充別人，因為 schema 裡根本沒這個欄位。這是整個 mesh 唯一的身份傳遞保證。（實測：即使呼叫方硬塞 `{"division": "finance"}`，那些欄位只會落進 `request.inputs`，principal 完全不受影響——`tests/test_mesh.py` 有守這件事。）
- **`Denied` 在這一層轉成正常回應，不是例外炸開。** 呼叫方看到的是一段說明「為什麼被拒絕」的文字，可以據此回應使用者。
- **Citation 會往上傳。** `on_response` callback 讓呼叫方把 skr agent 回應裡的 `citations` 收進自己的 trace，不會因為多一層委派就掉了出處。

### 3.2 跨 process——A2A protocol

skr agent 同時被 serve 成一個獨立的 A2A server（`serving/a2a.py`），讓不在同一個 process 的東西（另一個團隊的 agent、外部 orchestrator）可以呼叫它。細節見 §4。

### 3.3 兩者怎麼選

| | 3.1 in-process | 3.2 A2A |
|---|---|---|
| 關係 | 同一個 Python process | 不同 process，可能不同機器 |
| 身份怎麼傳 | `Principal` 物件直接傳（closure 綁定，永遠可信） | HTTP 帶 token，`Authorizer.verify()` 在邊界重新驗證 |
| 傳輸開銷 | 無（函式呼叫） | HTTP + JSON-RPC/REST 序列化 |
| 呼叫方是誰 | 一定是另一個模型的 tool call | 任何會講 A2A 的 client（模型或非模型） |

**不要為了「以防萬一要跨 process」而讓 in-process 呼叫也走 HTTP**——`agent_as_tool` 的 closure 身份綁定，是 HTTP 版本必須用 token 重新驗證才能達到的等價保證，同 process 內繞這一圈沒有安全性上的好處，只有延遲上的壞處。

---

## 4. 怎麼被 A2A serve

`serving/a2a.py` 把 skr agent 包成一個講 A2A（JSON-RPC + SSE）的 Starlette app：

```python
def build_a2a_app(
    agent: DeepAgent,
    *,
    url: str,
    registry: AgentRegistry | None = None,
    authorizer: Authorizer | None = None,
    default_principal: Principal | None = None,
    version: str = "0.1.0",
) -> A2AStarletteApplication: ...
```

**這一層在換框架時完全沒動**——它吃的是 `DeepAgent`，而 `DeepAgent` 的對外介面（`run(AgentRequest) -> AgentResponse`）沒變，底下換成 LangGraph 對它不可見。

**版本對齊——這是刻意的 pin，不是隨手挑的**：用 `a2a-sdk` **0.3.x**，因為那是團隊自己那份 reference server 用的世代（`A2AStarletteApplication` + `DefaultRequestHandler` + `TaskState.completed`）。1.x 把這些幾乎全部改名（`LegacyRequestHandler`、routes-based 接線、protobuf 訊息型別、強制 `A2A-Version: 1.0` header），**兩者不相容**。既然這份程式碼的目的就是要搬進公司環境，跟團隊同一個世代比跟最新版同步重要。

降版順帶消掉兩個只有 1.x 才有的問題：強制的 `A2A-Version` header，以及 metadata 的 protobuf `Struct` 轉換坑（0.3.x 的 `message.metadata` 就是一個普通的 `dict`）。

**接線方式**：

- `A2AStarletteApplication(agent_card=..., http_handler=DefaultRequestHandler(agent_executor=..., task_store=InMemoryTaskStore()))`
- `build_a2a_app()` 回傳的是 `A2AStarletteApplication` 而不是建好的 app，這樣呼叫端可以自己傳 `lifespan` 給 `.build()`——同時要跑排程的那個 process 就是這樣把兩者兜起來的

**一次請求的生命週期**（`DeepAgentExecutor.execute`）：

```
message/send 或 message/stream 進來
  → 沒有輸入文字 → 直接 failed，不叫 agent
  → resolve_principal(metadata, call_context)
      Denied → final failed event，內容是拒絕原因，不是 500
  → async for event in agent.stream(request):
        progress → TaskStatusUpdateEvent(final=False, state=working)   ← 邊跑邊送
        AgentResponse → 收下來當最終結果
  → 最終答案拆成 text / file artifact（見下方）並送出
  → final event: completed（response.ok）或 failed
```

**Streaming——為什麼要做**：一次 BOM sweep 要跑幾分鐘。之前的實作是跑完才一次性回一則訊息，呼叫方在那幾分鐘裡看到的是一條沒有任何動靜的連線。現在 `DeepAgent.stream()` 用 LangGraph 的 `astream(stream_mode="updates")` 把每一步吐出來，executor 轉成非 final 的 `working` event。

**progress 內容只講「跑了哪個 tool」，不回傳 tool 的輸出**（`runtime.py::_progress_note`）——這是刻意的：tool 的回傳內容經常包含呼叫方沒有權限看的東西，progress feed 不該成為繞過 tool 層授權的側漏管道。

`run()` 沒有改成用 `stream()` 實作：排程和 in-process 呼叫方沒有地方放 progress event，為了丟掉它而多付一份簿記沒有意義。

**檔案產出——`<render-cpochat />` 慣例**：平台用這個 tag 在對話裡呈現檔案。agent 在答案裡寫 `<render-cpochat src="..." name="..." desc="..." />`，executor 會把最終答案照 tag 切開，tag 變成 A2A file artifact（base64 + mime type），其餘文字照原本順序保留——所以報告裡「先放圖再解釋」的順序不會被打亂。檔案不存在時只記一行 warning 跳過，不會讓整個 task 失敗。

`context.task_id` 直接拿來當 `AgentRequest.trace_id`——A2A 的 task 追蹤跟內部 trace 是同一個 id，方便事後對日誌。

**身份怎麼決定**（`_default_principal_resolver`）：

- 沒配 `authorizer`：每個呼叫方都用 `default_principal`（`subject="a2a:anonymous"`、`division="shared"`、只有 `wiki.reader`）——安全的預設值，但代表**目前這個 server 沒有真的驗證外部呼叫方是誰**。刻意留白，部署前要補。
- 配了 `authorizer`：讀 `message.metadata["token"]` 呼叫 `authorizer.verify(token)`。沒帶 token 或驗證失敗直接 `Denied`，**不會**退回 `default_principal`——否則 authorizer 形同虛設。

**刻意不做的一件事：真的驗證呼叫方身份。** `PrincipalResolver` 就是那個縫，見上面的身份解析說明。

---

## 5. 排程怎麼跑

`serving/scheduler.py`，in-process 的 cron-like scheduler，跟 A2A 一樣**沒有因為換框架而改動**：

```python
@dataclass
class ScheduledJob:
    name: str
    cron: str                                       # 標準 5 欄 cron
    agent: DeepAgent
    task: str | Callable[[], str]
    principal: Principal | Callable[[], Principal]  # callable = 每次觸發現拿一個新的
    inputs: dict | Callable[[], dict] = field(default_factory=dict)
    budget: Budget = field(default_factory=lambda: Budget(max_turns=60))
    timezone: dt.tzinfo = dt.timezone.utc

class Scheduler:
    def due_jobs(self, now) -> list[ScheduledJob]: ...   # 純函式，不改狀態
    async def run_job(self, job, *, now=None) -> AgentResponse: ...  # 立即跑，會推進排程
    async def run_forever(self, *, poll_interval=30.0, max_iterations=None, sleep=asyncio.sleep): ...
```

**內建的 job**（`service.py::default_jobs`）：`weekly-bom-sweep`，`cron="0 8 * * 1"`（週一 08:00 UTC），`principal=service_principal`（**傳函式本身，不是呼叫結果**——每次觸發現拿一個新的，不是整個 process 生命週期共用一個）。

**跟 A2A 呼叫共用同一套授權邏輯**——排程觸發的 `AgentRequest` 跟 A2A 觸發的走同一個 `DeepAgent.run()`，差別只在 `Principal` 是哪一種。這是「定期報告 vs 使用者觸發報告：同一個 agent，不同 principal」在排程側的實作（`00-architecture.md` §5）。

**為什麼是自己寫的 scheduler，不是接一個代管排程平台**：這個排程本來就該跟 A2A server 用同一份資料來源連線、同一套 principal 邏輯，不是各自獨立的兩份系統。**這是刻意取捨**：如果之後某個 job 大到需要自己的 scaling、或需要在 process 重啟後接續（目前完全沒持久化），那才是換平台的時間點。

**三個容易誤會的執行語意**：

1. **`due_jobs()` 純函式，`run_job()` 才推進排程**——測試可以反覆呼叫 `due_jobs()` 檢查邏輯而沒有副作用。
2. **Job 依序執行，不是平行**——「排程系統不該去發現兩個 BOM 掃描同時搶著發布同一頁而互撞」是設計動機。單一 job 內部要平行，用 skr agent 自己的 subagent 委派做，不是靠排程主迴圈同時起兩個 top-level run。
3. **單一 job 失敗不影響其他 job**——`run_forever()` 的迴圈用 `try/except: continue` 接住。

---

## 6. 目前的部署形態：一個 process，兩個入口

`serving/service.py::run()`：

```python
mesh = build_mesh(fixtures=..., project_root=...)
a2a_app = build_a2a_app(mesh.report_agent, url=..., registry=mesh.registry)
server = uvicorn.Server(uvicorn.Config(a2a_app.build(), host=host, port=port, ...))
scheduler = Scheduler(default_jobs(mesh, cron=cron))

await asyncio.gather(
    server.serve(),
    scheduler.run_forever(poll_interval=scheduler_poll_interval),
)
```

一個 `asyncio` event loop，一個 skr agent 實例，兩件事掛在上面：`uvicorn` 服務 inbound 的 A2A 呼叫，`Scheduler.run_forever()` 輪詢並在時間到時觸發排程。**兩者是同一個 agent 實例、同一份資料來源連線**——排程寫入的東西馬上就能透過 A2A 讀到，不需要處理兩個獨立部署之間的資料一致性。這是把兩者放進同一個 process 最主要換來的東西，不只是省一個部署單位。

---

## 7. 換框架時實際驗證過什麼

測試套件（133 個，全部不需要金鑰）測的是接縫，不是模型。所以換框架後另外用一個**本地假 OpenAI-compatible server**（回一次 tool call、再回一次最終答案）把整條鏈路跑過一遍，確認：

- tool call 真的被 dispatch、tool 真的執行、最終答案真的被抽出來（`AIMessage` 內容可能是字串也可能是 typed block 陣列，兩種都要處理，見 `runtime.py::_message_text`）
- citation 跨 tool 累積並出現在 `AgentResponse.citations`
- token usage 有被加總
- **授權邊界仍然成立**：同一個問題，`supply` 使用者拿得到 `supply/acme-semiconductor` 和它的 raw report citations，`finance` 使用者拿到空的——授權是在 tool 層擋的，換 framework 不影響
- A2A `message/send` 回 `completed`；`message/stream` 透過 SSE 在最終 event 之前先送出遞增的 `working` event
- `<render-cpochat />` tag 正確變成 base64 file artifact，mime type 對
- 排程觸發一次 job，正確推進到下一次 fire time

---

## 8. 已知限制

1. **A2A 呼叫方預設沒有真的身份驗證。** `Authorizer` 是留好的縫，沒配之前每個外部呼叫方都是同一個唯讀匿名身份。對外開放前必須先接一個會驗 bearer token 的實作。
2. **排程狀態不持久化。** `Scheduler._next` 只在記憶體，process 重啟後重算，不會補跑錯過的觸發。weekly sweep 影響不大，跑更頻繁、對「不能漏跑」有要求的 job 之前要先處理。
3. **`cost_usd` 永遠是 0。** 舊 framework 的 `ResultMessage` 會回報成本，LangChain 的 `usage_metadata` 只有 token 數。要算成本得自己維護一份各 provider 的價目表——與其塞一個會過期的錯數字，不如先誠實回報 0，token 數是準的。
4. **deepagents 內建的 filesystem / `execute` tools 沒有明確限制範圍。** 目前用預設的 `StateBackend`（虛擬、記憶體內，不碰真實磁碟），所以 agent 的檔案操作是沙盒化的；但這是靠「沒去改預設值」得到的，不是明確設定的。如果之後有人為了讓 skill 從磁碟載入而改成 `FilesystemBackend`，agent 就會取得專案目錄的實際讀寫權限——那時候要一併設 `permissions=[FilesystemPermission(...)]`，不能只換 backend。
5. **多層委派（A→B→C）沒驗證過。** `agent_as_tool` 的 budget 傳遞、trace 追蹤、citation 彙總目前只被單層委派驗證過。
