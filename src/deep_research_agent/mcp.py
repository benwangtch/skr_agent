"""MCP servers as another data source for the research agent.

An MCP server is a peer of the BOM, the news feed, and the wiki: a place the
agent can go to find things out. Its tools arrive already shaped as LangChain
tools, so the only work here is fitting them to two of this codebase's
conventions.

**Loading is async, toolset factories are not.** ``ToolsetFactory`` is called
per request and returns tools synchronously, but discovering an MCP server's
tools means a network round trip. So the tools are fetched **once**, when the
process starts, and the factory just hands out the already-loaded objects.
Each *invocation* still opens its own session, so a long-lived process does
not hold a connection open between calls. The consequence worth knowing: a
server that gains a tool after startup will not be noticed until a restart.

**Identity does not propagate.** Every other tool in this codebase closes over
the caller's ``Principal`` and authorizes against it. MCP tools cannot: the
credential is configured per connection (``MCP_TOKEN``), so from the server's
side every call looks like the same service account no matter who triggered
the run. That is a real gap, not an oversight — see ``docs/design/DESIGN.md`` §5.5. If
the MCP service enforces per-user rules, it needs the end user's token, and
this module needs to grow a per-request client to carry it. Until then, only
connect servers whose whole contents the *least* privileged caller of this
agent is allowed to see.

**Capability is declared, never inferred.** The MCP protocol says nothing
about whether a tool writes, so an arriving tool is undeclared, and
``capabilities.py`` reads undeclared as mutating. The practical effect is that
an MCP tool is offered to the top-level agent and to nobody else — not the
researcher subagents, not the reference checker. When a tool really is
read-only, the operator says so in ``MCP_CAPABILITIES`` and it starts reaching
subagents. It is configuration rather than a rule in code because it is a
claim about someone else's service: ``search_*`` is a naming convention, and
guessing from it would put an undeclared mutation behind the fact-checker.

What this module does add is provenance: each MCP tool is wrapped so that
calling it records a ``Citation``, the same way reading a wiki page does. An
agent told to source every claim needs something to point at, and
``mcp://<server>/<tool>`` is the honest answer for where the fact came from.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Sequence

from langchain_core.tools import BaseTool, StructuredTool

from deep_research_agent.capabilities import lookup, mutating, search
from deep_research_agent.config import get_mcp
from deep_research_agent.config.mcp import MCP
from deep_research_agent.protocol import Citation, RetrievedDocument
from deep_research_agent.runtime import ToolBundle, ToolContext, ToolsetFactory
from deep_research_agent.wiki.routes import page_ref

log = logging.getLogger(__name__)

__all__ = ["load_mcp_tools", "make_mcp_toolset", "mcp_toolset_from_config"]


_DECLARE = {"lookup": lookup, "search": search, "mutating": mutating}


def _as_json(text: str) -> dict | None:
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _as_payload(result: Any) -> dict | None:
    """The tool's dict, whichever way the adapter handed it over.

    Three shapes are in play and which one you get depends on the versions of
    ``langchain-mcp-adapters``, ``mcp`` and the server: an already-decoded
    dict, a JSON string, or the raw MCP content blocks —
    ``[{"type": "text", "text": "<json>"}]``, which is what this repo's own
    fixture server produces today. Handling all three is not defensiveness:
    the first version of this handled only the first two, and the symptom was
    a search that recorded nothing at all, with no error anywhere.
    """
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        return _as_json(result)
    if isinstance(result, (list, tuple)):
        for block in result:
            text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
            if isinstance(text, str):
                payload = _as_json(text)
                if payload is not None:
                    return payload
    return None


def _record_wiki_pages(result: Any, tool_name: str, ctx: ToolContext) -> None:
    """Record a wiki-page search result in the run's corpus.

    Only called for tools named in ``MCP_WIKI_PAGE_TOOLS``. A hit becomes a
    ``RetrievedDocument`` under ``page_ref(namespace, page_name)`` so that the
    reference formatter can build a wiki link for it — without this the agent
    can read a page through MCP, cite it correctly, and still have the check
    report the citation as matching nothing.

    Nothing here raises. A tool that fails to parse has already returned its
    result to the model, and taking the call down afterwards would lose that
    for a bookkeeping problem. It does warn, though, and names the tool: a
    listed tool contributing no documents is a broken assumption about
    someone else's JSON, and the visible symptom otherwise is links silently
    missing from a report.
    """
    payload = _as_payload(result)
    hits = payload.get("hits") if isinstance(payload, dict) else None
    if not isinstance(hits, list):
        log.warning(
            "mcp.wiki_pages_unparsed tool=%s -- it is named in "
            "MCP_WIKI_PAGE_TOOLS but its result has no 'hits' list, so no "
            "pages entered the corpus and citations to them will not resolve. "
            "Check the tool's response shape.",
            tool_name,
        )
        return

    recorded = 0
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        namespace, name = hit.get("namespace"), hit.get("page_name")
        if not namespace or not name:
            continue
        ctx.record(
            RetrievedDocument(
                ref=page_ref(str(namespace), str(name)),
                kind="wiki_page",
                # The name as the source gave it. The reference formatter uses
                # this verbatim as link text, which is the whole reason the
                # document is stored rather than just cited.
                title=str(name),
                content=str(hit.get("content") or hit.get("description") or ""),
                metadata={
                    k: hit[k]
                    for k in ("page_id", "namespace", "score", "update_datetime")
                    if k in hit
                },
            )
        )
        recorded += 1

    log.info("mcp.wiki_pages_recorded tool=%s pages=%d", tool_name, recorded)


def _wrap_with_citation(
    tool: BaseTool,
    server: str,
    ctx: ToolContext,
    capability: str | None = None,
    records_wiki_pages: bool = False,
) -> BaseTool:
    """Return a tool that behaves like ``tool`` but records where data came from.

    The wrapper keeps the original name, description and argument schema, so
    the model sees exactly the tool the MCP server advertised — only the
    provenance bookkeeping is added.

    ``capability`` is the operator's declaration from ``MCP_CAPABILITIES``.
    ``None`` leaves the tool undeclared, which ``capabilities.py`` reads as
    mutating — so the tool stays on the top-level agent and out of every
    subagent. That is the safe default, not a bug to work around here.
    """

    async def _call(**kwargs: Any) -> Any:
        result = await tool.ainvoke(kwargs)
        ctx.cite(
            Citation(
                kind="internal_record",
                ref=f"mcp://{server}/{tool.name}",
                title=f"{server}: {tool.name}",
                snippet=str(kwargs)[:200],
            )
        )
        if records_wiki_pages:
            _record_wiki_pages(result, tool.name, ctx)
        return result

    wrapped = StructuredTool.from_function(
        coroutine=_call,
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
        # Rides along to the trace span. A tool span alone cannot tell you
        # whether this came from an MCP server or from this codebase, and
        # "which MCP server did this run actually touch" is exactly the
        # question a trace should answer.
        metadata={"tool_source": "mcp", "mcp_server": server},
    )
    # Declared after construction so the capability keys are merged into the
    # provenance metadata rather than replacing it -- both end up on the same
    # dict, and losing either one is a silent failure.
    declare = _DECLARE.get(capability or "")
    return declare(wrapped) if declare else wrapped


async def load_mcp_tools(config: MCP | None = None) -> list[tuple[str, BaseTool]]:
    """Connect to every configured MCP server and fetch its tools.

    Returns ``(server_name, tool)`` pairs so provenance can name the server a
    tool came from. Returns ``[]`` when nothing is configured, which is the
    normal case and not an error.

    A server that fails to respond is logged and skipped rather than taking
    the process down with it: an unreachable auxiliary data source should
    degrade the agent, not prevent it from starting. Check the log if a tool
    you expected is missing from the agent's surface.
    """
    config = config or get_mcp()
    connections = config.connections()
    if not connections:
        return []

    from langchain_mcp_adapters.client import MultiServerMCPClient

    loaded: list[tuple[str, BaseTool]] = []
    for name, connection in connections.items():
        try:
            client = MultiServerMCPClient({name: connection})
            tools = await client.get_tools()
        except Exception:
            log.exception("mcp.load_failed server=%s -- skipping it", name)
            continue
        log.info("mcp.loaded server=%s tools=%s", name, [t.name for t in tools])
        loaded.extend((name, tool) for tool in tools)

    return loaded


def make_mcp_toolset(
    tools: Sequence[tuple[str, BaseTool]], config: MCP | None = None
) -> ToolsetFactory:
    """Wrap already-loaded MCP tools as a ``ToolsetFactory``.

    Takes what ``load_mcp_tools`` returns. Split from loading so the network
    round trip happens once at startup while the factory stays synchronous and
    per-request, which is what ``DeepAgent`` expects.

    Capability declarations are resolved here, once, rather than inside the
    per-request factory: they come from configuration that cannot change
    between requests, and resolving them once gives somewhere to log the
    outcome without repeating it on every call.
    """
    config = config or get_mcp()
    declared = {
        (server, tool.name): config.capability_of(server, tool.name)
        for server, tool in tools
    }

    undeclared = sorted(f"{s}/{n}" for (s, n), level in declared.items() if level is None)
    if undeclared:
        log.info(
            "mcp.undeclared tools=%s -- treated as mutating, so they are "
            "available to the top-level agent only. Declare read-only ones in "
            "MCP_CAPABILITIES to let subagents use them.",
            undeclared,
        )
    for (server, name), level in sorted(declared.items()):
        if level is not None:
            log.info("mcp.capability server=%s tool=%s level=%s", server, name, level)

    wiki_page_tools = set(config.wiki_page_tools)
    missing = wiki_page_tools - {tool.name for _, tool in tools}
    if missing:
        log.warning(
            "mcp.wiki_page_tools_missing tools=%s -- named in "
            "MCP_WIKI_PAGE_TOOLS but no configured server exports them, so "
            "nothing will enter the corpus under those names.",
            sorted(missing),
        )

    def factory(ctx: ToolContext) -> ToolBundle:
        return [
            _wrap_with_citation(
                tool,
                server,
                ctx,
                declared[(server, tool.name)],
                records_wiki_pages=tool.name in wiki_page_tools,
            )
            for server, tool in tools
        ]

    return factory


async def mcp_toolset_from_config(config: MCP | None = None) -> ToolsetFactory | None:
    """Load and wrap in one step. ``None`` when no MCP server is configured.

    ``None`` rather than an empty toolset so a caller can tell "not configured"
    from "configured but the server exposed nothing", which are different
    problems.
    """
    config = config or get_mcp()
    tools = await load_mcp_tools(config)
    if not tools:
        return None
    return make_mcp_toolset(tools, config)
