# deep research agent — 設計文件

狀態：實作完成，對應現行程式碼
範圍：整個 `src/deep_research_agent/`

這份文件描述系統**現在的樣子**，是唯一一份架構參考。先前有三份按時間累積、彼此重疊又開始互相矛盾的設計文件，已經合併進這裡並刪除。演進過程在 git log 裡，這裡只講結論。

操作手冊（怎麼跑、怎麼驗證）在 [`../RUNBOOK.md`](../RUNBOOK.md)。套件管理在 [`package-management.md`](package-management.md)。

---

## 1. 這是什麼

**這是一個 deep research agent。** 它接開放式問題，自己決定要挖多深，跨多個資料來源交叉比對，產出**每個主張都有出處**的報告，並可發布到內部 wiki。

它不綁定任何特定主題。實務上有兩種部署形態，用的是同一個 agent：

| | 已知任務、排程跑 | 未知任務、使用者輸入 |
|---|---|---|
| 例子 | 每週供應鏈掃描 | 使用者打字問任何事 |
| 組法 | `build_mesh(domain=supply_chain.from_fixtures)` | `build_mesh(domain=None)` |
| 多了什麼 | BOM/新聞來源、`company-investigator`、嚴重度 rubric | 什麼都沒多 |
| 入口 | `serving/scheduler.py`、`cli/report.py` | A2A、`cli/ask.py` |

**`domain=None` 是完整形態，不是降級形態。** 讓它擅長研究的東西（§4 那四個設計）全都在 core，兩種形態都有。domain 只會**加**東西，不能拿掉也不能放寬任何規則——§3.4。

它可以被三種方式觸發，走的是同一條程式碼路徑：

| 觸發方式 | 入口 | 身份 |
|---|---|---|
| 同 process 的另一個 agent | `mesh.agent_as_tool` | 呼叫方的 `Principal`（closure 綁定） |
| 跨 process 的 A2A 呼叫 | `serving/a2a.py` | token 經 `Authorizer.verify()` 重新驗證 |
| cron 排程 | `serving/scheduler.py` | `service_principal()` |

**「誰觸發」決定「看得到什麼」**，這是整個系統的核心不變式，第 5 節展開。

---

## 2. 決策摘要

| 問題 | 決定 | 理由 |
|---|---|---|
| 執行框架 | LangChain **`deepagents`**（LangGraph 之上） | 內建 planning/todo、context 摘要、subagent 委派、virtual filesystem——§3.1 |
| 主題知識放哪 | `core/` 通用 + `domains/<name>/` 主題包，**domain 可有可無** | 排程是已知任務、face-to-user 是未知任務，同一個 agent 要吃下兩種——§3.4 |
| subagent 拿得到哪些 tool | 由 **tool 自己宣告能力**決定，不是寫死名單 | 掛了 domain 和 MCP 之後，名單永遠不可能完整——§3.5 |
| 模型怎麼接 | LangChain `BaseChatModel`，由 `config/llm.py` 建 | 不綁 wire protocol，內部 gateway 有 OpenAI-compatible endpoint 就能接——§6 |
| 怎麼讓它擅長 deep research | scratchpad + 發布前 fact-checker + 停止條件 + 矛盾攤開 | 框架只給 loop 和委派，研究品質是這四個決定堆出來的——§4 |
| 報告規範怎麼載入 | inline 進 system prompt，不用 `skills=` 的按需載入 | 必須遵守的規範不該靠模型記得去讀檔——§3.3 |
| wiki 是 agent 還是 tool | **一組掛授權的 tools**，不是 agent | 它要解決的是授權，授權是規則查表不是推理——§5.2 |
| 定期報告 vs 使用者觸發 | 同一個 agent，不同 `Principal` | 差別是誰在問，不是問什麼——§5.3 |
| 授權檢查幾個動作 | **三個：讀、寫、聚合** | 只查讀寫會漏掉「每步都合法、合起來外洩」——§5.4 |
| A2A SDK 版本 | `a2a-sdk` **1.1.2**（釘死） | 1.1.2 是最新的 1.x；這套件 API 變動很兇——§7.1 |
| 排程 | 自寫的 in-process `Scheduler` | 要跟 A2A server 共用同一份資料來源與 principal 邏輯——§7.4 |
| A2A 與排程的部署 | 同一個 process、同一個 `asyncio.gather` | 兩者只是「誰觸發」的不同入口——§7.5 |
| MCP service | 第四種資料來源，啟動時載入一次 | tool 本來就是 LangChain tool；但**身份不會傳過去**——§5.5 |
| 觀測性怎麼做？ | **Langfuse**（LangChain callback handler），預設關閉 | LangSmith 不能用；tool call / MCP / subagent 全部自動進 trace——見 §6.1 |
| 套件內 import | 一律絕對 import | 有測試擋著，見 §8 |

---

## 3. 執行框架

### 3.1 為什麼是 deepagents

`deepagents` 內建了一個 deep research agent 本來就需要、自己寫很花時間的東西：

- **planning / todo 狀態**——掃 20 家公司不會默默漏掉幾家
- **context 摘要**（`SummarizationMiddleware`）——長時間研究一定會撐爆 context
- **subagent 委派**（`task` tool）——每家公司一個獨立 context window
- **virtual filesystem**——中間筆記有地方放（§4.1 大量用到）

`runtime.py::DeepAgent` 刻意只是薄薄一層殼：把 `AgentRequest` 轉成 graph 輸入、graph 輸出轉回 `AgentResponse`、把 principal 綁進 tools。framework 給的東西一個都沒重做。

### 3.2 三層職責

```
protocol.py     契約層。Principal / AgentRequest / AgentResponse / AgentSpec /
                Budget / Citation。不 import 任何 agent framework。
                    ↑ 從 Claude Agent SDK 換成 deepagents 時，這層一行都沒改，
                      A2A serving、排程、授權模型因此全部原封不動沿用。
                      不是巧合，是刻意把 framework 擋在契約外面換來的。

runtime.py      執行殼。DeepAgent 包住 create_deep_agent。

serving/        怎麼被外部觸發到。
```

### 3.3 Skill 為什麼 inline

`create_deep_agent(skills=...)` 的載入機制是 **progressive disclosure**：prompt 裡只放 skill 的名字和描述，模型自己判斷要不要 `read_file` 去讀本文。

`skills/incident-report/SKILL.md`（報告格式 + 嚴重度分級）是**每次跑都必須遵守**的規範，不是「碰巧有用就查」的參考。所以 `runtime.py::load_skill()` 直接讀檔（去掉 YAML frontmatter，那是給 catalog 用的 metadata）接到 system prompt 後面。

**這不代表 `skills=` 是錯的設計。** 等 skill 有十幾二十份、大部分情況只有一兩份相關時，按需載入才是對的取捨。**界線大約在十份**——現在一份，inline 是對的。加第三、第四份 skill 的人請重新評估。

加一份 skill 有三條路（操作細節見 RUNBOOK §3.8）：

- **你自己維護、放在別處**：`SKILLS_PATH` 指到裝著它的目錄，`SKILLS_ENABLED` 寫名字。不改程式碼，也不留一份會跟原始檔不一致的複製品。同名時 `SKILLS_PATH` 蓋過內建的。
- **直接給路徑**：`skills=["incident-report", "/abs/path/to/x"]`，名字含 `/` 或結尾 `.md` 就當路徑讀。
- **屬於這個 repo（排程用的走這條）**：建 `skills/<name>/SKILL.md`，名字加進該 domain 的 `skills`（供應鏈的在 `domains/supply_chain/agent.py`）。排程是無人看管的，規範必須跟程式碼一起版控、一起 review，否則 job 的行為可能在沒有任何 commit 的情況下改變。

skill 資料夾是 `skills/`（不是 `.claude/skills/`——那是這個專案還跑在 Claude Agent SDK 上時的殘留，現在仍然找得到但不是主要位置）。一個排程 job 依賴的規範是**版控的商業邏輯**，藏在以工具命名的隱藏目錄裡就不會有人 review 它。

放進資料夾但沒有被載入的 skill，`build_research_agent` 會 log 一行 warning 點名它——資料夾慣例的失敗模式就是「看起來裝好了，其實沒有」。這行 warning **只在有掛 domain 時發**：沒有 domain 的通用 agent，repo 裡的 rubric 沒被用到是正常狀態，每次跑都警告只會讓人學會忽略警告。

`SKILLS_ENABLED` 是附加不是取代——一個能安靜關掉報告 rubric 的環境變數是個陷阱。找不到 skill 會 raise 並列出每個找過的路徑，不會安靜跳過。

### 3.4 core 與 domain

```
core/          跟主題無關的部分
  prompt.py      研究方法、證據紀律、停止條件、查核、發布規則（分段組裝）
  subagents.py   general-purpose + fact-checker
  domain.py      ResearchDomain / Specialist 兩個 dataclass
  agent.py       build_research_agent(domain=None)

capabilities.py  tool 能力宣告（§3.5），跟 protocol.py 同層的葉子模組

domains/
  supply_chain/  BOM 與新聞來源、company-investigator、incident-report rubric
```

**判準：這段文字換成專利研究、法規研究、事故調查，還成不成立？** 成立的放 core，不成立的放 domain。「讀完再引用」「兩個來源打架要攤開」到處都成立；「別名很重要，事故常掛在母公司名下」只在供應鏈成立。

一個 `ResearchDomain` 只提供五樣東西，全部是**加法**：

| 欄位 | 加了什麼 |
|---|---|
| `briefing` | 一段 prompt，插在通用角色之後、通用方法之前 |
| `toolsets` | 這個主題自己的資料來源 |
| `specialists` | 懂這個主題子任務形狀的 subagent |
| `skills` | 輸出必須遵守的 rubric |
| `inputs` | agent input schema 多出來的結構化欄位（排程呼叫端用） |

**domain 不能放寬任何規則。** specialist 宣告要哪些 tool 只是「請求」不是「授權」——core 會再過一次唯讀檢查（§3.5），要到寫入 tool 的會被丟掉並 log。有測試直接建一個貪心 domain 驗這件事。

沒有 domain 時，prompt 裡的 `# Publishing` 那段會依照有沒有寫入 tool 決定要不要出現——描述一個不存在的 tool 等於教模型幻覺呼叫。

**加新主題是加一個 `domains/` 底下的 sibling package，不是改 `core/`。** 如果你為了加 domain 而必須改 core，那個東西大概本來就是通用的，該憑自己的道理放進 core。

### 3.5 tool 能力宣告

兩個安全性質要在**沒人列舉過的 tool 表面**上成立：只有頂層 agent 能改東西；fact-checker 不能去找新資料。

舊寫法是在 agent 模組裡手寫 tool **名字**清單。那只在「一個 domain、一組固定 tool」下成立；多一個 domain 或一台 MCP server，清單就默默不完整了，而不完整的後果是某個 subagent 安靜地拿到會改狀態的 tool。

所以能力搬到 tool 上，兩個軸：

```python
lookup(tool)     # 唯讀，取一個具名的東西    → 誰都可以，包含 fact-checker
search(tool)     # 唯讀，用 query 找出新東西  → 研究用 subagent，不給 checker
mutating(tool)   # 會改外面的狀態            → 只有頂層 agent
```

`fact-checker` 拿的是全部 `lookup`，`general-purpose` 拿的是全部唯讀。不是名字比對，是 metadata 過濾。

**沒宣告 = 當成會改狀態。** 這是預設方向的選擇：猜錯一邊只是 subagent 少一個 tool，猜錯另一邊是一次沒有查核關卡的未宣告寫入。MCP tool 因此天生被擋在所有 subagent 外——那正是我們對它的真實知識狀態。確認某個 MCP tool 安全之後，讓它經過 `lookup()`/`search()` 再進來。

實測過兩件事：

- **忘記宣告不會破功。** 把 `wiki_write_page` 的 `mutating()` 拿掉，整套測試仍然全綠——因為 fail-closed，未宣告一樣被擋。
- **宣告錯會被抓到。** 把它改宣告成 `search()`，**7 個測試變紅**，其中包含從 `SubAgentMiddleware` 實際收到的清單去驗的那幾個。

---

## 4. 讓它擅長 deep research 的四個設計

框架給的是 agent loop 和委派機制。**研究品質是下面四個決定堆出來的。**

### 4.1 Scratchpad：investigator 寫檔案，lead 讀檔案

掃 20 家公司時，如果每個 investigator 把完整發現塞回 lead 的對話裡，lead 的 context 會爆掉、然後被 `SummarizationMiddleware` 壓縮——**壓縮掉的通常正是 citation 這種結構化細節**，而那是這份報告唯一的價值。

所以：investigator 把完整發現寫進 `/findings/<company_id>.md`，回覆只給一句摘要 + 檔案路徑；lead 用 `read_file` 從檔案彙整。

成立前提是**虛擬檔案系統在 agent 與 subagent 之間雙向共用**。這點實測過，不是照文件假設：subagent 寫的檔案會出現在 parent 的 state 裡（`_EXCLUDED_STATE_KEYS` 只排除 `todos`/`messages`/`structured_response`，不含 `files`）。

一個容易誤解的地方：`company-investigator` 「建構上唯讀」指的是**對 wiki 唯讀**（拿不到 `wiki_write_page`，那才是有授權意義的邊界）。它對虛擬檔案系統一直有寫入權——`deepagents` 不管 `tools` 給什麼，都會給每個 subagent 一份 `FilesystemMiddleware`。

### 4.1b 沒有任何 subagent 能發布

固定兩個 core subagent（`general-purpose`、`fact-checker`），加上 domain 提供的 specialist（供應鏈是 `company-investigator`）。**沒有一個拿得到 `wiki_write_page`**——不是靠白名單，是靠 §3.5 的能力過濾。只有頂層 agent 能發布，所以 §4.2 的查核關卡繞不過去。

`general-purpose` 是刻意保留的：把一個獨立的子問題丟進一個乾淨的 context window 去查，對開放式研究很有用——模型可以自己寫指示、自己決定要委派什麼。它只是不能發布。

**這一段是修正過的。** `deepagents` 在呼叫端沒有宣告同名 subagent 時，會**自動插入一個 `general-purpose`，而它繼承主 agent 的全部 tool——包含 `wiki_write_page`**。於是「只有頂層 agent 能發布」跟「發布前必過 fact-checker」兩個不變式同時被破掉，而當時的測試只檢查我們自己宣告的清單，看不到框架加的那個，所以一直是綠的。現在我們自己宣告 `general-purpose`（覆蓋掉自動的那個），測試也改成從 `SubAgentMiddleware` 實際收到的清單去驗——框架未來再自動加什麼，會被抓到。**`domain=None` 的形態也有同一組驗證**，因為那是最可能在沒人 review 這份清單的地方被組起來的形態。

**`core/subagents.py` 的 `core_subagents()` 裡不能拿掉 `general-purpose`**：拿掉它，框架就會把有寫入權的那個放回來。

### 4.2 發布前必過 fact-checker

Deep research 最常見的失敗不是查不到，是**寫出一句看起來合理、但沒有任何來源真的這樣說的話**。

`fact-checker` subagent 拿到草稿與它宣稱的來源，逐條判 Supported / Overstated / Unsupported / Contradicted，給 PASS 或 REVISE。兩個刻意的限制：

- **不給任何 search tool。** 能自己找新資料的 checker 會變成在做研究而不是查核，而且它「確認」的可能是報告根本沒引用的來源。它只有 `fetch_article` / `wiki_read_page` / `get_bom_company`，用來**重讀已被引用的來源**。
- **prompt 明講不准用「刪掉 citation」消 REVISE。** 沒有來源的宣稱，正確處理是拿掉那句話或找到來源。

代價：每份報告多一次 model pass。拿 token 換可稽核性。

### 4.3 明確的停止條件

「什麼時候算查完」不寫的話，模型會往兩個方向失敗：抓到第一個看起來對的答案就收工，或一直挖不知道停。草擬前的檢查點：

- 原本要涵蓋的東西裡，哪些沒有 finding？（那是缺口，不是乾淨結果）
- 哪些宣稱只有單一來源？
- 什麼資訊會改變你的結論？便宜的話就去查。

**是檢查不是儀式**——沒缺就說沒缺然後往下走。

### 4.4 矛盾要攤開

外部新聞與內部記錄對不上時，模型的預設行為是安靜挑一個比較順的。但**那個矛盾本身通常才是整份報告最有價值的東西**。prompt 要求兩邊都寫、都附來源、並說明比較相信哪一個與為什麼。

---

## 5. 資料來源與授權

### 5.1 來源地位平等

```
┌──────────────────────────────────────────────────────────────┐
│           deep research agent (core/agent.py)                 │
│                                                               │
│  core 一定掛：                                                  │
│    內部 wiki      wiki_search(search) / wiki_read_page(lookup) │
│                   / wiki_write_page(mutating)                 │
│                                    ← 唯一有授權模型的來源         │
│  domain 選配（供應鏈為例）：                                       │
│    BOM           list_bom_companies(search) /                 │
│                  get_bom_company(lookup)                      │
│    外部新聞       search_news(search) / fetch_article(lookup)   │
│  MCP server(s)   你設定的任何 tool（預設無）                       │
│                                    ← 未宣告，身份不傳，見 5.5      │
│                                                               │
│  subagents 拿到什麼由括號裡的能力決定，不由名字決定                    │
│    general-purpose  全部唯讀        fact-checker  全部 lookup    │
└──────────────────────────────────────────────────────────────┘
```

一份有價值的報告需要交叉比對：只看新聞會把三個月前就記錄過的舊事當新聞，只看內部記錄永遠不知道外面發生什麼。

**wiki 由 core 掛而不是由 domain 掛**，因為「內部已經知道什麼」和「做完的東西放哪」是每個研究任務都有的問題，包含一個對不上任何 domain 的使用者提問。它在程式碼上獨立成一個 package，只因為它是唯一有授權模型的來源——這個不對稱是 5.2–5.4 的全部內容。

### 5.2 wiki 為什麼不是 agent

授權要求的是「namespace 規則寫在一個地方」，不要求「那個地方是一個 LLM」。授權是規則判斷（principal 的 division/role 對照一張表），不需要推理。包進 agent 多出來的是：一次 model round trip 的延遲與成本、一次**最容易弄丟 citation** 的摘要、以及一個新的 prompt injection 攻擊面。

tool 層已經解決了：`wiki_search` 的 handler 直接呼叫 `authz.check_read(principal, namespace)`，principal 是**閉包捕獲、不是參數**——模型無法在 tool call 裡寫一個不同的 division 冒充別人，因為 schema 裡根本沒這個欄位。

`wiki/coordinator.py`（opt-in 的 `wiki_ask`）是為「wiki 團隊想主動擁有檢索品質（query rewriting、多跳、reranking）」留的骨架，預設關閉，目前沒有 caller。**一直沒人用就該刪掉**，不要當「以防萬一」的技術債留著。

### 5.3 兩種 principal

```python
# principals.py
service_principal()   # 跨 division 讀，只能寫進 exec namespace
user_principal(...)   # 只讀自己 division + shared；有 wiki.writer 也只寫自己 division
```

同一支 agent、同一個 prompt，跑出來的報告完全不同——**這是設計上要的**。使用者問「我們的供應風險」，看到的是他 division 能看到的頁面組成的答案；排程跑同一件事，看到的是跨部門彙整。

`service_principal()` 刻意**不帶** `wiki.admin`：讀取範圍放寬，寫入仍只有 exec namespace，所以週報裡的一段 prompt injection 沒辦法把排程帳號變成任意寫入的憑證。

### 5.4 三個檢查點：讀、寫、聚合

只把授權做在「讀」和「寫」會漏掉一個沒人故意設計、但邏輯上必然出現的洞：

排程 agent 用 `wiki.reader.all` 讀了 supply、finance、platform 三個 namespace，每一次讀都個別合法。彙整成報告後如果寫進 clearance 不足的 namespace，內容就從「高層限定」洩漏成「任何人可讀」——**而整個過程沒有任何一步單獨違規**。

```python
# wiki/authz.py::WikiAuthorizer
check_read(principal, namespace)          # 讀
check_write(principal, namespace)         # 寫
check_aggregation(target, sources)        # 聚合 ← 這個
#   規則一：來源的 clearance 不能比目標寬
#   規則二：來源橫跨兩個以上 division，目標必須是有 clearance 的 namespace
```

`tests/test_wiki_authz.py` 有一個對 `shared` 有寫入權的 admin，把跨兩個 division 的內容寫進 `shared`：`check_write` 放行，`check_aggregation` 擋下。**兩層獨立檢查，不是一層查兩次。**

另外 `exec` namespace 的讀取需要 `wiki.exec` role，不是靠 division 自動給——一般使用者就算 division 剛好叫 `exec` 也拿不到。

### 5.5 MCP：身份不會傳過去

公司內部 MCP service 可以掛成第四種資料來源（設定見 §6）。**預設完全關閉**：`MCP_*` 一個都沒設就不會連任何地方。

三個設計決定：

- **啟動時載入一次。** `ToolsetFactory` 是同步、每 request 呼叫；MCP 探索是網路往返。所以啟動時載入，factory 只發已載好的物件。每次**呼叫**仍各自開 session。代價：server 之後新增的 tool 要重啟才看得到。
- **連不上就降級。** 記一行 `mcp.load_failed` 跳過。輔助資料來源掛掉應該讓 agent 少幾個 tool，不是讓它起不來。
- **每個 MCP tool 包一層記 `Citation`**（`mcp://<server>/<tool>`）。prompt 要求每個事實都有出處，MCP 來的資料就得有東西可指。

> **⚠️ 這個整合最重要的限制：身份不會傳過去。**
> 其他每個 tool 都 closure 綁著呼叫者的 `Principal` 並對它授權。MCP tool 做不到——憑證是**連線層級**的（`MCP_TOKEN`），從 MCP server 角度看，不管誰觸發都是同一個 service account。
>
> **只接「這個 agent 權限最低的呼叫者也可以看到全部內容」的 MCP server。** 如果那個 service 有 per-user 規則，現在這個接法會繞過它。要修就得改成 per-request 建 client 帶終端使用者的 token。

MCP tool **不給任何 subagent**，而且這件事現在是自動的：它們沒有經過 §3.5 的能力宣告，未宣告一律當成會改狀態。這正是我們對它的真實知識狀態——我們無從得知某個 MCP tool 會不會改東西。確認某個 tool 安全之後，在包裝時讓它經過 `lookup()` 或 `search()`，它就會自動出現在對應的 subagent 手上。

---

## 6. Config 層

一個服務一個檔案、一個 class、一個 `env_prefix`，全部繼承 `BaseConfig`。**目的是把「搬進公司環境」壓到「改 `.env`」。**

```
config/  base.py  llm.py（接上）  mcp.py（接上，預設關）  langfuse.py（預設關）
         paths.py（skills / fixtures 的根，checkout 內自動找）  db.py / minio.py（佔位）
```

pydantic v2 下的關鍵行為（有測試驗證）：**子類別的 `model_config` 會跟父類別合併，不是整個蓋掉**——所以子類同時有 `env_file`（繼承）和 `env_prefix`（自己設的）。

**唯一規則**：這個資料夾外面不准直接讀 `os.environ`。所有設定經過某個 `get_*()`（都是 `lru_cache` singleton；測試用 `reset_settings_cache()`）。

**LLM**：`build_chat_model()` 回傳**建構好的物件**而非 `"provider:model"` 字串——字串帶不了 `base_url`，而「指到不同 endpoint」正是這個 config 存在的理由。

```bash
LLM_PROVIDER=custom
LLM_BASE_URL=https://llm.internal.corp/v1   # OpenAI-compatible chat-completions
LLM_API_KEY=內部憑證
LLM_MODEL=內部 model 名稱
```

`custom` 走 OpenAI chat-completions——**絕大多數內部 gateway（vLLM、LiteLLM、公司 proxy）本來就開這個介面**，不用翻譯層。金鑰沒填會在**建構時**報錯，不是跑到一半才失敗。

**MCP**：單一 server 用 `MCP_URL` + `MCP_TOKEN`；多個或 stdio 用 `MCP_SERVERS`（JSON）。兩者都設時 **`MCP_SERVERS` 整組蓋過 `MCP_URL`，不是合併**——合併到一半的連線設定比明顯被忽略的變數難除錯得多。

### 6.1 Langfuse 追蹤

**兩把 key 都設才會開**（`LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY`），`LANGFUSE_BASE_URL` 預設就是內部那台。只設一把會被當成設定錯誤報出來，而不是半開。

開了之後，一次 run 會匯出：

| 想看的東西 | 怎麼進到 trace 的 |
|---|---|
| **每個 tool call** | LangChain 每個 tool run 都發 callback，自動變成一個 span（含參數、結果、耗時）。這邊不用列舉任何 tool |
| **哪些是 MCP、來自哪台 server** | 光看 span 名字分不出來，所以 `mcp.py` 在包裝時把 `tool_source="mcp"` 和 server 名字寫進 tool 的 metadata，跟著進 span |
| **subagent 委派** | `task` 這個 tool call 的 input 帶 `subagent_type`，subagent 自己的 graph 又是一個以它命名的巢狀 chain span——**一次委派會出現兩次**，這是刻意的：一次是「決定要委派」，一次是「實際做的事」 |
| **誰觸發的** | trace 的 Langfuse user 是 `Principal.subject`，tag 帶 division 和 actor（service/user）。同一個問題不同 principal 跑出來的兩份 trace，就是靠這個分辨 |

session id 用 `trace_id`——A2A 本來就把 `task_id` 當 `trace_id` 傳進來，所以一個 A2A task 和它觸發的 run 會歸在同一個 session。

**兩條不會妥協的規則**（`observability.py`）：

1. **追蹤不能弄壞 run。** host 連不到、key 被拒、exporter 起不來——全部降級成「沒有追蹤」並記一次 log。觀測後端掛掉不是研究工作失敗的理由（實測過：把 base_url 指到死掉的 port，run 照常 `status=ok`）。
2. **沒設定就完全不連。** 沒有 handler、沒有網路行為。

`uv run python -m deep_research_agent check` 會報告目前是開是關。**注意它不驗證連得到**——SDK 是背景批次匯出的，host 打錯的症狀是「Langfuse 裡沒有 trace」，不是啟動報錯。

---

## 7. Serving

### 7.1 A2A：版本是 1.1.2

`serving/a2a.py` 把 agent 包成講 A2A（JSON-RPC + SSE）的 FastAPI app。**1.1.2 是 PyPI 上最新的 1.x**（1.0.0 → 1.0.3 → 1.1.0 → 1.1.2，沒有 1.2/1.3）。釘死版本，因為 0.3.x → 1.x 每一項都是破壞性變更：

| 0.3.x | 1.1.x |
|---|---|
| `A2AStarletteApplication` | **模組不存在**。改用 `create_*_routes()` + `add_a2a_routes_to_fastapi()`，所以 `build_a2a_app()` 直接回 `FastAPI`，沒有 `.build()` |
| pydantic 型別 | **protobuf 型別**。`TextPart`/`FilePart` 不存在，`Part` 是扁平訊息（`text`/`raw`/`url`/`data` oneof + `filename`/`media_type`），檔案放 raw bytes |
| `TaskStatusUpdateEvent(final=True)` | **沒有 `final` 欄位**，終態由 state 決定，`TaskUpdater` 會擋終態後的更新 |
| `TaskState.completed` | `TaskState.TASK_STATE_COMPLETED` |
| `AgentCard.url` | `AgentCard.supported_interfaces` |
| metadata 是 dict | protobuf `Struct`，要 `MessageToDict` 遞迴轉 |

### 7.2 `A2A-Version` header 與 body 必須配套

**header 沒帶不是「沒有版本」，SDK 會當成 `0.3`。** 實測（`enable_v0_3_compat=True`）：

| header | body | 結果 |
|---|---|---|
| `A2A-Version: 1.0` | 1.0（`SendMessage`、`ROLE_USER`） | 正常 |
| 不帶 | 0.3（`message/send`、`user`） | 正常，走 compat adapter |
| 不帶 | 1.0 | `VERSION_NOT_SUPPORTED` |

失敗訊息是**版本不符**（拿 1.0 body 比對預設的 0.3），不是「缺 header」。留著 compat 是讓既有 0.3 client 不改也能打。

### 7.3 一次請求的生命週期

```
SendMessage / SendStreamingMessage
  → enqueue Task(TASK_STATE_SUBMITTED)     ← 必須第一個，見下
  → 沒有輸入文字 → failed，不叫 agent
  → resolve_principal(metadata, call_context)
      Denied → failed，內容是拒絕原因，不是 500
  → submit() → start_work()
  → async for event in agent.stream(request):
        progress → update_status(TASK_STATE_WORKING, ...)
        AgentResponse → 最終結果
  → 檔案 → artifact event；文字 → 收集
  → complete(message=答案) / failed(message=答案)
```

**兩個實際踩到的坑：**

1. **Executor 必須先 enqueue 一個 `Task`。** `DefaultRequestHandler` 拒絕在 Task 開出來前抵達的 status event，而 `TaskUpdater.submit()` 送的是 status update **不是 Task**。這個 bug **整套 unit test 都沒抓到**（裸的 `EventQueue` 對事件順序沒意見），是實際發 HTTP 請求才炸出來的——所以有了 `TestThroughTheRealHandler`。
2. **答案放在終態事件上。** 舊做法把答案當 `working` 訊息送、終態不帶內容，對串流呼叫方沒差，但**非串流的 `SendMessage` 只看得到最終 Task**，於是拿到空答案。

**progress 只講「跑了哪個 tool」，不回傳 tool 輸出**（`runtime.py::_progress_note`）——tool 回傳經常包含呼叫方沒權限看的東西，progress feed 不該成為繞過 tool 層授權的側漏管道。

**身份**：沒配 `authorizer` 時每個呼叫方都是 `default_principal`（唯讀、`shared`），啟動時印一行警告。配了就讀 `metadata["token"]` 驗證，失敗直接 `Denied`，**不會**退回 default——否則 authorizer 形同虛設。

### 7.4 排程

```python
ScheduledJob(name, cron, agent, task, principal, inputs, budget, timezone)
Scheduler(jobs).run_forever(poll_interval=30)
```

`principal` 可傳 callable，每次觸發現拿一個新的（`service.py` 就是傳 `service_principal` 函式本身）。

**為什麼不是 Claude Managed Agents 的 scheduled deployment**：那是另一個代管平台，接的話 agent 要在那邊重新部署、重接資料來源連線。但排程本來就該跟 A2A server 用同一份 mesh。**這是刻意取捨**：等到某個 job 需要自己的 scaling、或需要在 process 重啟後接續（現在完全沒有持久化），那才是換過去的時間點。

三個容易誤會的語意：`due_jobs()` 是純函式、`run_job()` 才推進排程；job **依序執行不平行**（排程器不該是兩個 sweep 搶著發布同一頁 wiki 的地方）；單一 job 失敗不影響其他 job。

### 7.5 部署形態：一個 process，兩個入口

```python
mcp_toolset = await mcp_toolset_from_config()          # 啟動時一次
mesh = build_mesh(..., extra_toolsets=[mcp_toolset] if mcp_toolset else ())
app = build_a2a_app(mesh.agent, url=..., registry=mesh.registry)
await asyncio.gather(
    uvicorn.Server(...).serve(),
    Scheduler(default_jobs(mesh, cron=cron)).run_forever(...),
)
```

一個 event loop、一個 agent 實例、兩件事掛在上面。**兩者呼叫同一個 agent、同一份 backend**，所以排程寫入的東西馬上能透過 A2A 讀到，不用處理兩個獨立部署之間的資料一致性。`Ctrl+C` 同時停掉兩者。

---

## 8. 測試策略

```
260 個測試，全部不需要金鑰、不呼叫模型
  test_wiring.py      55   tool 清單、subagent 邊界、deep research 結構、報告取回、skill 載入、import 風格
  test_wiki_authz.py  32   namespace 授權、clearance、aggregation leak
  test_a2a_server.py  32   executor 生命週期 + 6 個走真 handler/HTTP 的整合測試
  test_general_agent.py 24 無 domain 也完整、domain 只能加不能放寬、能力宣告與 fail-closed
  test_config.py      22   provider 預設、env 覆蓋、chat model 形狀
  test_scheduler.py   19   cron 時序、失敗隔離
  test_observability.py 19  Langfuse 設定、降級、trace metadata、MCP 標記
  test_mcp.py         17   設定解析、降級、對真的 MCP server 載入/呼叫/citation
  test_mesh.py        15   agent-as-tool 的 principal 綁定、citation 傳遞
  test_cli.py         25   四個入口都 import 得起來、path 解析（含 checkout 外）、啟動前的設定檢查
```

三個刻意的選擇：

- **不呼叫模型。** 測的是接縫（授權規則、principal 解析、排程時序、訊息轉換）。每次 CI 花 token、又因模型隨機性而不穩定的測試不值得。真的跑一次的步驟在 RUNBOOK §3。
- **MCP 測試跑真的 MCP server subprocess**，不 mock client——會壞的是跟 `langchain-mcp-adapters` 的契約，mock 那個契約只是把自己的假設複述一遍。用 `-m "not mcp_server"` 可跳過。
- **A2A 有整合測試走真的 handler + HTTP。** 因為 unit test 結構上抓不到 §7.3 那個 bug。
- **安全規則靠「弄壞它」驗過，不只是靠測試存在。** 拿掉 `wiki_write_page` 的 `mutating()` 宣告 → 全綠（fail-closed 本來就擋）；把它錯宣告成 `search()` → 7 個測試變紅。一個從沒紅過的安全測試等於沒有安全測試。

---

## 9. 已知限制

**授權 / 安全**

1. **A2A 呼叫方預設沒有真的身份驗證。** `Authorizer` 是留好的縫，沒配之前所有外部呼叫方共用一個唯讀匿名身份。對外開放前必須接。
2. **MCP 不傳遞使用者身份**（§5.5）。這是目前最需要注意的一條。
3. **排程憑證怎麼發還沒定。** `service_principal()` 現在是函式呼叫，production 要決定從哪來、怎麼輪替、怎麼吊銷。
4. **`clearance` 寫死在 `WikiAuthorizer` 建構子裡。** 真有多個 gated namespace 時要變設定檔或從身分系統查。
5. **domain 的來源沒有授權模型。** 供應鏈的 BOM 與新聞目前假設能觸發這個 agent 的人都能看全部。之後要分級的話照 §5.2 的形狀在該 domain 裡補一個 `authz.py`，不要在 prompt 裡叫 agent 自己小心。

**研究品質**

6. **查詢改寫只寫在 prompt 裡，沒有結構化。** investigator 被要求輪過法人名/別名/母公司/料號/事件詞，但沒有東西保證它跑完。
7. **深度不隨 tier 變。** `critical` 與 `standard` 供應商目前花一樣的力氣。
8. **來源品質/時效沒有權重。** 嚴重度有 rubric，「這個來源可不可信、是不是三年前的」沒有。

**執行 / 運維**

9. **排程狀態不持久化。** process 重啟後重算下次觸發時間，錯過的不補跑。
10. **A2A task 歷史存在記憶體**（`InMemoryTaskStore`），重啟掉光。
11. **`Budget.max_usd` 沒有真的被強制**，deadline 只在進入時檢查一次，`cost_usd` 恆為 0。
12. **沒有迴圈偵測。** 目前拓撲用不到，開放給第三方 agent 時會需要。
13. **MCP tool 只在啟動時探索一次**，server 之後新增的 tool 要重啟。
14. **`wiki/coordinator.py` 沒有 caller**（§5.2）。

---

## 10. 給要新增 feature 的人

四個問題，順序很重要：

1. **這件事只對某個主題成立，還是對研究普遍成立？** 普遍 → `core/`；只對某主題 → `domains/<name>/`。判準見 §3.4：把句子裡的「供應商」換成「專利」還成不成立。
2. **需要授權嗎？** 需要 → 規則寫進一個 `authz.py`，掛成 tool（§5.2 的形狀），**不要預設包一個 agent**。
3. **步驟數事先可知嗎？** 不可知 → `DeepAgent`；可知 → 直接呼叫 chat model，不要為了「架構一致」硬套 agent。
4. **會被多種 principal 呼叫嗎？**（使用者／排程／第三方）會 → 現在就把各自的 grant 寫清楚（像 `principals.py`），不要假設「輸入一樣，輸出應該也一樣」——那個假設就是 §5.4 那個洞的來源。

**新增一個 tool**：用 `capabilities.py` 的 `lookup()` / `search()` / `mutating()` 宣告它能做什麼。不宣告是安全的但很受限——未宣告當成會改狀態，任何 subagent 都看不到它。

**新增一個主題**：在 `domains/` 下加一個 sibling package，回傳一個 `ResearchDomain`。`core/` 不該有任何改動。

新增一個 IO service：複製 `config/db.py` 的形狀（一個檔案、一個 class、一個 `env_prefix`、一個 cached getter、在 `__init__.py` 匯出）。

套件內 import 一律**絕對 import**（`from deep_research_agent.x import y`），`tests/test_wiring.py::TestImportStyle` 擋著。
