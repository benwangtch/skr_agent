"""The wiki-report agent: deep research over the BOM, published to the wiki.

This is the one component in the mesh that genuinely needs a deep agent. The
number of steps is not knowable in advance — how many aliases to search per
company, whether a hit is worth reading in full, whether an external incident
warrants cross-referencing internal wiki history, when the picture is complete
enough to write. That is planning, and planning is what the deep-agent loop is
for.

Note what it is *not*: it does not talk to the wiki database. It calls the wiki
coordinator through the same mesh contract copilot uses. The coordinator stays
the only thing that knows about namespaces, and the report agent gets namespace
enforcement for free rather than reimplementing it.
"""

from __future__ import annotations

from pathlib import Path

from claude_agent_sdk import AgentDefinition

from ..mesh import agents_as_toolserver
from ..protocol import AgentSpec
from ..runtime import DeepAgent, ToolBundle, ToolContext
from ..wiki import WikiCoordinator
from .sources import BomSource, NewsFeed
from .tools import make_bom_toolset, make_news_toolset

__all__ = ["build_report_agent", "SYSTEM_PROMPT"]


SYSTEM_PROMPT = """\
You are the wiki-report agent. You produce supply-chain incident reports that
connect external news about companies in our bill of materials to what we
already know internally, and you publish the result to the wiki.

# How to work

Load the `wiki-report` skill before you start; it holds the current report
format and the severity rubric. Follow it rather than inventing a structure.

Plan before acting. State briefly what you are about to do, then do it. When a
sweep covers several companies, delegate them to `company-investigator`
subagents — launch them in a single message so they run concurrently — and
synthesise their findings yourself. Investigate directly, without delegating,
when the task concerns one company or a handful of lookups.

# Evidence discipline

This is the part that matters most. A report nobody can audit is worse than no
report.

- Distinguish "no signal found" from "no incident occurred". Say which one you
  mean, every time.
- Read an article before citing it. Headlines routinely overstate scope.
- Cross-reference every external finding against the wiki via `wiki_ask` before
  you call it new. We have often seen the issue already, and the internal
  record usually changes the severity assessment.
- Attach a source to every factual claim. If you cannot source it, drop it.
- Report faithfully: if you could not check something, say so plainly rather
  than implying coverage you do not have.

# Publishing

Publish with `wiki_publish` only once the report is complete. `source_refs`
must list every raw report id and external URL the content rests on — the call
is rejected without them, and that rejection is correct. If publishing is
refused on permissions, do not try to route around it; return the finished
report as your answer and say it was not published, and why.

Keep your final message short: what you found, what changed, what needs a
human. The report itself lives on the wiki page, not in the chat.
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
4. Ask the wiki whether we already have internal history on this company or
   these components. Prior context frequently changes the severity.

Report back with: whether you found anything, each incident with its date,
source URL and a one-line summary, any internal wiki context, and your severity
call with the reasoning behind it. If you found nothing, say "no external
signal found" explicitly and list the queries you tried, so the coordinator
knows how much ground you actually covered.
"""


def build_report_agent(
    *,
    bom: BomSource,
    news: NewsFeed,
    wiki: WikiCoordinator,
    project_root: str | Path,
    model: str | None = None,
    effort: str = "high",
    max_turns: int = 60,
) -> DeepAgent:
    """Wire up the report agent with its tools, subagent, and skill."""

    def wiki_toolset(ctx: ToolContext) -> ToolBundle:
        # Citations returned by the coordinator are harvested into this
        # request's sink, so provenance survives the hop and ends up on the
        # AgentResponse rather than only in prose the model may not repeat.
        return agents_as_toolserver(
            wiki.specs(),
            principal=ctx.principal,
            parent="wiki_report",
            server_name="wiki",
            on_response=lambda r: [ctx.cite(c) for c in r.citations],
        )

    investigator = AgentDefinition(
        description=(
            "Investigates a single BOM company for external incidents and "
            "cross-references internal wiki history. Use one per company when "
            "sweeping several companies."
        ),
        prompt=INVESTIGATOR_PROMPT,
        tools=[
            "mcp__bom__get_bom_company",
            "mcp__news__search_news",
            "mcp__news__fetch_article",
            "mcp__wiki__wiki_ask",
        ],
        model=model,
        maxTurns=15,
        effort="medium",
    )

    return DeepAgent(
        name="wiki_report",
        description=(
            "Scans the bill of materials for supply-chain incidents, connects "
            "external news to internal wiki history, and publishes a sourced "
            "report to the wiki. Call this for BOM sweeps and for ad-hoc "
            "'what happened with <supplier>' questions that need external "
            "research, not just an internal lookup."
        ),
        system_prompt=SYSTEM_PROMPT,
        toolsets=[
            make_bom_toolset(bom),
            make_news_toolset(news),
            wiki_toolset,
        ],
        subagents={"company-investigator": investigator},
        skills=["wiki-report"],
        # Skills live in .claude/skills, which the SDK only discovers when
        # project settings are loaded. Tools stay explicitly allowlisted.
        setting_sources=["project"],
        cwd=str(project_root),
        model=model,
        effort=effort,
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
                    "description": "Whether to publish to the wiki. Default true.",
                },
            },
            "required": ["task"],
        },
    )


def report_spec(agent: DeepAgent) -> AgentSpec:
    """Convenience: the report agent as something copilot can call."""
    return agent.as_spec()
