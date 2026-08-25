# deep-research-agent

A **deep research agent** built on LangChain's
[`deepagents`](https://github.com/langchain-ai/deepagents). It takes an
open-ended question, decides for itself how deep to dig, cross-references
every source it has, and answers with the evidence attached. It is servable
over **A2A** (with streaming) and runnable on a **cron-like schedule**.

It is not tied to a subject. Two deployment shapes, one agent:

| | Known task, on a schedule | Unknown task, from a user |
|---|---|---|
| Example | the weekly supply-chain sweep | someone types any question |
| Built as | `build_mesh(domain=supply_chain.from_fixtures)` | `build_mesh(domain=None)` |
| Adds | BOM/news sources, a `company-investigator`, a severity rubric | nothing |
| Entry point | `serving/scheduler.py`, `cli/report.py` | A2A, `cli/ask.py` |

**`domain=None` is a complete configuration, not a degraded one.** Everything
that makes it good at research lives in `core/` and is present either way: a
shared scratchpad so a wide sweep does not blow up the lead's context, a
read-only `fact-checker` that must return PASS before anything publishes, an
explicit stopping check before drafting, and contradictions surfaced rather
than silently resolved (`DESIGN.md` §4). A domain only ever *adds*, and cannot
loosen a rule — `DESIGN.md` §3.4.

Its data sources are peers: the **internal wiki** (always mounted), whatever a
**domain** brings, and any **MCP servers** you point it at. The wiki gets its
own module only because it is the one source with an authorization model to
enforce.

Which tools a subagent may use is decided by **capability declared on the
tool** (`lookup` / `search` / `mutating`), not by a hand-written list of names
— with domains and MCP servers a name list can never be complete, and an
undeclared tool is treated as mutating. `DESIGN.md` §3.5.

References are checked by a **parser, not a model** (`DESIGN.md` §4.1c). The
`fact-checker` asks *does the source say this*; `check_references` asks *is a
reference attached, in the agreed shape*. And the check is not a prompt
instruction the model can skip: passing it is what unlocks `wiki_write_page`,
the same way missing `source_refs` already blocks a publish. Every document
the run loads is kept in a per-request store, so a checker can say "the loaded
copy of this source has no date, so it cannot be cited in the required form".

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
  __main__.py      ★ python -m deep_research_agent <ask|report|serve|check>
  cli/             ★ the entry points — each also runnable on its own
  protocol.py      the contract every agent speaks (no agent-framework dependency)
  capabilities.py  ★ lookup / search / mutating — what a tool may do, declared
  mesh.py          agent-as-tool adapter + registry
  runtime.py       DeepAgent — thin shell over deepagents/LangGraph, config-driven
  principals.py    service_principal() vs user_principal() — who triggers a run
  assembly.py      wiring; the only module that knows about all the others
  mcp.py           ★ MCP servers as a data source (off unless configured)
  observability.py ★ Langfuse tracing: tool calls, MCP calls, subagents
  core/            ★ the agent, minus any subject matter
    prompt.py        method, evidence rules, stopping check, verification (assembled)
    subagents.py     general-purpose + fact-checker + reference-checker; none can publish
    references.py    ★ is every claim referenced, in our format? (a parser)
    reference_tools.py ★ binds it to the run's document store + the publish gate
    domain.py        ResearchDomain / Specialist — what a subject pack may add
    agent.py         build_research_agent(domain=None)
  domains/         ★ subject packs; optional, additive, cannot loosen a rule
    supply_chain/    BOM + news sources, company-investigator, severity rubric
  config/
    base.py          BaseConfig — every IO-service config inherits this
    llm.py           ★ which chat model the agent runs on (any LangChain provider)
    mcp.py           ★ which MCP servers to connect
    langfuse.py      ★ tracing — off unless both keys are set
    paths.py         where skills and fixture data live (found automatically in a checkout)
    db.py            placeholder, same pattern, nothing uses it yet
    minio.py         placeholder, same pattern, nothing uses it yet
  wiki/              always mounted — the only source with an authz model
    authz.py         namespace rules, clearance-gated namespaces, aggregation check
    backend.py        storage interface + fixture implementation
    tools.py          ★ authorized wiki tools
    coordinator.py     optional LLM synthesis layer over the same tools (opt-in, off by default)
  serving/
    a2a.py            ★ serve any DeepAgent as a streaming A2A server
    scheduler.py       ★ cron-like recurring runs
    service.py          runs both together in one process
skills/incident-report/SKILL.md   report format + severity rubric (drop new ones here)
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
(`uv run pytest`, `uv run python -m deep_research_agent report`, ...). Prefer that
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

To add a **skill** (a rubric the agent must follow every run): if you
maintain it elsewhere, point `SKILLS_PATH` at the directory holding it and
name it in `SKILLS_ENABLED` — no code change, and no copy to drift from the
original. If it belongs to this repo, drop `skills/<name>/SKILL.md` in and add
the name to the domain's `skills`. RUNBOOK §3.8, `DESIGN.md` §3.3.

A domain can define its own citation format. The supply-chain pack does: every
entry on a `Sources:` line is a markdown link — news to the article URL, a wiki
page to its route — and raw report ids stay out of the page. It ships a
`format_reference` tool that builds the exact markdown from the loaded
document, so the agent pastes rather than guesses, and its extra rules merge
into the same `check_references` call rather than becoming a second gate.
`DESIGN.md` §4.1c, `wiki/routes.py` for the (mocked) routing.

To add a **new subject** (patents, incidents, regulatory filings): add a
package under `domains/` that returns a `ResearchDomain` — a briefing, its
sources, any specialist subagents, a rubric. Nothing in `core/` changes; if
you find yourself editing `core/` to add a domain, what you are adding is
probably general and belongs there on its own merits. `DESIGN.md` §3.4.

The test suite needs no credentials at all.

## Run it

Four commands, all under one entry point:

```
python -m deep_research_agent <command>     # or the `deep-research-agent` console script
  ask       research any question — no domain, the face-to-user path
  report    the supply-chain sweep — what the schedule runs
  serve     A2A server + scheduler in one process
  check     verify configuration; exits non-zero, so it works as a deploy gate
```

Each is also importable and runnable on its own
(`python -m deep_research_agent.cli.ask "…"`), so neither route is the
privileged one. They live in the package rather than in an `examples/` folder
because none of them is an example — they are how you run this, and a
container needs them installed, not checked out.

```bash
uv run python -m deep_research_agent check                       # verify config before spending tokens
uv run python -m deep_research_agent check --llm                 # ...and prove the model endpoint works

# The scheduled deployment: a known task, with the supply-chain domain loaded.
uv run python -m deep_research_agent report --out report.md        # user-triggered; --out saves the published report
uv run python -m deep_research_agent report --scheduled            # service account: cross-division, exec roll-up
uv run python -m deep_research_agent report --dry-run              # research only
uv run python -m deep_research_agent report --reader-only          # exercise the refusal path

# The face-to-user deployment: no domain, any question, streamed as it works.
uv run python -m deep_research_agent ask "Why did our Q3 lead times slip?"
uv run python -m deep_research_agent ask --read-only "What do we know about the auth rewrite?"

uv run python -m deep_research_agent serve                       # A2A server + scheduler, same process
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

Running from outside a checkout (a container, a cron entry in another
directory) works without arguments: the project root is found by walking up
for a checkout marker and falls back to the working directory. Set
`PATHS_PROJECT_ROOT` (where `skills/` lives) and `PATHS_FIXTURES` when neither
applies.

Use `uv run python -m …` (or `.venv/bin/python -m …`). This is a `src/`
layout, so the package is not under the repo root — a bare `python3 -m
deep_research_agent` from the root reports `No module named
deep_research_agent`, meaning "that interpreter doesn't have this installed",
not that anything is broken.

Exit codes: `0` succeeded, `1` ran but did not succeed, `2` misconfigured and
never started. A missing `LLM_API_KEY` is caught before any work happens and
reported in this repo's own variable names — otherwise it surfaces as a
provider traceback telling you to set `OPENAI_API_KEY`, which is the wrong
advice here.

Each of the above needs real `LLM_API_KEY` credentials and calls the model —
see [`docs/RUNBOOK.md`](docs/RUNBOOK.md) §3 for what to expect from each one
and how to confirm it actually worked (which namespace a report landed in,
that a denied write fails the way it's supposed to, that the A2A server and
scheduler survive running together).

## Test

```bash
uv run pytest              # 326 tests, no credentials required
```

Coverage: namespace authorization, clearance-gated namespaces, the
aggregation check that stops a cross-division sweep from landing somewhere
too public (even when the write itself would otherwise be permitted), that a
caller cannot widen its own scope, that publishing without provenance is
rejected, that no subagent — including the one the framework auto-inserts —
ever receives the write tool in either deployment shape, that a domain cannot
widen that rule by asking, that the agent is complete with no domain loaded,
that the report rubric really reaches the system prompt, LLM config and
chat-model resolution, scheduler timing and failure isolation, and the A2A
executor's principal resolution, streaming progress, task lifecycle and file
artifacts — plus six integration tests driving a real HTTP round trip through
the wired app, which is the layer that caught a bug the executor unit tests
structurally could not (`DESIGN.md` §7.3).

The capability rules were checked by breaking them: dropping the `mutating()`
declaration on the write tool leaves the suite green (undeclared already fails
closed), while *mis*-declaring it as `search()` turns 7 tests red.

The MCP tests run a real MCP server subprocess (`tests/mcp_fixture_server.py`)
rather than mocking the client, since the contract with
`langchain-mcp-adapters` is the part that can actually break; skip them with
`-m "not mcp_server"`.

The suite uses stubs and never calls a model. The framework migration was
additionally driven end to end against a local OpenAI-compatible stub server
— tool-call loop, citation propagation, an SSE streaming round trip, and a
scheduler firing. See design doc §7.

## Extending

Adding a feature? Four questions, in order (`DESIGN.md` §10):

1. Is it true of research in general, or only of one subject? → `core/` if
   general, `domains/<name>/` if not. The test: does the sentence still hold
   for patent research or incident investigation?
2. Does it need authorization? → write the rule in an `authz.py`, expose it as
   a tool. Don't default to wrapping an agent around it.
3. Is the step count known in advance? → call a chat model directly if yes,
   `DeepAgent` if no.
4. Will it be called by more than one kind of principal (user / scheduled /
   third party)? → decide their grants explicitly now, the way
   `principals.py` does — don't assume the same input implies the same output.

Adding a tool? Declare what it may do — `lookup()`, `search()` or
`mutating()` from `capabilities.py`. Skipping this is safe but restrictive:
an undeclared tool is treated as mutating, so no subagent will ever see it.

Imports inside the package are **absolute** (`from deep_research_agent.wiki.authz import
WikiAuthorizer`), never relative. A test enforces it — see
`tests/test_wiring.py::TestImportStyle`.

Adding an IO service (a database, another storage backend, ...)? Copy
`config/db.py`'s shape: one file, one class inheriting `BaseConfig`, one
`env_prefix`, a cached `get_*()` getter, exported from `config/__init__.py`.
Nothing outside `config/` reads `os.environ` directly.
