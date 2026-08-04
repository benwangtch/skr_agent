# Copilot / Wiki Coordinator / Report Generation — 架構決策

狀態：提案，第二版（推翻了第一版對 wiki coordinator 的判斷）
範圍：三個元件之間的關係，以及哪些東西要為未來的 skills-sharing platform 抽出來

---

## 更新記錄

第一版把 wiki coordinator 設計成一個 agent（`wiki_ask` / `wiki_publish` 兩個 AgentSpec，內部用檢索拼答案）。你指出「wiki coordinator 好像不一定要存在」之後我重新檢查了自己在第一版寫的理由，發現站不住腳的那一半（「互動形態不同」）其實是我自己在 §2 就已經承認的東西——只是沒有把結論推到底。這一版把它推到底：**wiki 不是 agent，是一組掛授權的 tools。** 詳見 §2、§3。

同時你也把兩個之前沒進到設計裡的需求講清楚了：定期報告是給高層看的，使用者自己觸發的報告要照使用者的權限走。這不是同一份報告換個開關，是兩種不同的 clearance 模型——已經在 §5 和程式碼裡落地。

---

## 0. 決策摘要

| 問題 | 決定 | 一句話理由 |
|---|---|---|
| Wiki coordinator 該不該存在？ | **不用，預設不存在** | 它要解決的問題（授權）已經在 tool 層解決了；再包一層 agent 只有成本，沒有新增的正確性 |
| Copilot 直接掛 wiki tools 行不行？ | **行，而且應該這樣做** | 授權規則寫在 tool 層的 authorizer 裡，不是寫在 copilot 裡；copilot 掛 tool 不代表 copilot 懂規則 |
| Report generation：skill 還是 sub-agent？ | 兩個都要，職責不同（不變） | skill = 格式與判準；sub-agent = 平行調查 |
| 定期報告 vs 使用者觸發報告 | **同一個 agent，不同 principal** | 差別是誰在問，不是問什麼；權限模型必須把這個差異當一等公民 |
| Deep agent 還是 nanobot？ | **Claude Agent SDK**，理由更新為「已查證」 | 見 §6：nanobot 有自己的 skills 系統，但不是 `SKILL.md`／progressive disclosure 相容 |
| 可復用什麼？ | protocol + runtime + wiki tool factory | 這三層是每個 feature 都會重寫一次的東西 |

---

## 1. 拓撲

```
                    ┌──────────────┐
   FE Dashboard ───▶│   Copilot    │  路由層 + 直接掛 wiki tools
                    └──────┬───────┘
                           │
              ┌────────────┼─────────────────┐
              │            │ agent-as-tool    │ 直接掛 tool
              ▼            ▼                  ▼
   ┌────────────────┐            ┌────────────────────────┐
   │ Report Agent    │            │  Wiki 授權 Tools        │
   │ （deep research）│───tool────▶│  wiki_search            │
   │ + BOM/news 搜尋  │            │  wiki_read_page         │
   │ + investigator   │            │  wiki_write_page        │
   │   sub-agents      │            │  （namespace 授權在這裡）│
   │ + wiki-report skill│            └───────────┬─────────────┘
   └────────────────┘                        ▼
                                    ┌────────────────────┐
                                    │  Wiki DB           │
                                    │  namespace 隔離      │
                                    └─────────▲──────────┘
                                              │
                                    ┌────────────────────┐
                                    │  Ingestion pipeline │
                                    └────────────────────┘
```

Wiki 不再是拓撲圖上獨立的一個「節點」。它是一個**掛在呼叫者裡的 toolset**，copilot 和 report agent 各自掛一份，各自用自己的 principal 建構。這不是重複——`make_wiki_toolset(backend, authz)` 是同一個函式，兩處呼叫只是傳入不同的 principal。

---

## 2. 為什麼 wiki coordinator 最後決定不做成 agent

回顧第一版的兩個理由：

> 1. 資料/權限隔離——站得住腳
> 2. 互動形態不同——**站不住腳**，我在原文就寫了「這描述的是實作複雜度，不是服務邊界」

第一版的失誤是：承認理由 2 不成立之後，沒有問下一個問題——**如果只剩理由 1，那 coordinator 到底需不需要是一個 agent？**

答案是不需要。理由 1（授權要有唯一權威）要求的是「**namespace 規則寫在一個地方**」，不要求「這個地方是一個 LLM」。授權是規則判斷（principal 的 division/role 對照一張表），不是需要推理的問題。把它包進 agent，多出來的是：

- 一次 model round trip 的延遲和成本
- 一次摘要——而摘要正是最容易把 citation 弄丟的地方（LLM 複述答案時，「引用哪份原始週報」這種結構化資訊最先被犧牲）
- 一個新的攻擊面：prompt injection 現在多一個可以說服的對象

而且它解決的問題，tool 層已經解決了：`wiki_search` 的 handler 直接呼叫 `authz.check_read(principal, namespace)`，principal 是閉包捕獲、不是參數，跟 agent 版本的保證完全一樣。

**新結論：wiki 是一組 tools，authorization 寫在 tool handler 裡，不需要 agent 包一層。**

程式碼位置：`src/skr_agent/wiki/tools.py::build_wiki_tools`。

---

## 3. Copilot 直接掛 wiki tools——重新論證

第一版說「不行」，理由是「這樣 copilot 就要懂 division→namespace 規則」。這個理由對**如果規則寫在 copilot 裡**成立，但錯誤地把「copilot 掛了 wiki tool」和「copilot 懂 wiki 規則」劃上等號。

拆開看：

```python
def make_wiki_toolset(backend, authz):
    def factory(ctx):           # ctx.principal 是呼叫者傳進來的
        async def wiki_search(args):
            authz.check_read(ctx.principal, ...)   # ← 規則在這裡，不在 copilot
```

Copilot 呼叫 `build_copilot(registry, wiki_backend=..., wiki_authz=...)`，把 backend 和 authz **當依賴注入**，不是自己重新實作規則。規則邏輯（`WikiAuthorizer` 類別）一行都沒有出現在 `copilot.py` 裡。新增一個部門，改的是 `wiki/authz.py`，copilot 不用碰。

這其實正是原本 wiki coordinator 該扮演的角色——**它不需要是網路上一個獨立服務或一個 agent，它可以是一個共用的 Python 模組（在真正跨服務時就是一個共用 client library），只要授權邏輯物理上只存在一份。**

之前的擔心（「授權必須有唯一權威」）被滿足了，只是這個「權威」現在是 `wiki/authz.py` 這個檔案／未來的 wiki 服務 client，不是一個要跑 model 的 agent。

**什麼時候要把它升級回一個獨立服務？** 當 wiki 從「in-process 模組」變成「別的團隊維護、透過網路呼叫的東西」的那天，`make_wiki_toolset` 的實作內部從呼叫本地 `WikiBackend` 換成打 API，介面不變——這是 `assembly.py` 裡明確留的擴充點。

**什麼時候要把它升級成 agent？** 只有一個理由夠格：wiki 團隊想要**主動擁有檢索品質**（query rewriting、多跳推理、reranking），這是需要模型推理的問題，不是授權問題。這種情況下 `wiki/coordinator.py` 已經是可以直接用的骨架——包住同一組 tools，加一層 synthesis。它預設不啟用（`build_mesh(with_wiki_agent=False)`），因為預設情況下多這一層沒有淨收益。

---

## 4. 為什麼 report agent 還是 deep agent（不變）

判準不變：

> 步驟數事先可知 → 固定 pipeline。
> 步驟數要現場決定 → deep agent。

Incident 收集仍然是開放式規劃——這個結論沒有因為 wiki 的決策改變而改變，因為 report agent 面對的不確定性來自**外部新聞搜尋要查幾輪**，跟 wiki 怎麼實作無關。

值得記錄的調整：report agent 現在**直接**掛 wiki tools（`make_wiki_toolset(..., writable=True)`），不再透過一層 coordinator agent 轉發。少一個 hop，citation 直接進 `ToolContext.cite()`，不需要跨 agent 邊界搬運。`company-investigator` sub-agent 只拿到 `wiki_search` / `wiki_read_page`，沒有 `wiki_write_page`——寫入權限只留給頂層 agent，這條規則沒變。

---

## 5. 定期報告 vs 使用者觸發報告：同一個 agent，不同 principal

這是這一版新增、也是你提出後我認為最該補的一塊。

你的兩句話拆開是兩個獨立的需求：

> 定期產的 report 是給高層看的 → **輸出的 clearance 比一般使用者高**
> User 自己產的 report 看他有什麼權限 → **輸出的 clearance 等於觸發者自己的**

如果只有一個 principal 種類，這兩件事沒辦法同時滿足——要嘛所有報告都放寬到「使用者能看到的」（高層專屬的內容就沒了），要嘛都收緊到「排程帳號能看到的」（一般使用者觸發時看到超出自己權限的東西）。

**解法是把「誰觸發」變成一等公民，而不是報告內容的一個參數：**

```python
# src/skr_agent/principals.py

def service_principal(...) -> Principal:
    # 跨 division 讀，只能寫進 exec namespace
    roles = {wiki.reader, wiki.reader.all, wiki.exec, wiki.writer, wiki.writer.exec}

def user_principal(subject, division, roles=...) -> Principal:
    # 預設只讀自己 division + shared；wiki.writer 也只能寫自己 division
    ...
```

同一支 `mesh.report_agent`，同一個 prompt，跑出來的報告完全不同——**這是設計上要的**，不是需要修正的不一致。使用者問「我們的供應風險」，看到的是他 division 能看到的頁面組成的答案；排程跑同一件事，看到的是跨部門彙整。

### 兩個新規則，因為這個切分而變得必要

單純把 service principal 設成「讀得比較多」是不夠的，會出現一個沒人故意設計、但邏輯上必然出現的洞：

**Namespace clearance（`wiki/authz.py::WikiAuthorizer.clearance`）**——新增一個 `exec` namespace，讀取需要 `wiki.exec` role，不是靠 division 自動給。一般使用者、就算他的 division 剛好叫 `exec`，也不會自動拿到。

**Aggregation check（`WikiAuthorizer.check_aggregation`）**——這是最容易漏掉的部分，值得展開講：排程 agent 用 `wiki.reader.all` 讀了 supply、finance、platform 三個 namespace，每一次讀都個別合法（service principal 有跨部門讀權）。彙整成一份報告後，如果寫入的目標 namespace 沒有相應的 clearance，內容就從「高層限定」洩漏成「任何人可讀」——**而這整個過程中，沒有任何一步單獨違反規則**。授權檢查如果只做在「讀」和「寫」兩個點，會漏掉「聚合」這個第三個動作。

```python
def check_aggregation(self, target: str, sources: set[str]) -> None:
    # 規則一：來源的 clearance 不能比目標寬
    # 規則二：來源橫跨兩個以上 division，目標必須是有 clearance 的 namespace
    #        （即使沒有任何單一來源本身是 gated 的）
```

`tests/test_wiki_authz.py::TestAggregation` 和 `TestWriteTool::test_aggregation_leak_is_caught_even_when_write_access_is_broad` 把這個場景寫成測試：一個對 `shared` 有寫入權的 admin，嘗試把橫跨兩個 division 的內容寫進 `shared`，即使 `check_write` 本身會放行，`check_aggregation` 仍然擋下來。**這是兩層獨立的檢查，不是一層檢查兩次。**

### 對 report agent prompt 的影響

Prompt 現在明講兩件事：

1. wiki 存取範圍由觸發者決定；查不到不代表「這件事沒發生」，可能是「這次執行看不到」
2. 一份彙整多個 division 的報告要發布到有 clearance 的 namespace；如果被拒絕，正確反應是**改發布目標**，不是拿掉造成檢查觸發的來源引用

---

## 6. Nanobot 對 Claude Skill 格式的支援程度——已查證

我去讀了原始碼（`HKUDS/nanobot`，`nanobot/agent/skills.py` + `nanobot/skills/*/SKILL.md`），不是只讀 README。結論比網路上的介紹頁準確：

**檔案格式相容，但不是同一份實作，有具體落差要注意。**

| 項目 | Claude Agent Skills | nanobot |
|---|---|---|
| 檔案 | `SKILL.md`，YAML frontmatter | 相同——`nanobot/agent/skills.py` 明確處理 `SKILL.md` |
| Frontmatter 欄位 | `name`, `description`（觸發用）, `license`, `allowed-tools` | **多了 `metadata: {"nanobot": {...}}`**——nanobot 專屬的 runtime 提示，例如 `emoji`、`requires.bins`（相依的執行檔）、`install`（怎麼裝相依套件）。你們 skill 裡沒有這個欄位，會被忽略，不會壞。 |
| Progressive disclosure | 有——描述常駐 context，本文按需讀取 | **有，但實作方式不同**：`build_skills_summary()` 產生「name + description + 相對路徑」的摘要放進 context，模型要看全文時**用 file read tool 自己讀 SKILL.md**，而不是 Claude Code 那種由 harness 決定何時注入全文的機制。對你們現有 skill 幾乎沒差，因為你們也是靠 description 觸發。 |
| `allowed-tools` frontmatter 欄位 | 有 | **驗證器認得這個欄位**（`skill-creator/scripts/quick_validate.py` 的 `ALLOWED_FRONTMATTER_KEYS` 包含它），但我沒有在載入路徑（`skills.py`）裡看到它被拿來做任何事——看起來是承認欄位存在但尚未接線執行。**不要假設它會被強制。** |
| 目錄結構（`scripts/` `references/` `assets/`） | 支援 | 支援，`ALLOWED_RESOURCE_DIRS` 同名 |
| 你們現有的 `wiki-report/SKILL.md`（只有 `name` + `description`，無 nanobot 專屬欄位） | — | **會被正確辨識、正確觸發、正確載入全文**，不需要改一個字 |

**能力邊界，不是格式問題：**

- Skill 只給知識，不給執行環境保證。這份 skill 假設有 `wiki_search` / `wiki_write_page` 這些 tool 名稱存在——不管換到哪個 runtime，tool 名稱都要對得上，這件事和 skill 格式相容性是兩回事，Claude Agent SDK 和 nanobot 都一樣。
- Nanobot 的 subagent 模型是「同一個 loop 裡 spawn，`maxConcurrentSubagents` 全域限流」（`nanobot/agent/subagent.py`），不是 Claude Agent SDK 的 `AgentDefinition` per-agent 設定（tools/model/effort 各自獨立）。如果换 runtime，`company-investigator` 那種「每個 investigator 自己的 tool 白名單」要重新設計，這是 §5 「skill vs sub-agent」裡「sub-agent 不太可分享」判斷的又一個例證。

**結論對決策的影響：** §6（原 §5）「format compatibility」這個論點基本成立——skill 檔案能原封不動搬過去。但這不構成換 runtime 的理由，因為 SKILL.md 從來不是唯一的相容性障礙；tool 名稱、subagent 設定模型都要重寫。**建議維持用 Claude Agent SDK**，因為它是唯一原生把 `SKILL.md` 當一等公民、progressive disclosure 由 harness（而非模型自己讀檔）保證的選項——這件事在你們要對外分享 skill 給 skills-sharing platform 上其他 agent 用的時候會變重要：harness 保證的行為比「模型自己決定要不要讀全文」更可預期。

---

## 7. Skill 還是 sub-agent（不變，補一句）

判準不變（見上一版 §4）。補一句跟 §6 有關的：**skill 是可搬遷的，sub-agent 設定不是。** 這進一步支持「skill 該是可分享的商品，sub-agent 定義該留在各自的 runtime 裡」這個 skills-sharing platform 的產品邊界。

---

## 8. 可復用的三層

跟上一版比多了一層：

### Layer 1：`protocol.py`——契約（不變）

`Principal` / `AgentRequest` / `AgentResponse` / `Citation` / `AgentSpec` / `Denied`。不依賴 Claude，不知道 wiki 或 report。

### Layer 2：`runtime.py` + `mesh.py`——執行殼（不變）

`DeepAgent`、`agent_as_tool`、`AgentRegistry`。

### Layer 3（新增於這一版）：`wiki/tools.py::build_wiki_tools` 這個模式本身

不是 wiki 的邏輯要復用（namespace 規則永遠留在 wiki），是**「授權 tool factory」這個形狀**值得抽成慣例：

```python
def make_<feature>_toolset(backend, authz) -> ToolsetFactory:
    def factory(ctx: ToolContext) -> ToolBundle:
        # tool handler 直接呼叫 authz.check_*(ctx.principal, ...)
        ...
    return factory
```

任何新 feature 只要「授權規則寫在 tool 層」這個判斷成立（大部分 feature 都成立——真正需要包一層 agent 的理由通常是「檢索/生成需要推理」，不是「需要授權」），都可以照這個形狀寫，不用重新決定「要不要包一個 coordinator agent」這個問題——預設不包，除非有 §3 結尾那個具體理由。

---

## 9. 對 skills-sharing platform 的意涵（更新）

除了上一版列的三點（`AgentRegistry` 是 marketplace 雛形、`AgentSpec` 統一形狀、principal 重新驗證原則），這一版多兩個：

4. **「授權 tool factory」模式（§8 Layer 3）本身可以是平台提供的建構區塊**——第三方開發者掛自己的 feature 時，不用每次重新決定「我要不要包一個 agent 才能做授權」，直接套用同一個 factory 形狀。
5. **同一 agent、不同 principal 產生不同輸出**（§5）是多租戶平台的基本假設，現在提早寫進契約，比之後補便宜。第三方 agent 在平台上跑，永遠是「這次呼叫的 principal 決定它能看到什麼」，不是「這個 agent 本身有沒有權限」。

---

## 10. 這一版實際交付了什麼

```
src/skr_agent/
  protocol.py            契約層
  mesh.py                agent-as-tool + registry
  runtime.py              DeepAgent
  principals.py           ★ 新增：service_principal / user_principal
  assembly.py              組裝，wiki_agent 預設關閉
  copilot.py               ★ 改動：直接掛 wiki tools，不再透過 coordinator
  wiki/
    authz.py                ★ 改動：加入 clearance（exec namespace）與 check_aggregation
    backend.py               不變
    tools.py                 ★ 新增：wiki 的主要介面，build_wiki_tools + make_wiki_toolset
    coordinator.py           ★ 改動：降級為 opt-in 的 LLM synthesis 層，預設不啟用
  report/
    sources.py / tools.py    不變
    agent.py                 ★ 改動：直接掛 wiki tools，prompt 加入 principal-scoped 說明
.claude/skills/wiki-report/SKILL.md   不變
fixtures/                     不變
examples/run_report.py         ★ 改動：--scheduled / 使用 principals.py
tests/                         69 個測試（原 45 + 24 新增），全部不需要 API 金鑰
```

新增測試涵蓋：exec namespace clearance、service principal 的跨部門讀寫邊界、aggregation leak（含「即使 write 權限本身放行，aggregation 仍擋下」這個關鍵情境）、wiki tools 在 `writable=False` 時 write tool 確實不存在（不是被拒絕，是不存在）、copilot 掛 wiki tool 的預設唯讀。

跑法不變，新增 `--scheduled`：

```bash
uv run python examples/run_report.py                    # 使用者觸發，自己 division 的權限
uv run python examples/run_report.py --scheduled         # 排程帳號，跨部門讀 + 寫 exec
uv run python examples/run_report.py --dry-run
uv run python examples/run_report.py --reader-only
uv run python examples/run_report.py --ask "..."
```

---

## 11. 已知取捨與未決事項

**這一版解決的（原 §9 清單的對應項）**

- ~~Usage 沒有跨 hop 聚合~~——wiki 現在是直接掛的 tool 而非透過 agent 轉發，report agent 的 usage 已經包含 wiki tool 呼叫的 token 成本，不再有「另一個 agent 的花費要往回聚合」的問題。這個副作用是拿掉 coordinator agent 之後意外解決的。

**還沒解決的（不變或新增）**

1. **Ingestion pipeline 完全沒碰。** Idempotency 沒設計。
2. **`Budget.max_usd` 只傳給 SDK，deadline 只在進入時檢查一次。** 中途超支不會中斷。
3. **沒有迴圈偵測。** 目前拓撲用不到，開放給第三方 agent 時會需要。
4. **排程觸發的身分怎麼發還沒定。** `service_principal()` 現在是函式呼叫，production 版需要決定這個憑證從哪來、怎麼輪替、被盜用時怎麼快速吊銷。
5. **`clearance` 目前是寫死在 `WikiAuthorizer` 建構子裡的 dict。** 真的有多個 gated namespace（不只 exec）時，這要嘛變成設定檔，要嘛變成從身分系統查詢——現在的形狀只是佔位。
6. **`wiki/coordinator.py`（opt-in 的 wiki_ask）沒有實際場景在用它。** 它存在是為了不擋住「wiki 團隊想要主動做檢索品質」這條路，但目前沒有 caller 開 `with_wiki_agent=True`。如果一直沒人用，該考慮直接刪掉，不要當成「以防萬一」的技術債留著。
7. **Nanobot 的驗證停在原始碼閱讀，沒有實跑。** §6 的結論基於讀 `nanobot/agent/skills.py` 和幾個內建 skill 的 frontmatter，沒有拿你們的 `wiki-report` skill 實際跑一次 nanobot 驗證「觸發、載入、執行」全鏈路。如果要认真評估換 runtime，這一步省不掉。

---

## 12. 給任何要新增 feature 的人：判斷清單

這是從這次修正裡萃取出來、下次可以直接套用的三個問題，順序很重要：

1. **這個 feature 需要授權嗎？** 需要 → 授權規則寫進一個 `authz.py`，掛成 tool（§2、§3 的模式），不要預設包 agent。
2. **這個 feature 的步驟數事先可知嗎？** 不可知 → `DeepAgent`；可知 → 直接呼叫 Messages API 就好，不要為了「架構一致」硬套 agent。
3. **這個 feature 會被多種 principal 呼叫（使用者 / 排程 / 第三方）嗎？** 會 → 現在就把 principal 種類和它們各自的 grant 寫清楚（像 `principals.py`），不要假設「反正輸入一樣，輸出應該也一樣」——那個假設在這次就是漏洞的源頭。
