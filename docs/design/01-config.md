# Config 層——env-driven 設定，換環境不改程式碼

狀態：實作完成
範圍：`src/skr_agent/config/`

這份文件只講設定層。A2A serving 和排程在
[`03-agent-architecture-and-serving.md`](03-agent-architecture-and-serving.md)。

> **這份文件取代了舊的 `01-config-and-serving.md`。** 那一版的 LLM 章節描述的是
> Claude Agent SDK 的環境變數注入（`ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN`
> 那一套），在換成 `deepagents` 之後不再成立；A2A 的部分現在歸
> `03-agent-architecture-and-serving.md` §4 管。留著只會誤導，所以整份重寫。

---

## 0. 目標

把所有對外部 IO 服務（LLM、DB、物件儲存……）的設定集中成一個資料夾，一個服務一個檔案、一個 class、一個 env prefix。**目的是把「搬進公司環境」這件事的成本壓到「改 `.env`」，不需要改任何一行程式碼。**

---

## 1. 結構

```
src/skr_agent/config/
  __init__.py    匯出所有 class + get_* 函式 + reset_settings_cache()
  base.py        BaseConfig
  llm.py         LLM（真的有接上）
  db.py          DB（佔位，還沒有東西在用）
  minio.py       Minio（佔位）
```

```python
class BaseConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file            = '.env',
        env_file_encoding   = 'utf-8',
        extra               = 'ignore',
    )

class Minio(BaseConfig):
    endpoint            : str = '...'
    bucket_name         : str = 'cpoml-object-storage'
    model_config = SettingsConfigDict(env_prefix = 'minio_')
```

這個 pattern 在 pydantic v2 下有一個關鍵行為（有先寫測試驗證過，不是憑印象）：**子類別的 `model_config` 會跟父類別合併，不是整個蓋掉**——所以 `Minio.model_config` 同時擁有 `env_file`（繼承）和 `env_prefix`（自己設的）。這正是這個 pattern 能運作的原因。

**新增一個服務**：複製 `db.py`，改 class 名字、改 `env_prefix`、加欄位、加 `get_*()` cached getter、在 `__init__.py` 匯出。不需要碰其他任何檔案。

**唯一規則**：這個資料夾外面的程式碼不准直接讀 `os.environ` 拿設定值。所有設定都經過這裡的某個 `get_*()`。這是讓「換環境只改 env var」成立的前提。

---

## 2. LLM——為什麼現在簡單了

skr agent 跑在 LangChain 上，所以模型就是一個 `BaseChatModel`，**protocol 由 client 決定**。這跟之前跑在 Claude Agent SDK 上時差很多：那時 SDK 只講 Anthropic Messages wire protocol，任何要接的 endpoint 都必須也講那個協定，公司內部 gateway 如果只講 OpenAI 格式就得在前面架一個翻譯 proxy。現在不用。

```python
# config/llm.py::LLM.build_chat_model()
provider = "openrouter"  # → ChatOpenAI(base_url="https://openrouter.ai/api/v1")
provider = "openai"      # → ChatOpenAI
provider = "custom"      # → ChatOpenAI(base_url=<你的內部 gateway>)
provider = "anthropic"   # → ChatAnthropic
```

`build_chat_model()` 回傳的是**建構好的物件**，不是 `"provider:model"` 字串——因為字串帶不了 `base_url`，而「指到不同的 endpoint」正是這個 config 存在的全部理由。provider SDK 的 import 是 function-local 的，所以 import config 不會把每個 provider 的套件都拉進來。

### 設定方式

```bash
cp .env.example .env
```

預設（OpenRouter + Qwen3.5，模擬內部 host）只需要填一行：

```bash
LLM_API_KEY=sk-or-v1-你的OpenRouter金鑰    # https://openrouter.ai/keys
```

換成公司內部 gateway：

```bash
LLM_PROVIDER=custom
LLM_BASE_URL=https://llm.internal.corp/v1   # OpenAI-compatible /v1/chat/completions
LLM_API_KEY=內部發的憑證
LLM_MODEL=內部 host 用的 model 名稱
```

`custom` 走的是 OpenAI chat-completions API——**這正是絕大多數內部 gateway（vLLM、LiteLLM、公司 proxy）本來就會開的介面**。不用翻譯層，不改程式碼。

驗證設定有生效（不打 API、不花錢）：

```bash
uv run python -c "from skr_agent.config import get_llm; l=get_llm(); print(l.provider, l.resolved_base_url(), l.resolved_model())"
```

### 兩個行為值得知道

1. **金鑰沒填會在建構時就報錯**，不是跑到一半才失敗。這是相對舊版的一個改善：舊版 `ANTHROPIC_API_KEY` 沒設會**默默** fallback 到本機登入過的 `claude` session 憑證，把請求送去真的 Anthropic API 而不是你設定的 endpoint，而且不報錯（舊 config 要刻意送空字串把這條路堵死）。現在 provider client 自己會擋。
2. **`provider="custom"` 沒有預設 model**，`LLM_MODEL` 是必填。對一個未知的 host 猜 model 名字沒有意義，在建構時明確報錯比跑到一半收到 gateway 的 404 好。

---

## 3. 快取與測試

每個 `get_*()` 都是 `lru_cache` 的 process-lifetime singleton——呼叫端可以在每個 request 讀它，不用擔心重複解析環境變數。

測試要改 env var 時用 `reset_settings_cache()` 讓下一次 `get_*()` 重新讀。production 程式碼不呼叫它。

---

## 4. 已知限制

1. **`db.py` / `minio.py` 是佔位**，沒有任何程式碼在用它們。它們存在是為了示範 pattern（以及讓「新增一個服務要抄哪個檔案」有答案）。
2. **沒有設定驗證的整合測試。** `test_config.py` 驗的是「provider 預設值、env var 覆蓋、chat model 建出來的形狀」，不會真的連線——所以「這個 base_url 真的活著」這件事只有在真的跑一次 agent 時才會知道。
3. **`temperature` 是唯一暴露出來的取樣參數。** top_p、max_tokens 之類的要調就得改 `build_chat_model()`。目前沒有需要，但這是刻意的簡化不是疏漏。
