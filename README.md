# skr-agent

An agent mesh: **copilot** (routing), **wiki coordinator** (mocked — another
team owns the real one), and the **wiki-report agent** (the deep research agent
this repo actually builds).

Read [`docs/design/00-architecture.md`](docs/design/00-architecture.md) first —
it explains why the pieces are split the way they are.

## Layout

```
src/skr_agent/
  protocol.py      the contract every agent speaks (no Claude dependency)
  mesh.py          agent-as-tool adapter + registry
  runtime.py       DeepAgent — thin shell over the Claude Agent SDK
  assembly.py      wiring; the only module that knows about all the others
  copilot.py       copilot's tool surface
  wiki/            authz + storage interface + MOCK coordinator
  report/          BOM/news sources, tools, and the deep research agent
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
python examples/run_report.py                        # sweep the critical tier, publish
python examples/run_report.py --dry-run              # research only
python examples/run_report.py --reader-only          # exercise the refusal path
python examples/run_report.py --ask "What is our exposure on the ASC-4400?"
```

The last one goes through copilot, which routes to `wiki_ask` or `wiki_report`
depending on whether the question needs external research.

## Test

```bash
pytest              # 45 tests, no credentials required
```

They cover the seams rather than the model: namespace authorization, that a
caller cannot widen its own scope, that publishing without provenance is
rejected, that budgets shrink on delegation, and that each agent's tool surface
is what the design says it is.

## Replacing the mock

`src/skr_agent/wiki/coordinator.py` is a stand-in. Swap it in
`assembly.py::build_mesh` and nothing above changes. The interface it has to
satisfy — and the three non-negotiable rules — are in
[§10 of the design doc](docs/design/00-architecture.md).
`tests/test_wiki_authz.py` can be pointed at the real implementation as a
conformance suite.
