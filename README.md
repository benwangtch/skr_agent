# skr-agent

An agent mesh: **copilot** (routing + directly-mounted wiki tools), the
**wiki** (an authorized toolset, not an agent — see the design doc for why),
and the **wiki-report agent** (the deep research agent this repo builds — also
servable over **A2A** and runnable on a **cron-like schedule**).

Read [`docs/design/00-architecture.md`](docs/design/00-architecture.md) first
— it explains why the wiki is a toolset rather than a coordinator agent, how
scheduled vs. user-triggered reports get different clearance through
different principals, and what nanobot's Claude-Skills support actually looks
like (checked against source, not the README).
[`docs/design/01-config-and-serving.md`](docs/design/01-config-and-serving.md)
covers the env-driven config layer, the A2A server, and the scheduler.

## Layout

```
src/skr_agent/
  protocol.py      the contract every agent speaks (no Claude dependency)
  mesh.py          agent-as-tool adapter + registry
  runtime.py       DeepAgent — thin shell over the Claude Agent SDK, config-driven
  principals.py    service_principal() vs user_principal() — who triggers a run
  assembly.py      wiring; the only module that knows about all the others
  copilot.py       copilot's tool surface — wiki tools mounted directly
  config/
    base.py          BaseConfig — every IO-service config inherits this
    llm.py           ★ which model backend the Claude Agent SDK talks to
    db.py            placeholder, same pattern, nothing uses it yet
    minio.py         placeholder, same pattern, nothing uses it yet
  wiki/
    authz.py         namespace rules, clearance-gated namespaces, aggregation check
    backend.py        storage interface + fixture implementation
    tools.py          ★ the primary wiki integration: authorized tools
    coordinator.py     optional LLM synthesis layer over the same tools (opt-in, off by default)
  report/
    sources.py / tools.py / agent.py   BOM/news tools, the deep research agent
  serving/
    a2a.py            ★ serve any DeepAgent as an A2A server
    scheduler.py       ★ cron-like recurring runs of a skill
    service.py          runs both together in one process
.claude/skills/wiki-report/SKILL.md    report format + severity rubric
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
(the default) is `LLM_API_KEY` (get one at https://openrouter.ai/keys). See
`config/llm.py` and design doc §2–3 in `01-config-and-serving.md` for why
OpenRouter's Anthropic-compatible endpoint (not its OpenAI-compatible one) is
what makes "simulate an internal host" work, and for switching to
`LLM_PROVIDER=anthropic` or a real internal host (`LLM_PROVIDER=custom`).

The test suite needs no credentials at all.

## Run it

```bash
uv run python examples/run_report.py                        # user-triggered, own division's access
uv run python examples/run_report.py --scheduled            # service account: cross-division, exec roll-up
uv run python examples/run_report.py --dry-run              # research only
uv run python examples/run_report.py --reader-only          # exercise the refusal path
uv run python examples/run_report.py --ask "What is our exposure on the ASC-4400?"

uv run python examples/run_service.py                       # A2A server + scheduler, same process
curl http://localhost:8000/.well-known/agent-card.json | jq
```

Scheduled and on-demand runs are the same agent with a different `Principal`
(`skr_agent.principals`) — that difference is what makes a scheduled sweep see
the executive roll-up while a user only ever sees their own division. See
design doc §5.

## Test

```bash
uv run pytest              # 129 tests, no credentials required
```

Coverage: namespace authorization, clearance-gated namespaces, the
aggregation check that stops a cross-division sweep from landing somewhere
too public (even when the write itself would otherwise be permitted), that a
caller cannot widen its own scope, that publishing without provenance is
rejected, budget propagation, each agent's tool surface, LLM config resolution
and env-var overrides, scheduler timing and failure isolation, and the A2A
executor's principal resolution and task lifecycle (verified against the
installed `a2a-sdk` 1.1.x API by introspection, plus one real end-to-end HTTP
round trip through the wired FastAPI app — see `01-config-and-serving.md` §4).

## Extending

Adding a feature? Three questions, in order (design doc §12):

1. Does it need authorization? → write the rule in an `authz.py`, expose it as
   a tool. Don't default to wrapping an agent around it.
2. Is the step count known in advance? → plain API call if yes, `DeepAgent` if
   no.
3. Will it be called by more than one kind of principal (user / scheduled /
   third party)? → decide their grants explicitly now, the way
   `principals.py` does — don't assume the same input implies the same output.

Adding an IO service (a database, another storage backend, ...)? Copy
`config/db.py`'s shape: one file, one class inheriting `BaseConfig`, one
`env_prefix`, a cached `get_*()` getter, exported from `config/__init__.py`.
Nothing outside `config/` reads `os.environ` directly.
