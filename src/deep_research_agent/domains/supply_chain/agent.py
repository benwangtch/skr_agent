"""Supply-chain knowledge, expressed as a ``ResearchDomain``.

Three things that a general research agent cannot know and should not have to
guess, and which are worth writing down because the same job runs every week:

* **The unit of investigation is a BOM company**, and one company can appear in
  the world under several names. Searching the legal name alone is the single
  most common way a real incident is missed.
* **A sweep is wide.** Twenty companies investigated in the lead's own context
  window will summarize away exactly the detail the report needs, so each one
  goes to a specialist that writes the long version to a file.
* **Severity is a rubric, not a judgement call** — that is the
  ``incident-report`` skill, inlined into the prompt on every run.
"""

from __future__ import annotations

from pathlib import Path

from deep_research_agent.core.domain import ResearchDomain, Specialist
from deep_research_agent.core.prompt import FINDINGS_DIR
from deep_research_agent.domains.supply_chain.sources import (
    BomSource,
    FixtureBom,
    FixtureNewsFeed,
    NewsFeed,
)
from deep_research_agent.domains.supply_chain.tools import make_bom_toolset, make_news_toolset

__all__ = ["supply_chain_domain", "from_fixtures", "BRIEFING", "INVESTIGATOR_PROMPT"]


BRIEFING = """\
# What you are researching

You investigate our supply chain: which companies we depend on, what happened
to them, and what that means for the parts we buy.

Alongside the internal wiki you have two domain sources:

- **Bill of materials** (`list_bom_companies`, `get_bom_company`) — which
  companies we depend on, for which components, and under what aliases.
- **External news** (`search_news`, `fetch_article`) — what happened out in
  the world.

The unit of investigation is a BOM company. Two things about that are worth
knowing before you start:

- **Aliases matter.** Incidents are routinely reported under a parent company
  or a brand name, not the legal entity in our BOM. Look up the entry first,
  then search each alias.
- **A sweep is wide.** When the task covers several companies, give each one to
  a `company-investigator` subagent and launch them in a single message so they
  run concurrently. Investigate directly, without delegating, when the task
  concerns one company or a handful of lookups.\
"""


INVESTIGATOR_PROMPT = f"""\
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

Vary your queries deliberately. A single phrasing finds a single slice of what
is out there, so work through: the legal name, each alias, the parent or brand
name, the component part numbers we buy, and incident words (recall, outage,
fire, strike, sanction, insolvency, breach). Stop when new queries stop
returning new material, not when the first one returns something.

# Write your findings to a file

Before you reply, write everything you found to `{FINDINGS_DIR}/<company_id>.md`.
Include every source URL, the internal pages you read, the queries you ran,
and your severity reasoning — the long version, not a summary.

Then reply with a SHORT summary: severity, the one-line reason, and the file
path you wrote. The file is the record; your reply is the pointer. This is
what keeps a twenty-company sweep from overflowing the lead agent's context.

If you found nothing, still write the file, and say "no external signal found"
explicitly in both the file and your reply, listing the queries you tried, so
the caller can tell "checked, clean" from "not checked".\
"""


INVESTIGATOR = Specialist(
    name="company-investigator",
    description=(
        "Investigates a single BOM company for external incidents and "
        "cross-references internal history. Use one per company when sweeping "
        "several companies."
    ),
    system_prompt=INVESTIGATOR_PROMPT,
    # Named explicitly rather than "every read tool": this subagent has one
    # job, and handing it unrelated sources invites it to wander. The core
    # still filters these through the read-only check, so naming a tool here
    # cannot widen what a subagent may do.
    tools=(
        "get_bom_company",
        "search_news",
        "fetch_article",
        "wiki_search",
        "wiki_read_page",
    ),
)


def supply_chain_domain(*, bom: BomSource, news: NewsFeed) -> ResearchDomain:
    """The domain, given its two data sources.

    Sources are injected rather than constructed here so the fixture-backed
    demo and a real BOM service are the same code path — see ``assembly.py``.
    """
    return ResearchDomain(
        name="supply-chain",
        summary=(
            "Deep research over the supply chain: scans the bill of materials, "
            "connects external news to internal history, and publishes a sourced "
            "report. Call this for BOM sweeps and for ad-hoc 'what happened with "
            "<supplier>' questions that need real research, not just a lookup."
        ),
        briefing=BRIEFING,
        toolsets=(make_bom_toolset(bom), make_news_toolset(news)),
        specialists=(INVESTIGATOR,),
        skills=("incident-report",),
        inputs={
            "tier": {
                "type": "string",
                "description": "Optional BOM tier to restrict the sweep to.",
            }
        },
    )


def from_fixtures(fixtures: str | Path) -> ResearchDomain:
    """The domain backed by this repo's fixture data. What the examples use."""
    fixtures = Path(fixtures)
    return supply_chain_domain(
        bom=FixtureBom.from_fixtures(fixtures),
        news=FixtureNewsFeed.from_fixtures(fixtures),
    )
