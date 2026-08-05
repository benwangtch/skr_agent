"""skr agent: the deep research agent this repo builds.

It answers open-ended supply-chain questions by working across every source it
has — the bill of materials, external news, and the internal wiki — and
publishing what it finds. Each of those is one data source; none of them is
the point. The point is the research loop: how many aliases to search per
company, whether a hit is worth reading in full, whether an external incident
warrants cross-referencing internal history, when the picture is complete
enough to write. That is planning, and planning is what a deep agent is for.

Sources that need authorization enforce it in their own tool layer, against
the principal that triggered the run, so this agent never learns the rules.
What a report contains therefore depends on who asked for it: the scheduled
service account sees across divisions, a user sees their own. See
``skr_agent.principals``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool

from ..protocol import AgentSpec
from ..runtime import DeepAgent, ToolContext
from ..wiki.authz import WikiAuthorizer
from ..wiki.backend import WikiBackend
from ..wiki.tools import make_wiki_toolset
from .sources import BomSource, NewsFeed
from .tools import make_bom_toolset, make_news_toolset

__all__ = ["build_skr_agent", "SYSTEM_PROMPT", "AGENT_NAME"]

AGENT_NAME = "skr_agent"

INVESTIGATOR_TOOLS = (
    "get_bom_company",
    "search_news",
    "fetch_article",
    "wiki_search",
    "wiki_read_page",
)


SYSTEM_PROMPT = """\
You are skr agent, a deep research agent. You investigate open-ended questions
about our supply chain by pulling together everything available to you, and you
publish sourced findings.

# Your sources

Treat these as complementary; a finding from one is rarely enough on its own.

- **Bill of materials** (`list_bom_companies`, `get_bom_company`) — which
  companies we depend on, for which components, and under what aliases.
- **External news** (`search_news`, `fetch_article`) — what happened out in
  the world.
- **Internal wiki** (`wiki_search`, `wiki_read_page`, `wiki_write_page`) —
  what we already knew, and where finished reports get published.

# How to work

Plan before acting. State briefly what you are about to do, then do it. Use
your todo list to track a multi-company sweep so nothing is silently dropped.

When a sweep covers several companies, delegate them to `company-investigator`
subagents via the `task` tool — launch them in a single message so they run
concurrently — and synthesise their findings yourself. Investigate directly,
without delegating, when the task concerns one company or a handful of
lookups.

Your access to any source is scoped to whoever triggered this run. If a search
returns nothing, that may mean the record exists somewhere you cannot read —
say "no record visible to this run", not "no record exists". Never speculate
about the contents of something you were refused.

# Evidence discipline

This is the part that matters most. A report nobody can audit is worse than no
report.

- Distinguish "no signal found" from "no incident occurred". Say which one you
  mean, every time.
- Read an article before citing it. Headlines routinely overstate scope.
- Cross-reference every external finding against what we already know before
  calling it new. We have often seen the issue already, and the internal
  record usually changes the severity assessment. Search previews are
  truncated — read the page.
- Attach a source to every factual claim. If you cannot source it, drop it.
- Report faithfully: if you could not check something, say so plainly rather
  than implying coverage you do not have.

# Publishing

Publish with `wiki_write_page` only once the report is complete. `source_refs`
must list every raw report id and external URL the content rests on — the call
is rejected without them, and that rejection is correct.

A report drawing on more than one division is a cross-division artefact and
belongs in a clearance-gated namespace. If the write is refused for that
reason, the fix is to publish to the gated namespace, not to drop the sources
that triggered the check.

If publishing is refused on permissions, do not try to route around it: return
the finished report as your answer and say plainly that it was not published,
and why.

Keep your final message short: what you found, what changed, what needs a
human. The report itself lives on the published page, not in the chat.
"""


INVESTIGATOR_PROMPT = """\
You investigate one company for supply-chain incidents.

Work the problem in this order, and do not stop at the first search:

1. Look up the company's BOM entry to get its aliases and the components we
   source from it. Aliases matter — incidents are frequently reported under a
   parent company or brand name.
2. Search news under the legal name and each alias. One query is rarely enough.
3. Fetch the full text of anything that looks material. Judge from the body,
   not the headline.
4. Search internal records for history on this company or these components,
   and read in full anything that looks relevant. Prior context frequently
   changes the severity.

Report back with: whether you found anything, each incident with its date,
source URL and a one-line summary, any internal context, and your severity
call with the reasoning behind it. If you found nothing, say "no external
signal found" explicitly and list the queries you tried, so the caller knows
how much ground you actually covered.
"""


def build_skr_agent(
    *,
    bom: BomSource,
    news: NewsFeed,
    wiki_backend: WikiBackend,
    wiki_authz: WikiAuthorizer,
    project_root: str | Path,
    model: str | None = None,
    max_turns: int = 60,
) -> DeepAgent:
    """Wire up skr agent with its sources, subagent, and report rubric."""

    def subagents(ctx: ToolContext, tools: dict[str, BaseTool]) -> list[dict[str, Any]]:
        # Read-only by construction: the investigator is handed a subset that
        # simply does not contain the write tool, so it cannot publish even if
        # it decides it should.
        return [
            {
                "name": "company-investigator",
                "description": (
                    "Investigates a single BOM company for external incidents and "
                    "cross-references internal history. Use one per company when "
                    "sweeping several companies."
                ),
                "system_prompt": INVESTIGATOR_PROMPT,
                "tools": [tools[n] for n in INVESTIGATOR_TOOLS if n in tools],
            }
        ]

    return DeepAgent(
        name=AGENT_NAME,
        description=(
            "Deep research over the supply chain: scans the bill of materials, "
            "connects external news to internal history, and publishes a sourced "
            "report. Call this for BOM sweeps and for ad-hoc 'what happened with "
            "<supplier>' questions that need real research, not just a lookup."
        ),
        system_prompt=SYSTEM_PROMPT,
        toolsets=[
            make_bom_toolset(bom),
            make_news_toolset(news),
            make_wiki_toolset(wiki_backend, wiki_authz, writable=True),
        ],
        subagents=subagents,
        skills=["incident-report"],
        project_root=project_root,
        model=model,
        max_turns=max_turns,
        input_schema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "What to investigate and report on.",
                },
                "tier": {
                    "type": "string",
                    "description": "Optional BOM tier to restrict the sweep to.",
                },
                "publish": {
                    "type": "boolean",
                    "description": "Whether to publish the finished report. Default true.",
                },
            },
            "required": ["task"],
        },
    )


def report_spec(agent: DeepAgent) -> AgentSpec:
    """Convenience: skr agent as something another agent can call."""
    return agent.as_spec()
