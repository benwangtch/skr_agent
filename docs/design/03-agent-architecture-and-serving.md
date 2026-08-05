# skr agent（deep research agent）框架設計——執行框架、A2A serving、排程、跨 agent 溝通

狀態：實作完成
範圍：`protocol.py`、`mesh.py`、`runtime.py`、`report/`、`serving/`

**這份文件只講 skr agent（`report/agent.py` 裡的 deep research agent，程式裡的 `wiki_report`）本身的框架**：它跑在什麼執行框架上、怎麼被 serve 成 A2A server、排程怎麼跑、它跟外部呼叫方之間怎麼溝通。copilot（FE 對話用的路由 agent）不在這份文件的討論範圍——那是另一個消費 skr agent 的角色，不是 skr agent 框架本身的一部分。跟 `00-architecture.md`（wiki 該不該是 agent、principal 怎麼分權限）和 `01-config-and-serving.md`（config/A2A/排程「怎麼用」的操作細節、實測出來的 API 限制）不重複——這裡是把分散在那兩份文件裡、跟「skr agent 這個框架怎麼組成」有關的決定收攏成一份。

---

## 0. 決策摘要

| 問題 | 決定 | 一句話理由 |
|---|---|---|
| 執行框架用什麼？ | **Claude Agent SDK**（不是 nanobot） | SKILL.md 載入是 harness 保證注入，不是靠模型自己去讀檔——見 §2 |
| skr agent 怎麼被外部呼叫？ | 兩種機制，依「呼叫方是否跟 skr agent 同一個 process」二選一 | 同 process 內用 `agent_as_tool` 把它包成一個 tool；跨 process/外部用 A2A——見 §3 |
| skr agent 怎麼暴露給 mesh 外部？ | serve 成一個 A2A server（`serving/a2a.py`） | 讓不在同一個 process 裡的呼叫方（另一個團隊的 agent、外部 orchestrator）也能觸發它——見 §4 |
| 排程怎麼實作？ | 自寫的 in-process `Scheduler`，不是 Claude Managed Agents 的 scheduled deployment | 排程要跟 A2A server 共用同一份 wiki backend、同一套 principal 邏輯，接第二個代管平台換不到什麼——見 §5 |
| A2A server 跟排程是不是分開部署？ | 不是，同一個 process、同一個 `asyncio.gather` | 兩者都只是「誰觸發 `report_agent.run()`」的不同入口，底下是同一個 agent 實例——見 §6 |

---

## 1. skr agent 本身的組成

```
                                外部呼叫方
                (另一個 process 裡的 agent／client／使用者)
                                    │
                    ┌───────────────┴────────────────┐
                    │                                  │
        同 process 內直接呼叫                跨 process：A2A
        （被包成 agent_as_tool）           （serving/a2a.py）
                    │                                  │
                    ▼                                  ▼
        ┌─────────────────────────────────────────────────────┐
        │              skr agent（wiki_report）                  │
        │              report/agent.py，DeepAgent, effort=high    │
        │                                                        │
        │   toolsets:                                            │
        │   - wiki toolset (wiki/tools.py)  wiki_search /         │
        │     wiki_read_page / wiki_write_page ——授權在工具層      │
        │   - BOM/news tools (report/sources.py, report/tools.py) │
        │   - .claude/skills/wiki-report/SKILL.md                 │
        │     （報告格式 + 嚴重度分級規則，見 §2）                    │
        └───────────────────────┬────────────────────────────────┘
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

三層職責，跟 `00-architecture.md` §8「可復用三層」對得上，這裡只講跟 skr agent 直接相關的部分：

1. **`protocol.py`** —— 契約層。`Principal` / `AgentRequest` / `AgentResponse` / `AgentSpec` / `Budget` / `Citation`。不 import Claude 的任何東西——skr agent 對外的輸入輸出形狀，跟它底下用哪個執行框架無關。
2. **`runtime.py`** —— 執行殼。`DeepAgent` 把 Claude Agent SDK 的 `query()` 包成一個吃 `AgentRequest`、吐 `AgentResponse` 的東西，config-driven（模型/effort/env 走 `config.get_llm()`）。skr agent 是這層唯一目前實際在跑的產品化實例。
3. **`serving/`** —— skr agent 怎麼被外部觸發到。這份文件主要在講這一層。

---

## 2. 執行框架：為什麼是 Claude Agent SDK

skr agent 的執行殼是 `runtime.py::DeepAgent`，底層是 `claude_agent_sdk.query()`。

決定的過程（`00-architecture.md` §6 有完整記錄）：一開始有考慮 nanobot，因為它宣稱支援 Claude Skill（`SKILL.md`）格式，理論上可以讓 `.claude/skills/wiki-report/SKILL.md` 這份技能定義在兩個框架間共用。實際去讀 nanobot 原始碼（不是只看 README）後發現：**檔案格式相容，但載入機制不同**——

- **Claude Agent SDK**：skill 的 metadata（name/description/allowed-tools）由 harness 在啟動時解析並注入 system prompt，agent 一定會「知道」有哪些 skill 可用，不需要自己先去找檔案。
- **nanobot**：SKILL.md 的載入是「model 自己決定要不要去讀那個檔案」——沒有 harness 保證的注入，少了一層確定性。

`.claude/skills/wiki-report/SKILL.md`（報告格式 + 嚴重度分級規則）是 skr agent 每次跑都必須遵守的規則，不是「碰巧有用就查」的參考資料，所以選了保證注入的那個。這是唯一影響框架選擇的技術差異——`DeepAgent` 本來就是為了讓底層可換而寫的一層殼（見 `runtime.py` 開頭的設計說明），如果之後有更好的理由換框架，殼本身不用重寫。

**具體落地**：`DeepAgent.__init__` 吃 `name` / `description` / `system_prompt` / `toolsets` / `model` / `effort` / `max_turns`；`toolsets` 是 `Callable[[ToolContext], ToolBundle]` 的列表，`ToolContext` 帶著呼叫者的 `Principal`，讓每個 toolset 在建構時就把授權綁死（`ToolContext.principal`），不是在 tool call 參數裡帶身份——skr agent 掛的 wiki toolset 就是這樣拿到「這次是誰在跑」的授權範圍。這跟 §3.1 `agent_as_tool` 用同一個「principal 在 closure 裡，不在 schema 裡」的手法，是整個 mesh 唯一的身份傳遞方式。

---

## 3. 呼叫方怎麼跟 skr agent 溝通

外部想觸發 skr agent 有兩種機制，判斷依據是**呼叫方跟 skr agent 是不是同一個 process**。

### 3.1 同 process（in-process）——`agent_as_tool` / `agents_as_toolserver`

`mesh.py::agent_as_tool`（`mesh.py:100`）把 skr agent 的 `AgentSpec` 包成一個 SDK tool，讓同一個 process 裡任何一個模型（例如某個路由層 agent）可以直接把它當工具呼叫；`agents_as_toolserver`（`mesh.py:146`）是批次版本，一次把 `AgentRegistry` 裡的多個 `AgentSpec` 包成一個 in-process MCP server。

```python
def agent_as_tool(
    spec: AgentSpec,
    *,
    principal: Principal,
    parent: str,
    max_turns: int | None = None,
    on_response: Callable[[AgentResponse], None] | None = None,
): ...
```

- **Principal 用 closure 綁定，不是 tool 參數。** `_invoke(args)` 建 `AgentRequest` 時，`principal=principal` 讀的是外層函式參數，不是 `args` 字典裡的欄位——呼叫方的模型**沒有辦法**在 tool call 裡寫一個不同的 division 來冒充別人，因為 schema 裡根本沒這個欄位可以填。這是整個 mesh 唯一的身份傳遞保證，`00-architecture.md` §5 的「同一個 agent、不同 principal」模型能成立，靠的就是這一點。
- **`Denied` 在這一層被轉成正常回應，不是例外炸開。** `except Denied as exc: response = AgentResponse.refuse(...)`——呼叫方看到的是一段文字說明「為什麼被拒絕」，可以據此回應，而不是一個未處理的例外。
- **Citation 會往上傳。** `on_response` callback 讓呼叫方從 skr agent 的回應裡把 `citations` 收進自己的 trace，不會因為多一層委派就掉了引用來源——這是 `Citation` 契約（`protocol.py:182`）能跨 agent 存活的唯一原因。

適用時機：**呼叫方跟 skr agent 在同一個 Python process，而且呼叫方本身也是一個模型（需要工具呼叫的語意)。**

### 3.2 跨 process / 外部——A2A protocol

skr agent 也被 serve 成一個獨立的 A2A server（`serving/a2a.py`），讓**不在同一個 process** 的東西（另一個團隊的 agent、一個外部 orchestrator、甚至另一份部署的這個系統）可以呼叫它，細節見 §4。

適用時機：**呼叫方是另一個 process(可能是另一個語言、另一個團隊維護、根本不在同一台機器)。**

### 3.3 兩者怎麼選

| | 3.1 in-process | 3.2 A2A |
|---|---|---|
| 呼叫方與 skr agent 的關係 | 同一個 Python process | 不同 process，可能不同機器 |
| 身份怎麼傳 | `Principal` 物件直接傳（closure 綁定，永遠可信） | HTTP request 帶 token，`Authorizer.verify()` 在邊界重新驗證（§4） |
| 傳輸開銷 | 無（函式呼叫） | HTTP + JSON-RPC/REST 序列化 |
| 呼叫方是誰 | 一定是另一個模型的 tool call | 可以是任何會講 A2A 的 client（模型或非模型） |

新增一個會呼叫 skr agent 的呼叫方時，先問「它會跟 skr agent 在同一個 process 裡跑嗎」——會的話走 3.1（把 skr agent 的 `AgentSpec` 註冊進呼叫方能看到的 `AgentRegistry`），不會的話走 3.2（透過已經跑起來的 A2A endpoint）。**不要為了「以防萬一要跨 process」而讓 in-process 呼叫也走 HTTP**——`agent_as_tool` 的身份保證（closure 綁定)是 HTTP 版本必須用 token 重新驗證才能達到的等價物，同 process 內繞這一圈沒有安全性上的好處，只有延遲上的壞處。

---

## 4. 怎麼被 A2A serve

`serving/a2a.py` 把 skr agent（一個 `DeepAgent`）包成一個講 A2A（JSON-RPC + REST）protocol 的 FastAPI app。核心是 `build_a2a_app()`：

```python
def build_a2a_app(
    agent: DeepAgent,
    *,
    base_url: str,
    registry: AgentRegistry | None = None,
    authorizer: Authorizer | None = None,
    default_principal: Principal | None = None,
) -> FastAPI: ...
```

**接線方式**（`a2a-sdk` 1.1.x 的實際 API，不是網路上教學常見的 `A2AStarletteApplication`——那個 class 這個版本沒有，是直接 import 套件用 `inspect.signature()` 逐一核對出來的，見 `01-config-and-serving.md` §4）：

- `LegacyRequestHandler`（不是文件常見的 `DefaultRequestHandler`）+ `InMemoryTaskStore` + `agent_card`
- `create_agent_card_routes()` / `create_jsonrpc_routes()` / `create_rest_routes()` 產生路由，`add_a2a_routes_to_fastapi()` 掛進既有的 FastAPI app

**一次請求的生命週期**（`DeepAgentExecutor.execute`，`a2a.py:170-212`）：

```
SendMessage 進來
  → TaskUpdater.submit()          任務登記
  → TaskUpdater.start_work()      標記開始處理
  → resolve_principal(metadata, call_context)   見下方「身份怎麼決定」
      成功 → 建 AgentRequest → agent.run(request)
      Denied → updater.failed(...)，回應內容是拒絕原因，不是 500
  → response.ok?
      是 → updater.complete(...)
      否 → updater.failed(...)
```

`context.task_id` 直接拿來當 `AgentRequest.trace_id`——A2A 的 task 追蹤跟內部的 trace 是同一個 id，方便事後對日誌。

**身份怎麼決定**（`_default_principal_resolver`，`a2a.py:71-97`)——這是 A2A 呼叫跟 §3.1 in-process 呼叫的關鍵差異：

- 沒有配 `authorizer`：每個呼叫方都用 `default_principal` 跑（預設 `subject="a2a:anonymous"`、`division="shared"`、只有 `wiki.reader`）——夠安全的預設值，但也代表**目前這個 server 沒有真的驗證外部呼叫方是誰**。這是刻意留白但要在部署前補上的東西，不是遺漏——見本節最後的已知限制。
- 配了 `authorizer`：讀 `message.metadata["token"]`，呼叫 `authorizer.verify(token)`。沒帶 token 或驗證失敗直接 `Denied`，**不會**退回 `default_principal`——否則 authorizer 形同虛設。

**Agent card 探索**：`build_agent_card()` 產生 `/.well-known/agent-card.json`，公開 skr agent 的 name/description，有給 `registry` 的話也一併列出 registry 裡其他已註冊的 `AgentSpec` 作為可發現的 skill。

**協定版本限制（實測出來，非文件記載）**：每個請求都必須帶 `A2A-Version: 1.0` header，沒帶會被拒絕（`VERSION_NOT_SUPPORTED`），`enable_v0_3_compat=True` 這個 flag **不會**放寬這個限制——這是直接發沒帶 header 的請求測出來的行為，原本以為這個 flag 會讓舊版 client 通過，結果不會，已經把這個誤解在 `a2a.py:253-258` 的註解裡改正。細節與 curl 範例見 `01-config-and-serving.md` §4 和 `RUNBOOK.md` §3.6。

**刻意不做的兩件事**（`a2a.py` 模組開頭 docstring）：

1. **Streaming task events。** 現在每次呼叫都是跑完整個 agent 才一次性回一則訊息，A2A 支援的 `TaskArtifactUpdateEvent` 增量更新沒接——要接的話 `DeepAgent.run()` 要先能吐出部分輸出，目前沒有這個能力。多分鐘的報告生成想在 UI 上顯示進度前，這個要先做；peer agent 對 peer agent 的呼叫通常 polling 或直接等就夠。
2. **真的驗證呼叫方身份。** `PrincipalResolver` 就是這個縫——串一個真的會驗 bearer token 的 `Authorizer`(見 `protocol.py::Authorizer`)。沒配之前每個外部呼叫方都是同一個唯讀、`shared` namespace 的匿名身份，足夠安全到不會意外洩漏東西，但也代表現在無法區分「哪個外部呼叫方在呼叫我」。

---

## 5. 排程怎麼跑

`serving/scheduler.py` 是一個 in-process 的 cron-like scheduler，核心兩個型別：

```python
@dataclass
class ScheduledJob:
    name: str
    cron: str                                     # 標準 5 欄 cron，如 "0 8 * * 1"
    agent: DeepAgent
    task: str | Callable[[], str]
    principal: Principal | Callable[[], Principal]  # 可傳 callable，每次觸發現拿一個新的
    inputs: dict[str, Any] | Callable[[], dict[str, Any]] = field(default_factory=dict)
    budget: Budget = field(default_factory=lambda: Budget(max_turns=60))
    timezone: dt.tzinfo = dt.timezone.utc

class Scheduler:
    def __init__(self, jobs: list[ScheduledJob], *, on_result: ResultHook | None = None): ...
    def due_jobs(self, now: dt.datetime) -> list[ScheduledJob]: ...       # 純函式，不改狀態
    async def run_job(self, job, *, now=None) -> AgentResponse: ...       # 立即跑一個 job，會推進排程
    async def run_forever(self, *, poll_interval=30.0, max_iterations=None, sleep=asyncio.sleep) -> None: ...
```

**這個系統唯一內建的 job**（`service.py::default_jobs`）：`weekly-bom-sweep`，`cron="0 8 * * 1"`（週一 08:00 UTC，可覆寫），`agent=mesh.report_agent`（也就是 skr agent），`principal=service_principal`（**傳函式本身，不是呼叫結果**——每次觸發都現拿一個新的 service principal，不是整個 process 生命週期共用同一個)。

**跟 A2A 呼叫共用同一套授權邏輯**——排程觸發的 `AgentRequest` 跟 A2A SendMessage 觸發的 `AgentRequest` 走的是同一個 `skr_agent.run()`，差別只在 `Principal` 是哪一種（`service_principal()` vs 從 token 解出的呼叫方 principal)。這正是 `00-architecture.md` §5「定期報告 vs 使用者觸發報告：同一個 agent，不同 principal」在排程這一側的具體實作。

**為什麼是自己寫的 scheduler，不是 Claude Managed Agents 的 scheduled deployment**（`scheduler.py` 模組開頭)：Managed Agents 的排程部署是另一個代管平台，要接的話 skr agent 要在那邊重新部署一份、重新接一次 wiki backend 的連線——但這個排程本來就該跟 A2A server 用同一份 mesh（同一個 wiki backend instance、同一份 principal 邏輯），不是各自獨立的兩份系統。**這是刻意的取捨，不是「還沒空做」**：如果之後某個 job 的工作量大到需要自己的 scaling、或需要在 process 重啟後還能接續（這個 in-process scheduler 完全沒有持久化，process 重啟排程狀態全部重置），那才是換到 Managed Agents 部署更合適的時間點。

**執行語意上兩個容易誤會的地方**：

1. **`due_jobs()` 是純函式，`run_job()` 才會推進排程**——`run_forever()` 的迴圈是 `due_jobs(now)` 挑出到期的 job，再逐一 `run_job()`。測試可以呼叫 `due_jobs()` 檢查邏輯而不會有副作用，這是刻意分開的兩個方法，不是意外多出來的 API。
2. **Job 是依序執行，不是平行**（`run_forever` 內的 for-loop 是 `await` 逐一等)——「一個排程系統不該去發現兩個 BOM 掃描同時搶著發布同一頁 wiki 而互撞」是設計動機，一個 job 內部真的需要平行，應該用 skr agent 自己的 subagent 委派去做，不是靠排程主迴圈同時起兩個 top-level run。
3. **單一 job 失敗不影響其他 job**——`run_job()` 內的例外會被 log 並往上拋，`run_forever()` 的 for-loop 用 `try/except: continue` 接住，壞掉的一個 job 不會讓整個排程停擺。

---

## 6. 目前的部署形態：一個 process，兩個入口

`serving/service.py::run()` 是實際的進入點，`examples/run_service.py` 是它的 CLI 包裝：

```python
mesh = build_mesh(fixtures=..., project_root=...)
app = build_a2a_app(mesh.report_agent, base_url=..., registry=mesh.registry)
server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, ...))
scheduler = Scheduler(default_jobs(mesh, cron=cron))

await asyncio.gather(
    server.serve(),
    scheduler.run_forever(poll_interval=scheduler_poll_interval),
)
```

一個 `asyncio` event loop，一個 skr agent 實例（`mesh.report_agent`），兩件事同時掛在上面：`uvicorn` 服務 inbound 的 A2A 呼叫，`Scheduler.run_forever()` 輪詢並在時間到時觸發 outbound 的排程呼叫。**兩者呼叫的是同一個 skr agent 實例**——同一份 `InMemoryWikiBackend`（之後換真的 wiki client 也是同一份連線)、同一個 `WikiAuthorizer`。這代表一次排程寫入的東西，馬上就能透過 A2A 讀到，不需要處理兩個獨立部署之間的資料一致性問題——這是把兩者放進同一個 process 最主要換來的東西，不只是省一個部署單位。

`Ctrl+C` 會同時停掉兩者，因為它們是同一個 event loop 上的兩個 task，不是兩個獨立行程。完整的手動驗證步驟（開兩個 terminal、curl agent-card、送一個 A2A 請求、等排程觸發)見 `RUNBOOK.md` §3.6。

---

## 7. 已知限制

1. **A2A 呼叫方預設沒有真的身份驗證。** §4 提過——`Authorizer` 是留好的縫，沒配之前每個外部呼叫方都是同一個唯讀匿名身份。部署到真正對外開放的環境前必須先接一個會驗 bearer token 的 `Authorizer` 實作，否則 A2A endpoint 只是一個「大家都用同一組唯讀權限」的公開查詢介面。
2. **排程狀態不持久化。** `Scheduler._next` 只存在記憶體裡，process 重啟後所有 job 的「下次觸發時間」重新用 `next_fire(now)` 算過，不會記得重啟前錯過了哪一次——如果 process 剛好在該觸發的那一分鐘重啟，那次觸發會直接跳過，不會補跑。目前的 weekly sweep 影響不大（下週一還會再跑)，但拿去跑更頻繁、對「不能漏跑」有要求的 job 之前要先處理這個。
3. **A2A 沒有 streaming。** 見 §4——多分鐘的報告生成，呼叫方要嘛等完整回應，要嘛自己 polling task 狀態，拿不到中途進度。
4. **A2A server 目前只暴露 `mesh.report_agent`，不是整個 registry。** §4 提過，`build_a2a_app` 的 `registry` 參數只影響 agent-card 列出的 skills 名單，不代表 registry 裡其他 agent 真的能透過這個 A2A endpoint 被叫到——如果之後 registry 裡有多個可獨立被外部呼叫的 agent，這裡需要先補上「依 skill 名稱路由到對應 agent」的邏輯，不是現在這樣硬接單一 agent。
