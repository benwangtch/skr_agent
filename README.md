# deep-research-agent

A **deep research agent** built on LangChain's
[`deepagents`](https://github.com/langchain-ai/deepagents). It takes open-ended
supply-chain questions, decides for itself how deep to dig, cross-references
several data sources, and publishes a sourced report. It is servable over
**A2A** (with streaming) and runnable on a **cron-like schedule**.

Research quality comes from four deliberate choices, not from the framework:
investigators write findings to a shared scratchpad so a wide sweep does not
blow up the lead's context, a read-only `fact-checker` re-reads primary
sources and must return PASS before anything publishes, an explicit stopping
check runs before drafting, and contradictions between sources get surfaced
rather than silently resolved. See `DESIGN.md` §4.

Its data sources are peers: the **bill of materials**, **external news**, the
**internal wiki**, and any **MCP servers** you point it at. The wiki gets its
own module only because it is the one source with an authorization model to
enforce.

[**`docs/design/DESIGN.md`**](docs/design/DESIGN.md) is the design doc — one
current-state reference covering the execution framework, the four choices
that make it good at research, the data sources and their authorization
model, config, A2A serving, scheduling, and the known limitations.
[`package-management.md`](docs/design/package-management.md) covers why uv
over pip.
[`docs/RUNBOOK.md`](docs/RUNBOOK.md) is the operational doc — every command to
run this locally and a step-by-step manual end-to-end verification pass (the
kind that actually calls the model, unlike the test suite below).

## Layout

```
src/deep_research_agent/
  protocol.py      the contract every agent speaks (no agent-framework dependency)
  mesh.py          agent-as-tool adapter + registry
  runtime.py       DeepAgent — thin shell over deepagents/LangGraph, config-driven
  principals.py    service_principal() vs user_principal() — who triggers a run
  assembly.py      wiring; the only module that knows about all the others
  mcp.py           ★ MCP servers as a data source (off unless configured)
  config/
    base.py          BaseConfig — every IO-service config inherits this
    llm.py           ★ which chat model the agent runs on (any LangChain provider)
    mcp.py           ★ which MCP servers to connect
    db.py            placeholder, same pattern, nothing uses it yet
    minio.py         placeholder, same pattern, nothing uses it yet
  wiki/              one data source — the only one with an authz model
    authz.py         namespace rules, clearance-gated namespaces, aggregation check
    backend.py        storage interface + fixture implementation
    tools.py          ★ authorized wiki tools
    coordinator.py     optional LLM synthesis layer over the same tools (opt-in, off by default)
  report/
    sources.py / tools.py   BOM + news data sources and their tools
    agent.py          ★ the agent itself: prompt, sources, subagents, rubric
                        (company-investigator + fact-checker)
  serving/
    a2a.py            ★ serve any DeepAgent as a streaming A2A server
    scheduler.py       ★ cron-like recurring runs
    service.py          runs both together in one process
.claude/skills/incident-report/SKILL.md   report format + severity rubric
fixtures/          4 companies, 4 articles, 5 wiki pages, 4 raw weekly reports
```

## Setup

Managed with [uv](https://docs.astral.sh/uv/) — `pyproject.toml` +
`uv.lock`, no `requirements.txt`.

```bash
uv sync --group dev      # creates .venv, installs pinned deps from uv.lock
cp .env.example .env
```

`uv run <command>` runs inside that venv without activating it
(`uv run pytest`, `uv run python examples/run_report.py`, ...). Prefer that
over `source .venv/bin/activate` so the venv can't silently drift from the
lockfile.

Adding a dependency: `uv add <package>` (runtime) or `uv add --group dev
<package>` (dev-only) — both update `pyproject.toml` and `uv.lock` together;
don't hand-edit the dependency list and re-lock separately.

Edit `.env` — the only line that needs a real value to run against OpenRouter
(the default) is `LLM_API_KEY` (get one at https://openrouter.ai/keys).

Because the agent runs on LangChain, any provider with a LangChain chat model
works. `LLM_PROVIDER=custom` + `LLM_BASE_URL` points at any internal gateway
exposing an OpenAI-compatible `/v1/chat/completions`, which is what vLLM,
LiteLLM and most corporate proxies expose — no protocol translation needed.

To let the agent call your own **MCP service**, set `MCP_URL` (and `MCP_TOKEN`
if it needs auth) — see `.env.example`. Nothing connects anywhere unless you
do. One caveat worth reading before you point it at anything sensitive: the
triggering user's identity is **not** forwarded to the MCP server, so every
call arrives as the same service account. `docs/RUNBOOK.md` §3.7 has the
verification steps; `DESIGN.md` §5.5 has the full caveat.

To add your own **skill** (a rubric the agent must follow every run): drop
`.claude/skills/<name>/SKILL.md` in and add the name to `DEFAULT_SKILLS` in
`src/deep_research_agent/report/agent.py`. RUNBOOK §3.8, `DESIGN.md` §3.3.

The test suite needs no credentials at all.

## Run it

```bash
uv run python examples/run_report.py --out report.md        # user-triggered; --out saves the published report
uv run python examples/run_report.py --scheduled            # service account: cross-division, exec roll-up
uv run python examples/run_report.py --dry-run              # research only
uv run python examples/run_report.py --reader-only          # exercise the refusal path
uv run python examples/run_report.py --ask "What is our exposure on the ASC-4400?"   # one question, no sweep

uv run python examples/run_service.py                       # A2A server + scheduler, same process
curl http://localhost:8000/.well-known/agent-card.json | jq

# Send it a task. The A2A-Version header and the body shape go together:
# omit the header and the SDK reads the request as A2A 0.3. Swap SendMessage
# for SendStreamingMessage to get incremental progress over SSE.
curl http://localhost:8000/ \
  -H 'Content-Type: application/json' -H 'A2A-Version: 1.0' \
  -d '{"jsonrpc":"2.0","id":"1","method":"SendMessage","params":{
        "message":{"messageId":"m1","role":"ROLE_USER",
                   "parts":[{"text":"our ASC-4400 exposure?"}]}}}'
```

Scheduled and on-demand runs are the same agent with a different `Principal`
(`deep_research_agent.principals`) — that difference is what makes a scheduled sweep see
the executive roll-up while a user only ever sees their own division. See
[`DESIGN.md`](docs/design/DESIGN.md) §5.3.

Each of the above needs real `LLM_API_KEY` credentials and calls the model —
see [`docs/RUNBOOK.md`](docs/RUNBOOK.md) §3 for what to expect from each one
and how to confirm it actually worked (which namespace a report landed in,
that a denied write fails the way it's supposed to, that the A2A server and
scheduler survive running together).

## Test

```bash
uv run pytest              # 171 tests, no credentials required
```

Coverage: namespace authorization, clearance-gated namespaces, the
aggregation check that stops a cross-division sweep from landing somewhere
too public (even when the write itself would otherwise be permitted), that a
caller cannot widen its own scope, that publishing without provenance is
rejected, that a subagent never receives the write tool, that the report
rubric really reaches the system prompt, LLM config and chat-model
resolution, scheduler timing and failure isolation, and the A2A executor's
principal resolution, streaming progress, task lifecycle and file artifacts —
plus six integration tests driving a real HTTP round trip through the wired
app, which is the layer that caught a bug the executor unit tests structurally
could not (`DESIGN.md` §7.3).

The MCP tests run a real MCP server subprocess (`tests/mcp_fixture_server.py`)
rather than mocking the client, since the contract with
`langchain-mcp-adapters` is the part that can actually break; skip them with
`-m "not mcp_server"`.

The suite uses stubs and never calls a model. The framework migration was
additionally driven end to end against a local OpenAI-compatible stub server
— tool-call loop, citation propagation, an SSE streaming round trip, and a
scheduler firing. See design doc §7.

## Extending

Adding a feature? Three questions, in order (`DESIGN.md` §10):

1. Does it need authorization? → write the rule in an `authz.py`, expose it as
   a tool. Don't default to wrapping an agent around it.
2. Is the step count known in advance? → call a chat model directly if yes,
   `DeepAgent` if no.
3. Will it be called by more than one kind of principal (user / scheduled /
   third party)? → decide their grants explicitly now, the way
   `principals.py` does — don't assume the same input implies the same output.

Imports inside the package are **absolute** (`from deep_research_agent.wiki.authz import
WikiAuthorizer`), never relative. A test enforces it — see
`tests/test_wiring.py::TestImportStyle`.

Adding an IO service (a database, another storage backend, ...)? Copy
`config/db.py`'s shape: one file, one class inheriting `BaseConfig`, one
`env_prefix`, a cached `get_*()` getter, exported from `config/__init__.py`.
Nothing outside `config/` reads `os.environ` directly.
