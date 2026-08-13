"""MCP servers as another data source for skr agent.

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

What this module does add is provenance: each MCP tool is wrapped so that
calling it records a ``Citation``, the same way reading a wiki page does. An
agent told to source every claim needs something to point at, and
``mcp://<server>/<tool>`` is the honest answer for where the fact came from.
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from langchain_core.tools import BaseTool, StructuredTool

from skr_agent.config import get_mcp
from skr_agent.config.mcp import MCP
from skr_agent.protocol import Citation
from skr_agent.runtime import ToolBundle, ToolContext, ToolsetFactory

log = logging.getLogger(__name__)

__all__ = ["load_mcp_tools", "make_mcp_toolset", "mcp_toolset_from_config"]


def _wrap_with_citation(tool: BaseTool, server: str, ctx: ToolContext) -> BaseTool:
    """Return a tool that behaves like ``tool`` but records where data came from.

    The wrapper keeps the original name, description and argument schema, so
    the model sees exactly the tool the MCP server advertised — only the
    provenance bookkeeping is added.
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

    return StructuredTool.from_function(
        coroutine=_call,
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
    )


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


def make_mcp_toolset(tools: Sequence[tuple[str, BaseTool]]) -> ToolsetFactory:
    """Wrap already-loaded MCP tools as a ``ToolsetFactory``.

    Takes what ``load_mcp_tools`` returns. Split from loading so the network
    round trip happens once at startup while the factory stays synchronous and
    per-request, which is what ``DeepAgent`` expects.
    """

    def factory(ctx: ToolContext) -> ToolBundle:
        return [_wrap_with_citation(tool, server, ctx) for server, tool in tools]

    return factory


async def mcp_toolset_from_config(config: MCP | None = None) -> ToolsetFactory | None:
    """Load and wrap in one step. ``None`` when no MCP server is configured.

    ``None`` rather than an empty toolset so a caller can tell "not configured"
    from "configured but the server exposed nothing", which are different
    problems.
    """
    tools = await load_mcp_tools(config)
    if not tools:
        return None
    return make_mcp_toolset(tools)
