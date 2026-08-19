# 套件管理——為什麼換成 uv

狀態：實作完成
範圍：`pyproject.toml`、`uv.lock`、`.python-version`

---

## 0. 決策摘要

| 問題 | 決定 | 一句話理由 |
|---|---|---|
| 用什麼管套件？ | **uv**（不是 pip + venv） | team 慣用；`uv.lock` 讓安裝可重現，pip 沒有內建這件事 |
| dev 依賴放哪裡？ | `[dependency-groups]`（PEP 735），不是 `[project.optional-dependencies]` | 這是 uv 原生慣例（`uv add --group dev`），extras 是給「使用這個套件的人可選裝的功能」用的，dev 依賴不是那個語意 |
| Build backend 要不要換？ | **不用換，維持 setuptools** | uv 對 build backend 沒有偏好，原本的 `[tool.setuptools.packages.find]` 照樣能用，換成 hatchling 之類的沒有額外好處 |
| Python 版本怎麼釘？ | `.python-version` = `3.11` | uv 慣例；跟 `pyproject.toml` 的 `requires-python = ">=3.11"` 是兩件事——後者是「這個套件支援的範圍」，前者是「這個專案開發時用哪一個」 |

---

## 1. 從 pip + venv 換成 uv，實際改了什麼

**之前**：`python3 -m venv .venv && pip install -e ".[dev]"`。每次裝的版本取決於當下 PyPI 上有什麼，沒有 lockfile，兩台機器跑起來可能裝到不同的次版本。

**之後**：

```bash
uv sync --group dev
```

一行完成建 venv、解析、安裝，而且是照 `uv.lock` 裡釘死的版本裝——這是跟 pip 最大的差別：pip 的 `requirements.txt`（就算有）也只是「一份清單」，沒有內建的鎖檔機制去保證兩次安裝拿到位元組相同的依賴樹；uv 有。

```
pyproject.toml     依賴的「意圖」（版本範圍、哪些是必要/哪些是 dev-only）
uv.lock             依賴的「事實」（實際解析出來、釘死版本的完整依賴樹，含 transitive deps）
.python-version     這個專案開發用哪個 Python（uv 用它決定要不要幫你抓一個對應版本的直譯器）
```

三個檔案職責不重疊。`uv.lock` 是機器產生的，不手動改；要改依賴，改 `pyproject.toml` 或直接用 `uv add`，然後讓 uv 重新算 lockfile。

---

## 2. dev 依賴為什麼搬到 `[dependency-groups]`

原本：

```toml
[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio>=0.23", "httpx>=0.27"]
```

`optional-dependencies` 這個欄位的原始語意是「這個套件的**使用者**可以選擇裝的額外功能」（例如某個套件的 `[redis]` extra，代表「如果你要用到 Redis 整合就多裝這些」）。dev 依賴不是這個東西——它們是「開發這個套件的人需要的工具」，跟這個套件本身要不要被別人 `pip install deep-research-agent[dev]` 沒有關係，事實上也不會有人這樣裝。

`[dependency-groups]`（PEP 735，uv 原生支援）語意才是對的：

```toml
[dependency-groups]
dev = ["pytest>=8.0", "pytest-asyncio>=0.23", "httpx>=0.27"]
```

對應指令：

```bash
uv sync --group dev              # 裝進去
uv add --group dev <package>     # 加一個新的 dev 依賴
```

跟 `uv add <package>`（沒有 `--group`，加進 `[project.dependencies]`，正式執行期依賴）分開，職責清楚。

---

## 3. 日常操作

```bash
uv sync --group dev          # 第一次設置，或 pyproject.toml/uv.lock 有變動後同步
uv run pytest                # 在 venv 裡跑指令，不用 source activate
uv run python examples/run_report.py

uv add fastapi                 # 加執行期依賴
uv add --group dev ruff        # 加開發依賴
uv lock --upgrade-package a2a-sdk   # 只升級某一個套件，其他維持鎖住
```

**不要手動改 `uv.lock`，也不要繞過 `uv add` 直接改 `pyproject.toml` 的版本號再自己跑 `pip install`。** 這樣兩個檔案會不同步，回到「不知道到底裝的是什麼版本」的舊問題——這正是換 uv 想解決的事。

`uv run <command>` 優先於 `source .venv/bin/activate`：前者每次都會先確認 venv 跟 lockfile 一致（不一致會自動同步或報錯），後者只是把 venv 塞進 PATH，venv 內容跟 lockfile 是否一致完全不會被檢查，容易在改完 `pyproject.toml` 忘記重新 sync 的情況下裝到 stale 的依賴而沒發現。

---

## 4. 已知限制

1. **`uv.lock` 沒有跨平台的 CI 驗證。** 目前只在這個 sandbox（Linux）產生過；如果 team 有人在 macOS/Windows 開發，第一次 `uv sync` 時 uv 會自動幫該平台解析對應的 wheel，但如果某個依賴只有原始碼、沒有預編譯 wheel（例如某些含 C extension 的套件在特定平台），可能要另外處理，這裡沒有驗證過。
2. **沒有設 CI 去檢查 `pyproject.toml` 跟 `uv.lock` 是否同步。** 理論上有人可能手動改了 `pyproject.toml` 卻忘記 `uv lock`，導致兩者不一致——`uv sync --locked`（拒絕自動更新 lockfile，不一致就直接失敗）是這個問題的標準解法，值得之後接進 CI，目前還沒做。
