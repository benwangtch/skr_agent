# skr-agent

An agent mesh: **copilot** (routing + directly-mounted wiki tools), the
**wiki** (an authorized toolset, not an agent — see the design doc for why),
and the **wiki-report agent** (the deep research agent this repo builds).

Read [`docs/design/00-architecture.md`](docs/design/00-architecture.md) first
— it explains why the wiki is a toolset rather than a coordinator agent, how
scheduled vs. user-triggered reports get different clearance through
different principals, and what nanobot's Claude-Skills support actually looks
like (checked against source, not the README).

## Layout

```
src/skr_agent/
  protocol.py      the contract every agent speaks (no Claude dependency)
  mesh.py          agent-as-tool adapter + registry
  runtime.py       DeepAgent — thin shell over the Claude Agent SDK
  principals.py    service_principal() vs user_principal() — who triggers a run
  assembly.py      wiring; the only module that knows about all the others
  copilot.py       copilot's tool surface — wiki tools mounted directly
  wiki/
    authz.py         namespace rules, clearance-gated namespaces, aggregation check
    backend.py        storage interface + fixture implementation
    tools.py          ★ the primary wiki integration: authorized tools
    coordinator.py     optional LLM synthesis layer over the same tools (opt-in, off by default)
  report/
    sources.py / tools.py / agent.py   BOM/news tools, the deep research agent
.claude/skills/wiki-report/SKILL.md    report format + severity rubric
fixtures/          4 companies, 4 articles, 5 wiki pages, 4 raw weekly reports
```

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Running the agent needs Claude Agent SDK credentials — `ANTHROPIC_API_KEY`, or
an `ant auth login` profile. The tests do not.

## Run it

```bash
python examples/run_report.py                        # user-triggered, own division's access
python examples/run_report.py --scheduled            # service account: cross-division, exec roll-up
python examples/run_report.py --dry-run              # research only
python examples/run_report.py --reader-only          # exercise the refusal path
python examples/run_report.py --ask "What is our exposure on the ASC-4400?"
```

Scheduled and on-demand runs are the same agent with a different `Principal`
(`skr_agent.principals`) — that difference is what makes a scheduled sweep see
the executive roll-up while a user only ever sees their own division. See
design doc §5.

## Test

```bash
pytest              # 69 tests, no credentials required
```

Coverage: namespace authorization, clearance-gated namespaces, the
aggregation check that stops a cross-division sweep from landing somewhere
too public (even when the write itself would otherwise be permitted), that a
caller cannot widen its own scope, that publishing without provenance is
rejected, budget propagation, and each agent's tool surface.

## Extending

Adding a feature? Three questions, in order (design doc §12):

1. Does it need authorization? → write the rule in an `authz.py`, expose it as
   a tool. Don't default to wrapping an agent around it.
2. Is the step count known in advance? → plain API call if yes, `DeepAgent` if
   no.
3. Will it be called by more than one kind of principal (user / scheduled /
   third party)? → decide their grants explicitly now, the way
   `principals.py` does — don't assume the same input implies the same output.
