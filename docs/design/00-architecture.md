# Copilot / Wiki Coordinator / Report Generation — 架構決策

狀態：提案，待 review
範圍：三個元件之間的關係，以及哪些東西要為未來的 skills-sharing platform 抽出來

---

## 0. 決策摘要

| 問題 | 決定 | 一句話理由 |
|---|---|---|
| Wiki coordinator 該不該存在？ | **該**，但它是**服務邊界**，不是「一個 agent」 | 有人得是 namespace 授權的唯一權威；那個東西就叫 wiki coordinator |
| Copilot 直接掛 wiki tools 行不行？ | **不行** | 那等於把 division→namespace 的規則複製到 copilot，每加一個部門要改兩個服務 |
| Report generation：skill 還是 sub-agent？ | **兩個都要，但職責不同** | skill = 格式與判準（知識）；sub-agent = 平行調查（執行）；它們不是替代關係 |
| Report agent 掛在誰底下？ | **和 wiki coordinator 平行，但把它當成自己的 tool** | 外部搜尋和多公司 fan-out 不是 wiki 的職責 |
| Deep agent 還是 nanobot？ | **Claude Agent SDK**，而且這個問題被問錯了（見 §5） | 真正的軸線是「loop 誰擁有」和「skill 是什麼格式」 |
| 可復用什麼？ | **protocol + runtime 兩層**，feature 邏輯不復用 | 這兩層是每個 feature 都會重寫一次的東西 |

---

## 1. 拓撲

```
                    ┌──────────────┐
   FE Dashboard ───▶│   Copilot    │  路由層，不含 feature 邏輯
                    └──────┬───────┘
                           │  agent-as-tool（同一套 protocol）
              ┌────────────┴─────────────┐
              ▼                          ▼
   ┌────────────────────┐    ┌────────────────────────┐
   │  Wiki Coordinator  │◀───│  Report Agent          │
   │  （其他同事開發）    │tool│  （deep research）      │
   │  wiki_ask          │    │  + BOM / news search    │
   │  wiki_publish      │    │  + company-investigator │
   └─────────┬──────────┘    │    sub-agents           │
             │               │  + wiki-report skill    │
             ▼               └────────────────────────┘
   ┌────────────────────┐
   │  Wiki DB           │
   │  （namespace 隔離） │
   └─────────▲──────────┘
             │
   ┌────────────────────┐
   │  Ingestion pipeline │  weekly reports → wiki pages
   └────────────────────┘
```

三條邊，同一種呼叫方式：`AgentRequest → AgentResponse`。這是刻意的——多加一個 feature 應該是註冊一筆，不是寫一個新的整合。

---

## 2. 為什麼 wiki coordinator 要獨立存在

你給的兩個理由（資料/權限隔離、互動形態不同）我認為只有**第一個**站得住腳，第二個其實不是理由。這個區別重要，因為它決定了 coordinator 該長什麼樣。

### 站得住腳的：授權必須有唯一權威

規則是「一個 division 的週報只能更新該 division namespace 的頁面」。如果 copilot 直接掛 `query_wiki(namespace=...)`，那 copilot 就得知道：

- 每個使用者屬於哪個 division
- 哪些 namespace 是共用的
- 讀和寫的規則不對稱（可以讀 `shared`，但不能寫進 `shared`）

每新增一個部門、每改一次共用規則，要同時改 copilot 和 wiki。更糟的是：**未來 skills-sharing platform 上的第三方 agent 也會想查 wiki**，那時候你不可能要求每個第三方 agent 都正確實作你的 namespace 規則。

所以 coordinator 存在的理由是：**授權決策必須發生在資料旁邊，而且必須由 callee 決定，不能相信 caller 的宣稱。**

程式碼裡這件事是這樣落實的（`mesh.py`）：

```python
def agent_as_tool(spec, *, principal, parent, ...):
    async def _invoke(args):
        request = AgentRequest(principal=principal, ...)  # ← closure，不是 args
```

principal 是**閉包捕獲**的，不是 tool 參數。模型無法在 tool call 裡寫一個不同的 `division`，因為 schema 裡根本沒這個欄位。這不是靠 prompt 約束，是靠型別。

對應測試：`tests/test_mesh.py::TestPrincipalBinding`、`tests/test_wiki_authz.py::TestCoordinatorEnforcement::test_caller_cannot_widen_scope_via_inputs`。

### 站不住腳的：「互動形態不同」

「wiki 問答需要多步 tool use，所以要有自己的 loop」——這描述的是**實作複雜度**，不是**服務邊界**。Copilot 本來就是 agent，它自己就能跑多步 tool use。如果只有這個理由，正確答案是把 tools 掛給 copilot，不是拆服務。

這點值得講清楚，因為它影響 wiki coordinator 的設計：**coordinator 不一定要是 deep agent。** 它只要是一個「有授權、有 citation、可被當成 tool 呼叫」的服務。它內部是 RAG、是 deep agent、還是查資料庫，是你們同事的自由，只要 contract 不變。

這也是為什麼這一版把它 mock 掉，而且 mock 得很笨（`wiki/coordinator.py` 只做關鍵字檢索 + 拼接），沒有任何損失。

---

## 3. 為什麼 report agent 是 deep agent，wiki coordinator 不必是

你自己的判斷是對的：incident 收集是**開放式規劃**——每家公司要查幾次、查到什麼程度算夠、要不要繼續 cross-reference，都是模型現場決定的。步驟數不固定，這正是 deep agent 存在的理由。

反過來說，deep agent 是貴的選項。判準是：

> **步驟數事先可知 → 固定 pipeline（直接呼叫 Messages API）。
> 步驟數要現場決定 → deep agent。**

用這個判準檢查三個元件：

| 元件 | 步驟數 | 結論 |
|---|---|---|
| Copilot | 通常一次 tool call + 轉述 | 淺 agent 就夠，`effort="medium"` |
| Wiki coordinator | 檢索 → 組答案，可預測 | 不必是 deep agent |
| Report agent | 每家公司查幾輪不固定 | **真正需要 deep agent** |

所以 `runtime.py` 的 `DeepAgent` 三個都能用，但參數差很多——copilot 是 `effort="medium", max_turns=20`，report agent 是 `effort="high", max_turns=60` 加 sub-agents。

---

## 4. Skill 還是 sub-agent？兩個都要，但別搞混

這是最容易做錯的一題。它們不是同一個軸上的選項：

| | Skill | Sub-agent |
|---|---|---|
| 是什麼 | **知識**：格式、判準、規則 | **執行單元**：獨立 context 的 worker |
| 解決什麼 | 「報告該長什麼樣」 | 「四家公司要平行查」 |
| 載入時機 | progressive disclosure，需要時才進 context | 主 agent 決定要 fan-out 時 |
| 可分享嗎 | **可以**——這就是 skills platform 的商品 | 不太行，綁死在 harness |

具體到 wiki report：

- **`.claude/skills/wiki-report/SKILL.md`** 放 severity rubric、頁面結構、provenance 規則。這是你們現在已經在維護的那個 skill，形式不變。它是「知識」，換一個 agent 來執行也還是同一份。
- **`company-investigator` sub-agent** 負責「查一家公司」。四家公司同時發出去，各自有獨立 context，不會互相污染。這是「執行」。

如果只有 skill 沒有 sub-agent：BOM 掃描會變成主 agent 在一個 context 裡塞四家公司的搜尋結果，context 爆掉、後面幾家品質下降。
如果只有 sub-agent 沒有 skill：每個 investigator 的 severity 判準會不一致，因為判準只存在於 prompt 裡而不是可共享的文件。

一個具體的設計選擇：**investigator 沒有 `wiki_publish` 權限**。只有主 agent 能寫。這在 `tests/test_wiring.py::test_subagent_cannot_publish` 有守住。

---

## 5. Deep agent 還是 nanobot？——這個問題要重問

「deep agent vs nanobot」把兩個不同的決策綁在一起了。拆開之後是兩個問題：

**問題 A：agent loop 誰擁有？**
自己寫 / Agent SDK 提供 / 平台代管。

**問題 B：skill 用什麼格式？**
Claude 的 `SKILL.md`（progressive disclosure）/ 框架自訂 / 純 prompt 字串。

問題 B 對你們是決定性的，因為你說**要為 skills-sharing platform 而設計**。一個能分享 skill 的平台，商品就是 skill 本身；如果 skill 格式和你們現在維護的那份不一樣，遷移成本會在最不該出現的地方出現。

**建議：Claude Agent SDK。** 理由：

1. 原生吃 `SKILL.md`，你們現有那份 wiki report skill 直接沿用，不用翻譯成 prompt
2. loop、planning、context compaction、sub-agent dispatch、MCP tools 都內建，我們不重寫
3. tool 是 MCP——這代表未來要換 host（含 nanobot 這類 MCP-based 的 runtime）時，tool 層可以整組搬走

關於 nanobot 我要誠實說明：我對它目前的細節掌握不夠完整，不該基於印象做這個決定。但**架構上這個決定可以延後**——因為 `protocol.py` 完全不依賴 Claude。`AgentRequest`/`AgentResponse`/`Principal` 沒有一行 import claude_agent_sdk。真的要換 runtime，換的是 `runtime.py` 一個檔案，protocol 和所有 feature 邏輯不動。

**建議的驗證方式**：拿實際的 BOM 掃描任務，同一份 skill、同一組 tool，在兩個 runtime 上各跑一次，比 (a) 完成率 (b) token 成本 (c) skill 是否需要改寫。這比繼續在會議上辯論便宜。

---

## 6. 可復用的是哪兩層

你說 runtime 和 protocol 都要復用。我同意，但要講清楚**不該復用什麼**——不然抽象會長到擋路。

### Layer 1：`protocol.py` — 契約（最重要）

```python
Principal      # 誰在問。authority。
AgentRequest   # 一次工作。principal + task + inputs + budget + trace
AgentResponse  # 統一回傳。status / output / citations / usage
Citation       # 出處。wiki_page | raw_report | external_url | internal_record
AgentSpec      # 一個有名字、有描述、有 schema 的能力。capability。
```

這一層不知道 wiki、不知道 report、不知道 Claude。它是唯一四個元件（含未來的 platform）共同依賴的東西，所以必須保持乾淨。

兩個設計重點：

- **`Principal` 帶 `token`，其他欄位只是解碼後的方便視圖。** 跨信任邊界時重新驗 token，不信 caller 給的 division。
- **`Citation` 進 protocol 而不是留給各 feature 自己處理。** 因為 wiki 這個 feature 的整個價值主張就是「頁面能追回原始週報」。provenance 是契約的一部分，不是呈現細節。

`Denied` 也在這層：權限拒絕是**正常結果**，不是錯誤。它會被轉成 `status="refused"` 回給呼叫的模型，讓模型知道為什麼被擋而改變策略——而不是當成 transport failure 重試。

### Layer 2：`runtime.py` + `mesh.py` — 執行殼

`DeepAgent` 做的事，是每個 feature 不做就得自己重寫一次的：

- 把 tool 綁到 principal（tools 是**每次 request 建構**的 factory，不是 module-level 常數——這是安全性的關鍵）
- budget 遞減（`Budget.child()` 永遠不會比 parent 大）
- 收集 citation（`ToolContext.cite()`，tool 自己記錄出處，不靠模型記得複述）
- 把 SDK message stream 轉回 `AgentResponse`
- `setting_sources=[]` 預設不載入任何磁碟設定，避免 tool surface 被 `~/.claude` 意外撐大

`mesh.py` 的 `agent_as_tool` 是讓「agent 互相當 tool」變成一行的東西。copilot→wiki 和 report→wiki 走的是**同一個函式**，不是兩個整合。

### 不該復用的

feature 邏輯。`wiki/authz.py` 的 namespace 規則就該留在 wiki 服務裡；report agent 的 BOM 邏輯就該留在 report 裡。把授權規則抽到共用層，代表每加一個 feature 都要改一個共用檔案——這是我們正在避免的東西。

---

## 7. 對 skills-sharing platform 的意涵

現在做的三件事，是那個產品直接要用的：

1. **`AgentRegistry`** 就是 marketplace 的雛形。`register` / `list` / `catalog` / `dispatch`——一個能被列舉、被描述、被交給模型的能力目錄。現在是 in-process dict，之後換成服務，介面不變。
2. **`AgentSpec` 統一了 agent / tool / skill-backed workflow 的形狀。** 這代表「別的部門分享出來的能力」可以直接被你的 agent 當 tool 用，不需要 per-integration 的 adapter。
3. **`Principal` 的重新驗證原則**是多租戶的前提。平台上跑別人寫的 agent 時，你不能相信它宣稱的身分。這個原則現在就寫進契約，比之後補便宜得多。

一個現在還沒做、但那個產品一定需要的：**能力的 sandboxing 與計費歸屬**。`Budget` 有欄位但只做到 turn 上限；`Usage` 有回傳但沒有往上聚合。列在 §9。

---

## 8. 這一版實際交付了什麼

```
src/skr_agent/
  protocol.py          契約層（不依賴 Claude）
  mesh.py              agent-as-tool + registry
  runtime.py           DeepAgent（Claude Agent SDK 之上的薄殼）
  assembly.py          組裝——唯一知道所有模組的檔案
  copilot.py           copilot 的 tool surface
  wiki/
    authz.py           namespace 規則（wiki 服務自己的）
    backend.py         儲存介面 + fixture 實作
    coordinator.py     ★ MOCK — 同事接手時只換這個檔案
  report/
    sources.py         BOM / news 介面 + fixture 實作
    tools.py           toolset factories
    agent.py           ★ 主要交付物：deep research agent
.claude/skills/wiki-report/SKILL.md   報告格式與 severity rubric
fixtures/              4 家公司、4 篇新聞、5 頁 wiki、4 份原始週報
examples/run_report.py end-to-end 跑一次
tests/                 45 個測試，全部不需要 API 金鑰
```

跑起來：

```bash
python examples/run_report.py                          # 完整掃描並發布
python examples/run_report.py --dry-run                # 只研究不發布
python examples/run_report.py --reader-only            # 驗證權限拒絕路徑
python examples/run_report.py --ask "ASC-4400 現在的供應風險？"   # 走 copilot
```

---

## 9. 已知取捨與未決事項

**明確做了但可能有爭議的取捨**

- **Copilot 直接呼叫 report agent，不經過 wiki coordinator。** 代價是 copilot 的 tool 數量會隨 feature 線性成長，到十幾個以上時要引入 tool search 或分層路由。但另一個選擇（讓 wiki coordinator 代理 report generation）會讓它變成一個它並不擁有的能力的 proxy，更糟。
- **`permission_mode="bypassPermissions"`。** tool surface 已經用 `allowed_tools` 明確白名單，且沒有給 Bash/Write，所以不需要互動式確認。**但這在生產環境要重新檢視**——尤其如果之後給 report agent 檔案或 shell 工具。
- **Report agent 的 `wiki_publish` 失敗時不繞路。** prompt 明講：被拒絕就回報完成的內容並說明沒發布，不要試別的路徑。這是刻意的——一個會想辦法繞過權限的 agent 比一個會失敗的 agent 危險得多。

**還沒解決的**

1. **Ingestion pipeline 完全沒碰。** 這一版假設 wiki 頁面已經存在。ingestion 的 idempotency（同一份週報跑兩次會怎樣）沒有設計。
2. **`Usage` 沒有跨 hop 聚合。** report agent 呼叫 wiki coordinator 的成本目前不會回到 report agent 的 usage 裡。計費歸屬要靠這個。
3. **Budget 只做到 turn 上限。** `max_usd` 有傳給 SDK，deadline 只在進入時檢查一次，不會中途中斷。
4. **沒有迴圈偵測。** `parent_agent` 有記錄但沒用來擋 A→B→A。目前拓撲不會發生，開放給第三方 agent 時會。
5. **排程觸發沒實作。** 你說排程和即時都要——即時路徑（copilot → report agent）已經通了；排程只是同一個 `agent.run()` 換一個 principal（service account）來呼叫，但 service account 的身分怎麼發還沒定。
6. **Nanobot 沒有實測。** §5 的建議是基於「skill 格式相容性」這個論點，不是基於實測比較。

---

## 10. 給 wiki 團隊的 contract

同事接手時要滿足的，只有這些：

```python
# 兩個 AgentSpec，名字固定
wiki_ask      # inputs: {task: str, namespace?: str}
wiki_publish  # inputs: {task, namespace, slug, title, body, source_refs: [str]}
```

三條不可協商的規則：

1. **授權在 callee 決定。** 從 `request.principal` 重新推導可讀/可寫的 namespace，不要相信 inputs 裡任何看起來像權限宣稱的欄位。
2. **`source_refs` 為空時必須拒絕 publish。** 這是 wiki 可信度的基礎，要在服務端擋，不能只寫在 prompt 裡。
3. **回傳的 `citations` 要包含 `raw_report`，不只是 `wiki_page`。** 使用者問的是「這個結論從哪來」，答案是原始週報，不是中間的 wiki 頁。

`src/skr_agent/wiki/coordinator.py` 是一份會動的參考實作，內部怎麼做（RAG、deep agent、SQL）完全自由。
`tests/test_wiki_authz.py` 的 12 個測試可以直接拿去對真實實作跑——如果真實實作能通過，介面就是相容的。
