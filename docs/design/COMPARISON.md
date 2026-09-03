# 對 deep research 任務，這套實作比什麼好、好在哪

> 這份文件是**對照**，不是設計說明。每個決定的完整理由在 [`DESIGN.md`](DESIGN.md)，這裡只講「跟另外兩個做法比，差在哪、為什麼那個差別對 deep research 有影響」。
>
> 兩個對照組：
>
> * **nanobot**（`HKUDS/nanobot`）——當初評估過、沒有選用的框架。
> * **v1**——這個 repo 自己的第一版：Claude Agent SDK harness、wiki 是一個 coordinator agent、前面掛 copilot 路由層。commit `ad78dcb`。
>
> 兩邊的依據都在 repo 裡：nanobot 的查證記錄在舊 `docs/design/00-architecture.md` §7（`git show 5f0478a:docs/design/00-architecture.md`），v1 的形狀直接讀 `ad78dcb`、`56c1039`、`a7e8f44` 三個 commit。

---

## 0. 一句話總結

框架（不管是 nanobot、Claude Agent SDK 還是 `deepagents`）給的是 **agent loop 和委派機制**。這三樣東西上面，deep research 的品質是另外一批決定堆出來的——**這套實作的價值幾乎全在那批決定，不在框架選擇**。

所以下面的對照分兩層：

1. **§1 對 nanobot**：範圍很窄，只有一個經過驗證的差異點。誠實講清楚它有多窄。
2. **§2–§8 對 v1**：這才是實質的部分，因為 v1 是同一批人在同一個問題上的前一次嘗試。

---

## 1. 對 nanobot：一個驗證過的差異點

### 1.1 查證了什麼

當時去讀了 `nanobot/agent/skills.py` 的原始碼，不是只讀 README。結論：

| | nanobot | 這套實作 |
|---|---|---|
| `SKILL.md` 檔案格式 | 相容 | 相容 |
| 載入機制 | **progressive disclosure**：把「name + description + 路徑」的摘要放進 context，模型要看全文得**自己用 file read tool 去讀** | 必要的規範**直接 inline 進 system prompt**，harness 保證它在 context 裡 |

### 1.2 為什麼這個差異對 deep research 有影響

`skills/incident-report/SKILL.md` 裝的是**嚴重度評級的量表**和**provenance 規則**。這種東西的性質是「每一次執行都必須遵守」，不是「需要時去查」。

progressive disclosure 的失敗模式是**模型判斷自己不需要讀**——而它安靜地發生。一趟沒讀量表就評級的執行，輸出看起來完全正常：格式對、語氣對、每家公司都有一個 severity。錯的只有那個 severity 憑什麼是那個值，而那件事沒有人會發現。

這在長流程裡更糟。跑到第 18 家公司時，模型早就不記得 context 開頭那份摘要提過有個檔案可以讀。

### 1.3 這個判斷後來又用了一次

換到 `deepagents` 之後同樣的問題再出現：`create_deep_agent(skills=...)` **也是** progressive disclosure。所以做了同樣的判斷、採取同樣的對策——inline。

值得記下來的是這代表**當初沒選 nanobot 的理由不是「nanobot 比較差」**，而是「skill 載入該不該靠模型自己記得去讀」這個判斷；同一個判斷在新框架上一樣成立，我們只是自己補掉了框架的行為。

**取捨講清楚**：inline 是「規範必須每次遵守」時的正確選擇。當 optional skill 變多（十幾二十個各自適用不同任務），progressive disclosure 才是對的——那時 context 塞不下，讓模型挑反而正確。目前的規模不到那裡。

### 1.4 這個對照的邊界（重要）

**當初那次查證停在讀原始碼，沒有實跑一次驗證「觸發 → 載入 → 執行」全鏈路。** 所以：

- §1.1 那格「載入機制」是讀原始碼得到的，可信。
- 除此之外，這份文件**不對 nanobot 的其他能力（記憶、多 agent 拓撲、MCP 整合、可觀測性）做任何比較宣稱**——沒有查證就沒有依據。

如果要對外用這份對照，§1 只能用到這裡為止。要更完整就得補一次實跑。

---

## 2. 對 v1：context 是有限資源，而摘要會摘掉 citation

**v1**：`company-investigator` 把完整發現直接回傳給 lead。

掃 20 家公司時 lead 的 context 會爆，然後被 `SummarizationMiddleware` 壓縮——而**壓縮掉的通常正是 citation 這種結構化細節**，也就是這份報告唯一的價值。

**現在**：investigator 把完整發現寫進 `/findings/<company_id>.md`，回覆只給一句摘要 + 檔案路徑；lead 用 `read_file` 彙整。

成立前提（**實測過，不是照文件假設**）：虛擬檔案系統在 agent 與 subagent 之間雙向共用——subagent 寫的檔案會出現在 parent 的 state 裡（`_EXCLUDED_STATE_KEYS` 只排除 `todos`/`messages`/`structured_response`，不含 `files`）。

> 對 deep research 的意義：這決定了**掃描寬度的上限**。沒有這個設計，公司數一多，報告品質不是線性下降而是斷崖式的——而且斷在最不容易發現的地方。

---

## 3. 對 v1：wiki 不是 agent，是一組掛授權的 tools

**v1**：wiki coordinator 是一個 agent，對外是 `wiki_ask` / `wiki_publish`。

**現在**：wiki 是一組 tool，授權寫在 tool handler 裡、對著 closure 綁定的 principal。

理由：wiki 要解決的問題是**授權**，而授權是規則查表不是推理。包一層 agent 的成本是多一次 model hop、多一個會幻覺的環節，換到的是零。

`wiki/coordinator.py` 的 `wiki_ask` 留著但預設不開（`with_wiki_agent=False`），因為「wiki 團隊想自己做檢索品質（query rewriting、rerank、multi-hop）」是個合理的未來——但目前沒有 caller 開它。

> **這是清單裡唯一一個「拿掉東西」的改進**，也是最容易被反向做錯的：agent 數量看起來像架構完整度，實際上每多一個不需要推理的 agent，就多一個安靜出錯的地方。

---

## 4. 對 v1：兩種 principal、三個授權動作

**v1**：假設「輸入一樣，輸出就一樣」。

**現在**：

- **同一個 agent、不同 principal**。定期報告（給高層看）和使用者觸發的報告，差別是**誰在問**不是**問什麼**。
- **檢查三個動作：讀、寫、聚合**。只檢查讀跟寫會漏掉「每一步都合法、合起來卻外洩」——一份報告把三個 division 的資訊聚合進一個低 clearance 的 namespace，每一次讀都合法，結果違規。

> 對 deep research 的意義：research agent 天生就在做聚合。**聚合檢查不是加分項，是這類 agent 的必要條件**，而它只有在你事先把它寫進契約時才存在。

---

## 5. 對 v1：能力用宣告的，不是比對名字

**v1 / 早期**：subagent 拿哪些 tool，靠 agent 模組裡手寫的**名字清單**。

那對「一個 domain、一組固定 tool」剛好夠用。加一個 domain，或接一台 MCP server，清單就靜默地不完整——而失敗模式是**subagent 安靜地拿到一個會改狀態的 tool**。

**現在**：能力宣告在 tool 上，兩個軸：

```
lookup(tool)     唯讀、取一個指名的東西    -> 所有人，含 checker
search(tool)     唯讀、用查詢找出新東西    -> 研究型 subagent，不含 checker
mutating(tool)   會改狀態                 -> 只有頂層 agent
```

**沒宣告 = 當成 mutating**（fail closed）。

這條規則直接決定了 MCP 怎麼接：MCP 協定沒有任何欄位說一個 tool 會不會寫入，所以送進來的 tool 一律沒宣告、一律只有頂層 agent 拿得到。要讓 subagent 用，operator 在 `MCP_CAPABILITIES` 明講。**從名字猜是不行的**——`search_*` 是命名慣例不是保證，猜錯等於把一個未宣告的寫入操作放到 fact-checker 後面。

### 5.1 這裡修掉過一個真的洞

`deepagents` 在呼叫端沒宣告同名 subagent 時，會**自動插入一個 `general-purpose`，繼承主 agent 的全部 tool——包含 `wiki_write_page`**。

於是「只有頂層 agent 能發布」和「發布前必過 fact-checker」兩個不變式同時被破掉。而**當時的測試只檢查我們自己宣告的清單，看不到框架加的那個，所以一直是綠的**。

現在自己宣告 `general-purpose` 蓋掉它，測試改成從 `SubAgentMiddleware` 實際收到的清單去驗——框架未來再自動加什麼會被抓到。

> 這一段值得單獨記住：**不變式要對著「實際生效的東西」驗，不是對著「我們宣告的東西」驗。** 兩者之間就是框架塞東西進來的地方。

---

## 6. 對 v1：引用檢查是 parser，而且綁在 publish gate 上

v1 沒有這一層。這是目前對報告品質影響最直接的一塊。

### 6.1 該用 parser 的地方不要用模型

| 問題 | 性質 | 用什麼 |
|---|---|---|
| 來源**有沒有這樣說** | 語意判斷 | 模型（`fact-checker`） |
| 引用**有沒有照格式附上** | 解析 | parser（`check_references`） |

差別在失敗模式。叫模型確認一份 20 家公司的報告每一段都有附來源，是**逐項清點**任務——它會安靜地漏掉一個，而且每次漏的不一樣。parser 一個都不漏、零成本、還直接告訴你在第幾行。

### 6.2 觸發靠 gate 不靠 prompt

「發布前要先檢查」寫在 prompt 裡只是**請求**，長流程跑久了模型就會跳過。

所以：`check_references` 通過時記下**該份內容的 fingerprint**，`wiki_write_page` 拿到 body 算 fingerprint，不在核可清單裡就退回。模型沒辦法宣稱「我檢查過了」而實際沒跑；**檢查完再改草稿也會失效**，因為核可綁的是那段文字。

> 誠實的限制：這道 gate 只擋得住「寫出去」那條路。read-only 部署的交付物是最終訊息本身，沒有 tool 邊界可以攔。

### 6.3 生成器與檢查器必須互相同意

格式不是只用 checker 退件，還提供 `format_reference` 直接生出正確的 markdown。**兩者一旦不一致，agent 會在兩個 tool 之間來回而永遠收斂不了**——這條性質現在有測試守著。

實際踩到過兩次，兩次都是單元測試抓不到、把 generator 產出接進 checker 跑一次才抓到的：

- 來源變成 markdown link 之後，parser 找到的是 link **target** 不是 ref，於是正確的草稿被報成引用不存在的來源。
- 修好上面那個之後，正規化只做在內文側，`source_refs` 側沒做——同一個錯誤換一半再犯一次。

### 6.4 retrieval store 存內容，不只存 ref

`ToolContext` 兩個 sink：

| | 記什麼 | 給誰用 |
|---|---|---|
| `citations` | **有讀過**某樣東西 | 報告的讀者 |
| `documents` | **讀到的內容本身** | 之後要對這批資料做事的東西 |

link 文字取自 store 裡文件的名字（tool 回傳的那個），不是模型寫的。這是「存內容不只存 ref」的直接用途——**模型記憶中的標題和來源實際的標題，不一致的機率不低**。

---

## 7. 對 v1：fact-checker 不給 search tool

v1 沒有 fact-checker。

Deep research 最常見的失敗不是查不到，是**寫出一句看起來合理、但沒有任何來源真的這樣說的話**。

`fact-checker` 逐條判 Supported / Overstated / Unsupported / Contradicted。兩個刻意的限制：

- **不給任何 search tool。** 能自己找新資料的 checker 會變成在做研究，而且它「確認」的可能是報告根本沒引用的來源。它只有重讀已被引用來源的能力。
- **prompt 明講不准用「刪掉 citation」消 REVISE。**

代價：每份報告多一次 model pass。拿 token 換可稽核性。

---

## 8. 對 v1：從一個任務變成一個框架

**v1**：整個 repo 就是供應鏈報告這一個任務。

**現在**：`core/`（與主題無關的研究迴圈）+ 可選的 `domains/` pack。

- `domain=None` 是**面向使用者**的形態：沒有主題簡報、沒有專屬資料源、沒有 specialist，就是研究迴圈 + wiki + MCP。使用者打進一個沒人預期過的問題時用這個。
- `domains/supply_chain` 是**排程任務**的形態：已知任務、每週跑、值得把知識寫下來。
- domain 只能**加**東西，不能放寬 core 的規則（`reference_rules`、`reference_format` 都是加法；`select_read_only` 會把 domain 點名但不是唯讀的 tool 丟掉並記錄）。

> 對 deep research 的意義：這兩種形態的差別不是「要不要寫 prompt」，而是**能不能事先知道問題**。把它做成同一套 core 的兩種組裝，比維護兩個 agent 誠實。

其他跟著這條線的：進入點搬進 package（`python -m deep_research_agent`）、路徑解析變成設定、啟動設定錯誤一行講清楚而不是噴 provider traceback。

---

## 9. 跨越框架換代的那條縫

v1 是 Claude Agent SDK，現在是 LangChain `deepagents`。**換框架時 `protocol.py` 完全沒動**，所以 A2A serving、scheduler、principal / 授權模型整組原封不動搬過去。

這不是運氣，是 v1 就決定的：agent 之間的契約定義在自己的 protocol，不是框架的型別。

> 這是這份對照裡**唯一一個 v1 就做對的架構決定**，也是唯一一個讓「換框架」變成 runtime 層的事而不是重寫的原因。如果之後要再換（換回 nanobot 也一樣），成本落在 `runtime.py` 和 `core/agent.py`，不在協定和授權層。

---

## 10. 可觀測性

v1 沒有。現在整趟執行進 Langfuse：每個 tool call、MCP call、subagent 各自成 span，帶 trace name / user id / session id / tags。MCP tool 的 span 額外帶 `tool_source=mcp` 和 `mcp_server=<name>`——「這趟到底碰了哪一台 MCP server」是 trace 應該要能回答的問題。

對 deep research 特別重要，因為**一趟執行可能有幾十個 tool call 和好幾個 subagent**，出問題時沒有 trace 就只能重跑。

---

## 11. 誠實的限制

不列這一節的對照不值得信。

1. **§1 對 nanobot 的比較只有一個點**，而且來自讀原始碼、沒有實跑全鏈路。其他能力沒有依據，不做宣稱。
2. **`Budget.max_usd` 沒有真的被強制**，deadline 只在進入時檢查一次，`cost_usd` 恆為 0。
3. **MCP 不傳遞使用者身分**：憑證是連線層的，從 server 看每一次呼叫都是同一個 service account。只能接「這個 agent 最低權限的呼叫方都可以看全部內容」的 server。
4. **read-only 部署沒有 publish gate**（§6.2）。
5. **沒有迴圈偵測**。目前拓撲用不到，開放給第三方 agent 時會需要。
6. **排程觸發的身分怎麼發還沒定**，`service_principal()` 目前是函式呼叫。
7. **BOM 和新聞來源沒有授權模型**，假設能觸發 agent 的人都能看整份 BOM。

---

## 12. 如果只能記三件事

1. **框架選擇不是差異來源。** nanobot / Claude Agent SDK / `deepagents` 都給 agent loop 和委派；研究品質是 §2–§7 那批決定堆出來的。真正跟框架有關的只有一件事——§1 那個「規範不該靠模型自己記得去讀」，而它在兩個框架上都得自己補。
2. **這類 agent 的錯誤是安靜的。** 沒讀量表的評級、被摘要掉的 citation、subagent 悄悄拿到寫入權、正確草稿被誤報——沒有一個會拋例外。所以設計的重點反覆是同一句：**把安靜的失敗變成吵的失敗**（fail closed、publish gate、parser 而非模型、對實際生效的清單做驗證）。
3. **不變式要對著實際生效的東西驗。** §5.1 那個洞在測試全綠的情況下存在，因為測試驗的是我們宣告的清單，不是框架實際組出來的那個。
