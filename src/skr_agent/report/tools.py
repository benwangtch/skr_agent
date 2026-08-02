"""Toolset factories for the report agent.

A factory takes a ``ToolContext`` (principal + citation sink) and returns tools
bound to it. This is why they are functions rather than module-level servers:
the principal has to be baked into the closure so the model cannot supply it.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from claude_agent_sdk import ToolAnnotations, create_sdk_mcp_server, tool

from ..protocol import Citation
from ..runtime import ToolBundle, ToolContext
from .sources import BomSource, NewsFeed

__all__ = ["make_bom_toolset", "make_news_toolset"]


def make_bom_toolset(bom: BomSource):
    """Read-only access to the bill of materials."""

    def factory(ctx: ToolContext) -> ToolBundle:
        @tool(
            "list_bom_companies",
            "List companies in the bill of materials. Call this first when asked to "
            "scan or sweep the BOM, so you know the full set before deciding where to "
            "dig. Filter by tier to prioritise critical suppliers.",
            {
                "type": "object",
                "properties": {
                    "tier": {
                        "type": "string",
                        "description": "Optional tier filter, e.g. 'critical'.",
                    }
                },
            },
            annotations=ToolAnnotations(readOnlyHint=True),
        )
        async def list_companies(args: dict[str, Any]) -> dict[str, Any]:
            companies = bom.list_companies(args.get("tier"))
            lines = [
                f"- {c.company_id} | {c.name} | tier={c.tier} | "
                f"components={', '.join(c.components) or 'n/a'}"
                for c in companies
            ]
            body = "\n".join(lines) or "(BOM is empty)"
            return {"content": [{"type": "text", "text": f"{len(companies)} companies:\n{body}"}]}

        @tool(
            "get_bom_company",
            "Look up one company's BOM entry: legal name, aliases, tier, and which "
            "components we source from them. Use the aliases when searching news — "
            "incidents are often reported under a parent or brand name.",
            {"company_id": str},
            annotations=ToolAnnotations(readOnlyHint=True),
        )
        async def get_company(args: dict[str, Any]) -> dict[str, Any]:
            company = bom.get_company(args["company_id"])
            if company is None:
                return {
                    "content": [
                        {"type": "text", "text": f"No BOM entry for {args['company_id']!r}."}
                    ],
                    "is_error": True,
                }
            ctx.cite(
                Citation(
                    kind="internal_record",
                    ref=f"bom/{company.company_id}",
                    title=f"BOM entry: {company.name}",
                )
            )
            return {"content": [{"type": "text", "text": str(asdict(company))}]}

        server = create_sdk_mcp_server(name="bom", version="1.0.0", tools=[list_companies, get_company])
        return server, ["mcp__bom__list_bom_companies", "mcp__bom__get_bom_company"]

    return factory


def make_news_toolset(feed: NewsFeed):
    """External incident search. Swap the feed for real search in production."""

    def factory(ctx: ToolContext) -> ToolBundle:
        @tool(
            "search_news",
            "Search external news for incidents involving a company — outages, "
            "recalls, breaches, insolvency, sanctions, natural disasters affecting "
            "sites. Search each company under its name and its aliases; a single "
            "query per company is usually not enough.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": ["query"],
            },
            annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
        )
        async def search_news(args: dict[str, Any]) -> dict[str, Any]:
            articles = feed.search(args["query"], limit=int(args.get("limit", 5)))
            if not articles:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"No articles matched {args['query']!r}. Record this as "
                                "'no external signal found', not as 'no incident occurred'."
                            ),
                        }
                    ]
                }
            lines = [
                f"- {a.published} | {a.source} | {a.title}\n  {a.url}\n  {a.summary}"
                for a in articles
            ]
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        @tool(
            "fetch_article",
            "Retrieve the full text of a specific article you found via search_news. "
            "Do this before citing an article as evidence — headlines routinely "
            "overstate scope.",
            {"url": str},
            annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
        )
        async def fetch_article(args: dict[str, Any]) -> dict[str, Any]:
            article = feed.fetch(args["url"])
            if article is None:
                return {
                    "content": [{"type": "text", "text": f"Could not fetch {args['url']!r}."}],
                    "is_error": True,
                }
            ctx.cite(
                Citation(
                    kind="external_url",
                    ref=article.url,
                    title=article.title,
                    snippet=article.summary[:200],
                )
            )
            text = (
                f"# {article.title}\n{article.source} — {article.published}\n\n"
                f"{article.summary}\n\nCompanies mentioned: {', '.join(article.companies)}"
            )
            return {"content": [{"type": "text", "text": text}]}

        server = create_sdk_mcp_server(name="news", version="1.0.0", tools=[search_news, fetch_article])
        return server, ["mcp__news__search_news", "mcp__news__fetch_article"]

    return factory
