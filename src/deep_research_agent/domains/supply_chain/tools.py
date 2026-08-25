"""Toolset factories for the supply-chain domain's BOM and news data sources.

A factory takes a ``ToolContext`` (principal + citation sink) and returns tools
bound to it. This is why they are functions rather than module-level
singletons: the principal has to be baked into the closure so the model cannot
supply it.

Every tool is wrapped in a capability declaration — ``lookup`` for "fetch this
named thing", ``search`` for "find things matching a query". That is what lets
the core hand the right subset to a subagent without knowing any of these tool
names, and it is why the fact-checker gets ``get_bom_company`` and
``fetch_article`` but not ``search_news``. See ``core.capabilities``.
"""

from __future__ import annotations

from dataclasses import asdict

from langchain_core.tools import BaseTool, StructuredTool

from deep_research_agent.capabilities import lookup, search
from deep_research_agent.protocol import Citation
from deep_research_agent.runtime import ToolBundle, ToolContext
from deep_research_agent.domains.supply_chain.sources import BomSource, NewsFeed

__all__ = ["make_bom_toolset", "make_news_toolset"]


def make_bom_toolset(bom: BomSource):
    """Read-only access to the bill of materials."""

    def factory(ctx: ToolContext) -> ToolBundle:
        async def list_bom_companies(tier: str | None = None, **_ignored: object) -> str:
            companies = bom.list_companies(tier)
            lines = [
                f"- {c.company_id} | {c.name} | tier={c.tier} | "
                f"components={', '.join(c.components) or 'n/a'}"
                for c in companies
            ]
            body = "\n".join(lines) or "(BOM is empty)"
            return f"{len(companies)} companies:\n{body}"

        async def get_bom_company(company_id: str, **_ignored: object) -> str:
            company = bom.get_company(company_id)
            if company is None:
                return f"Error: no BOM entry for {company_id!r}."
            ctx.cite(
                Citation(
                    kind="internal_record",
                    ref=f"bom/{company.company_id}",
                    title=f"BOM entry: {company.name}",
                )
            )
            return str(asdict(company))

        tools: list[BaseTool] = [
            # Discovers: it is how you find out which companies exist at all.
            search(StructuredTool.from_function(
                coroutine=list_bom_companies,
                name="list_bom_companies",
                description=(
                    "List companies in the bill of materials. Call this first when asked "
                    "to scan or sweep the BOM, so you know the full set before deciding "
                    "where to dig. Filter by tier to prioritise critical suppliers."
                ),
                args_schema={
                    "type": "object",
                    "properties": {
                        "tier": {
                            "type": "string",
                            "description": "Optional tier filter, e.g. 'critical'.",
                        }
                    },
                },
            )),
            lookup(StructuredTool.from_function(
                coroutine=get_bom_company,
                name="get_bom_company",
                description=(
                    "Look up one company's BOM entry: legal name, aliases, tier, and which "
                    "components we source from them. Use the aliases when searching news — "
                    "incidents are often reported under a parent or brand name."
                ),
                args_schema={
                    "type": "object",
                    "properties": {"company_id": {"type": "string"}},
                    "required": ["company_id"],
                },
            )),
        ]
        return tools

    return factory


def make_news_toolset(feed: NewsFeed):
    """External incident search. Swap the feed for real search in production."""

    def factory(ctx: ToolContext) -> ToolBundle:
        async def search_news(query: str, limit: int = 5, **_ignored: object) -> str:
            articles = feed.search(query, limit=int(limit))
            if not articles:
                return (
                    f"No articles matched {query!r}. Record this as 'no external "
                    "signal found', not as 'no incident occurred'."
                )
            return "\n".join(
                f"- {a.published} | {a.source} | {a.title}\n  {a.url}\n  {a.summary}"
                for a in articles
            )

        async def fetch_article(url: str, **_ignored: object) -> str:
            article = feed.fetch(url)
            if article is None:
                return f"Error: could not fetch {url!r}."
            ctx.cite(
                Citation(
                    kind="external_url",
                    ref=article.url,
                    title=article.title,
                    snippet=article.summary[:200],
                )
            )
            return (
                f"# {article.title}\n{article.source} — {article.published}\n\n"
                f"{article.summary}\n\nCompanies mentioned: {', '.join(article.companies)}"
            )

        tools: list[BaseTool] = [
            search(StructuredTool.from_function(
                coroutine=search_news,
                name="search_news",
                description=(
                    "Search external news for incidents involving a company — outages, "
                    "recalls, breaches, insolvency, sanctions, natural disasters affecting "
                    "sites. Search each company under its name and its aliases; a single "
                    "query per company is usually not enough."
                ),
                args_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                    },
                    "required": ["query"],
                },
            )),
            # A lookup, not a search: it retrieves one article you already
            # found. That distinction is what makes it safe for the
            # fact-checker, whose whole job is re-reading cited sources.
            lookup(StructuredTool.from_function(
                coroutine=fetch_article,
                name="fetch_article",
                description=(
                    "Retrieve the full text of a specific article you found via "
                    "search_news. Do this before citing an article as evidence — "
                    "headlines routinely overstate scope."
                ),
                args_schema={
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
            )),
        ]
        return tools

    return factory
