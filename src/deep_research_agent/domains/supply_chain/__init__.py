"""The supply-chain domain: a known task, run on a schedule.

This is what a ``ResearchDomain`` is for. The weekly sweep asks the same shape
of question every week — which suppliers moved, does anything external touch a
part we buy, has the internal record already seen it — so the things a model
would otherwise have to rederive each run are written down once: what a BOM
company is, why aliases matter, and when to stop searching.

None of it is required for the agent to work. Drop this package and the agent
still answers open-ended questions against the wiki and any MCP servers; it
just no longer knows what a tier-1 supplier is.

Everything supply-chain-specific in this repo lives under here. That is the
test for whether the split is real: adding a second domain should mean adding
a sibling package, not editing ``core/``.
"""

from deep_research_agent.domains.supply_chain.agent import (
    BRIEFING,
    INVESTIGATOR_PROMPT,
    from_fixtures,
    supply_chain_domain,
)
from deep_research_agent.domains.supply_chain.sources import (
    Article,
    BomSource,
    Company,
    FixtureBom,
    FixtureNewsFeed,
    NewsFeed,
)

__all__ = [
    "supply_chain_domain",
    "from_fixtures",
    "BRIEFING",
    "INVESTIGATOR_PROMPT",
    "Article",
    "Company",
    "BomSource",
    "NewsFeed",
    "FixtureBom",
    "FixtureNewsFeed",
]
