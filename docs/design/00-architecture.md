# 資料來源與授權模型——skr agent 看得到什麼

狀態：實作完成
範圍：`wiki/`、`report/sources.py`、`principals.py`、`protocol.py`

這份文件講 skr agent 的**輸入面**：它有哪些資料來源、來源之間的關係、以及「誰觸發這次執行」怎麼決定它看得到什麼。執行框架、A2A serving、排程在 [`03-agent-architecture-and-serving.md`](03-agent-architecture-and-serving.md)。

---

## 更新記錄

**v3（現行）**——執行框架換成 LangChain `deepagents`（見 03）。同時把 `copilot.py` 移除：這個 repo 的範圍是 skr agent 這個 deep research agent 本身，不包含消費它的路由層。原本 §1、§3 大量以 copilot 為例的論述改寫成「呼叫方」的通則，結論不變。原 §6 的 nanobot 查證從決策依據降級為歷史記錄（見 §7），因為框架已經不是 Claude Agent SDK 了——但那次查證得到的判斷（skill 的載入不該靠模型自己記得去讀檔）**在新框架上又用了一次**，見 03 §2.3。

**v2**——第一版把 wiki coordinator 設計成一個 agent（`wiki_ask` / `wiki_publish`）。重新檢查後推翻：**wiki 不是 agent，是一組掛授權的 tools。** 詳見 §2。同時把「定期報告給高層看、使用者觸發的報告照使用者權限走」落地成兩種 principal，見 §4。

---

## 0. 決策摘要

| 問題 | 決定 | 一句話理由 |
|---|---|---|
| Wiki 該不該是一個 agent？ | **不該，它是一組掛授權的 tools** | 它要解決的問題是授權，而授權是規則查表不是推理；包一層 agent 只有成本 |
| 三個資料來源的地位？ | **平等**：BOM、外部新聞、內部 wiki | wiki 單獨成一個模組是因為只有它需要授權，不是因為它比較重要 |
| 授權規則寫在哪？ | tool handler 裡，對著 closure 綁定的 principal | 呼叫方掛了 wiki tool ≠ 呼叫方懂 namespace 規則 |
| 定期報告 vs 使用者觸發報告 | **同一個 agent，不同 principal** | 差別是誰在問，不是問什麼 |
| 授權要檢查幾個動作？ | **三個：讀、寫、聚合** | 只檢查讀跟寫會漏掉「每一步都合法、合起來卻外洩」的情況——見 §4 |

---

## 1. 三個資料來源

```
                  ┌──────────────────────────┐
                  │        skr agent          │
                  │   (report/agent.py)       │
                  └────┬──────────┬───────┬───┘
                       │          │       │
        ┌──────────────┘          │       └──────────────┐
        ▼                          ▼                      ▼
┌────────────────┐   ┌────────────────────┐   ┌────────────────────────┐
│  BOM            │   │  外部新聞            │   │  內部 wiki               │
│ (report/        │   │ (report/sources.py) │   │ (wiki/)                 │
│  sources.py)    │   │                     │   │                         │
│ list_bom_       │   │ search_news         │   │ wiki_search             │
│  companies      │   │ fetch_article       │   │ wiki_read_page          │
│ get_bom_company │   │                     │   │ wiki_write_page         │
│                 │   │                     │   │   ↓ 每次呼叫都過          │
│ 唯讀，無授權      │   │ 唯讀，無授權          │   │  WikiAuthorizer         │
└────────────────┘   └────────────────────┘   └────────────────────────┘
                                                    namespace 隔離 +
                                                    clearance + 聚合檢查
```

三個來源在 prompt 裡是平等的：BOM 說「我們依賴誰」，新聞說「外面發生什麼」，wiki 說「我們已經知道什麼、報告發布到哪」。一份有價值的報告需要三個交叉比對——只看新聞會把三個月前就記錄過的舊事當新聞，只看 wiki 則永遠不會知道外面發生了什麼。

**wiki 在程式碼上獨立成一個 package，只有一個原因：它是唯一有授權模型的來源。** BOM 和新聞對所有觸發者都一樣（誰跑都看到同一份 BOM），wiki 不是。這個不對稱是 §2–§4 的全部內容。

---

## 2. 為什麼 wiki 不做成 agent

第一版的兩個理由：

> 1. 資料/權限隔離——站得住腳
> 2. 互動形態不同——**站不住腳**（我在原文就寫了「這描述的是實作複雜度，不是服務邊界」）

第一版的失誤是：承認理由 2 不成立之後，沒有問下一個問題——**如果只剩理由 1，那它需不需要是一個 agent？**

不需要。理由 1（授權要有唯一權威）要求的是「namespace 規則寫在一個地方」，不要求「這個地方是一個 LLM」。授權是規則判斷（principal 的 division/role 對照一張表），不需要推理。包進 agent 多出來的是：

- 一次 model round trip 的延遲和成本
- 一次摘要——而摘要正是最容易把 citation 弄丟的地方（LLM 複述答案時，「引用哪份原始週報」這種結構化資訊最先被犧牲）
- 一個新的攻擊面：prompt injection 多一個可以說服的對象

而 tool 層已經解決了它：`wiki_search` 的 handler 直接呼叫 `authz.check_read(principal, namespace)`，principal 是**閉包捕獲、不是參數**，跟 agent 版本的保證完全一樣。

程式碼：`src/skr_agent/wiki/tools.py::build_wiki_tools`。

### 掛了 tool 不等於懂規則

值得拆開講，因為第一版就是在這裡搞混的：

```python
def make_wiki_toolset(backend, authz):
    def factory(ctx):                              # ctx.principal 由呼叫方傳入
        async def wiki_search(query, ...):
            authz.check_read(ctx.principal, ...)   # ← 規則在這裡
```

呼叫方把 `backend` 和 `authz` **當依賴注入**，不是自己重新實作規則。`WikiAuthorizer` 的邏輯一行都不會出現在呼叫方的程式碼裡。新增一個部門，改的是 `wiki/authz.py`，呼叫方不用碰。

「授權必須有唯一權威」這個要求被滿足了，只是這個權威是 `wiki/authz.py` 這個模組（未來是 wiki 服務的 client library），不是一個要跑 model 的 agent。

**什麼時候升級成獨立服務？** 當 wiki 從「in-process 模組」變成「別的團隊維護、透過網路呼叫的東西」——`make_wiki_toolset` 內部從呼叫本地 `WikiBackend` 換成打 API，介面不變。這是 `assembly.py` 明確留的擴充點。

**什麼時候升級成 agent？** 只有一個理由夠格：wiki 團隊想主動擁有**檢索品質**（query rewriting、多跳推理、reranking）——那是需要推理的問題，不是授權問題。`wiki/coordinator.py` 是這條路的骨架（`build_mesh(with_wiki_agent=True)`），預設關閉。

---

## 3. 授權 tool factory 這個形狀

不是 wiki 的邏輯要復用（namespace 規則永遠留在 wiki），是**這個形狀**值得當慣例：

```python
def make_<source>_toolset(backend, authz) -> ToolsetFactory:
    def factory(ctx: ToolContext) -> ToolBundle:
        # tool handler 直接呼叫 authz.check_*(ctx.principal, ...)
        ...
    return factory
```

新增一個需要授權的資料來源時照這個形狀寫，不用重新決定「要不要包一個 coordinator agent」——預設不包，除非有 §2 結尾那個具體理由。

---

## 4. 定期報告 vs 使用者觸發報告：同一個 agent，不同 principal

兩個獨立的需求：

> 定期產的 report 是給高層看的 → **輸出的 clearance 比一般使用者高**
> User 自己產的 report 看他有什麼權限 → **輸出的 clearance 等於觸發者自己的**

只有一種 principal 沒辦法同時滿足——要嘛所有報告都放寬到「使用者能看到的」（高層專屬內容就沒了），要嘛都收緊到「排程帳號能看到的」（一般使用者看到超出自己權限的東西）。

**解法是把「誰觸發」變成一等公民，而不是報告內容的一個參數：**

```python
# src/skr_agent/principals.py

def service_principal(...) -> Principal:
    # 跨 division 讀，只能寫進 exec namespace
    roles = {wiki.reader, wiki.reader.all, wiki.exec, wiki.writer, wiki.writer.exec}

def user_principal(subject, division, roles=...) -> Principal:
    # 預設只讀自己 division + shared；wiki.writer 也只能寫自己 division
```

同一支 `mesh.report_agent`、同一個 prompt，跑出來的報告完全不同——**這是設計上要的**，不是需要修正的不一致。

`service_principal()` 刻意**不帶** `wiki.admin`：讀取範圍放寬了，寫入範圍仍然只有 exec namespace，所以週報裡的一段 prompt injection 沒辦法把排程帳號變成任意寫入的憑證。

### 兩個因為這個切分而必要的規則

單純把 service principal 設成「讀得比較多」會出現一個沒人故意設計、但邏輯上必然出現的洞：

**Namespace clearance**（`WikiAuthorizer.clearance`）——`exec` namespace 的讀取需要 `wiki.exec` role，不是靠 division 自動給。一般使用者就算 division 剛好叫 `exec` 也拿不到。

**Aggregation check**（`WikiAuthorizer.check_aggregation`）——最容易漏掉的一塊，值得展開：排程 agent 用 `wiki.reader.all` 讀了 supply、finance、platform 三個 namespace，每一次讀都個別合法。彙整成一份報告後，如果寫入的目標 namespace 沒有相應 clearance，內容就從「高層限定」洩漏成「任何人可讀」——**而這整個過程中，沒有任何一步單獨違反規則**。授權只做在「讀」和「寫」兩個點，會漏掉「聚合」這第三個動作。

```python
def check_aggregation(self, target: str, sources: set[str]) -> None:
    # 規則一：來源的 clearance 不能比目標寬
    # 規則二：來源橫跨兩個以上 division，目標必須是有 clearance 的 namespace
    #        （即使沒有任何單一來源本身是 gated 的）
```

`tests/test_wiki_authz.py::TestWriteTool::test_aggregation_leak_is_caught_even_when_write_access_is_broad` 把這個場景寫成測試：一個對 `shared` 有寫入權的 admin，嘗試把橫跨兩個 division 的內容寫進 `shared`，即使 `check_write` 放行，`check_aggregation` 仍然擋下來。**這是兩層獨立的檢查，不是一層檢查兩次。**

### 對 prompt 的影響

Prompt 明講兩件事：

1. 資料存取範圍由觸發者決定；查不到不代表「這件事沒發生」，可能是「這次執行看不到」
2. 一份彙整多個 division 的報告要發布到有 clearance 的 namespace；如果被拒絕，正確反應是**改發布目標**，不是拿掉造成檢查觸發的來源引用

---

## 5. Skill 還是 subagent

判準：

> **Skill** = 格式與判準（報告長什麼樣、嚴重度怎麼分級）——知識，可搬遷。
> **Subagent** = 平行調查（每家公司一個獨立 context window）——執行結構，跟 runtime 綁定。

兩個都要，職責不重疊。`company-investigator` subagent 只拿到唯讀的 tool 子集，寫入權限只留給頂層 agent——這條規則從 v1 到現在沒變，實作方式換了（見 03 §2.1）。

**skill 是可搬遷的，subagent 設定不是**——這對之後的 skills-sharing platform 是一條產品邊界：skill 該是可分享的商品，subagent 定義該留在各自的 runtime 裡。

---

## 6. 對 skills-sharing platform 的意涵

1. **`AgentRegistry` 是 marketplace 的雛形**——一個可以列舉、描述、交給模型的具名能力目錄。
2. **`AgentSpec` 讓 agent / tool / skill-backed workflow 是同一個形狀**，所以一個 agent 能被當成另一個的 tool，不需要每次寫轉接器。
3. **Principal 要在信任邊界重新驗證**，不信呼叫方對自己權限的宣稱。
4. **「授權 tool factory」（§3）可以是平台提供的建構區塊**——第三方開發者掛自己的 feature 時，不用每次重新決定「我要不要包一個 agent 才能做授權」。
5. **同一 agent、不同 principal 產生不同輸出**（§4）是多租戶平台的基本假設，提早寫進契約比之後補便宜。

---

## 7. 歷史記錄：nanobot 的 skill 支援查證

**這一節已經不是決策依據**（框架現在是 `deepagents`，見 03 §2.1），保留是因為它得出的判斷後來又用了一次。

當時去讀了 `HKUDS/nanobot` 的原始碼（`nanobot/agent/skills.py`），不是只讀 README。結論：**`SKILL.md` 檔案格式相容，但載入機制不同**——nanobot 產生一份「name + description + 路徑」的摘要放進 context，模型要看全文得**自己用 file read tool 去讀**，而不是由 harness 保證注入。

這個差異當時是選 Claude Agent SDK 而非 nanobot 的理由。**換到 `deepagents` 之後，同樣的問題又出現了一次**：`create_deep_agent(skills=...)` 也是 progressive disclosure。所以做了同樣的判斷、採取同樣的對策——把必要的規範直接 inline 進 system prompt。見 03 §2.3。

一併記錄當時沒做完的部分：這個查證停在讀原始碼，沒有實跑一次驗證「觸發、載入、執行」全鏈路。

---

## 8. 已知取捨與未決事項

1. **Ingestion pipeline 完全沒碰。** Idempotency 沒設計。
2. **`Budget.max_usd` 沒有真的被強制。** deadline 只在進入時檢查一次，中途超支不會中斷；`cost_usd` 現在恆為 0（見 03 §8）。
3. **沒有迴圈偵測。** 目前拓撲用不到，開放給第三方 agent 時會需要。
4. **排程觸發的身分怎麼發還沒定。** `service_principal()` 現在是函式呼叫，production 版需要決定這個憑證從哪來、怎麼輪替、被盜用時怎麼快速吊銷。
5. **`clearance` 是寫死在 `WikiAuthorizer` 建構子裡的 dict。** 真的有多個 gated namespace 時，這要嘛變成設定檔，要嘛從身分系統查詢——現在的形狀只是佔位。
6. **`wiki/coordinator.py`（opt-in 的 `wiki_ask`）沒有實際場景在用。** 它存在是為了不擋住「wiki 團隊想主動做檢索品質」這條路，但沒有 caller 開 `with_wiki_agent=True`。如果一直沒人用，該直接刪掉，不要當成「以防萬一」的技術債留著。
7. **BOM 和新聞來源沒有授權模型。** 目前假設「能觸發這個 agent 的人都能看整份 BOM」。如果之後 BOM 本身有分級（例如某些供應商合約條件只有採購看得到），要照 §3 的形狀補一個 `report/authz.py`，而不是在 prompt 裡叫 agent 自己小心。

---

## 9. 給任何要新增 feature 的人：判斷清單

順序很重要：

1. **這個 feature 需要授權嗎？** 需要 → 授權規則寫進一個 `authz.py`，掛成 tool（§2、§3 的模式），不要預設包 agent。
2. **這個 feature 的步驟數事先可知嗎？** 不可知 → `DeepAgent`；可知 → 直接呼叫 chat model 就好，不要為了「架構一致」硬套 agent。
3. **這個 feature 會被多種 principal 呼叫（使用者 / 排程 / 第三方）嗎？** 會 → 現在就把 principal 種類和它們各自的 grant 寫清楚（像 `principals.py`），不要假設「反正輸入一樣，輸出應該也一樣」——那個假設在 v1 就是漏洞的源頭。
