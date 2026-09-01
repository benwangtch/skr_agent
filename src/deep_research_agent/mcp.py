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

import logging
from typing import Any, Sequence

from langchain_core.tools import BaseTool, StructuredTool

from deep_research_agent.capabilities import lookup, mutating, search
from deep_research_agent.config import get_mcp
from deep_research_agent.config.mcp import MCP
from deep_research_agent.protocol import Citation
from deep_research_agent.runtime import ToolBundle, ToolContext, ToolsetFactory

log = logging.getLogger(__name__)

__all__ = ["load_mcp_tools", "make_mcp_toolset", "mcp_toolset_from_config"]


_DECLARE = {"lookup": lookup, "search": search, "mutating": mutating}


def _wrap_with_citation(
    tool: BaseTool, server: str, ctx: ToolContext, capability: str | None = None
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

    def factory(ctx: ToolContext) -> ToolBundle:
        return [
            _wrap_with_citation(tool, server, ctx, declared[(server, tool.name)])
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
