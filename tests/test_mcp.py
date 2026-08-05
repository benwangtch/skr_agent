"""MCP integration, against a real MCP server subprocess -- no network, no model.

``mcp_fixture_server.py`` is started over stdio for the tests that need a live
server. That is slower than mocking the client, and it is the point: what can
actually break here is the contract with ``langchain-mcp-adapters``, and a mock
of that contract would only restate this codebase's assumptions back to itself.

The config tests need no server at all -- they check that nothing is mounted
unless someone deliberately configured it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from skr_agent.config.mcp import MCP
from skr_agent.mcp import load_mcp_tools, make_mcp_toolset, mcp_toolset_from_config
from skr_agent.protocol import AgentRequest, Principal
from skr_agent.runtime import ToolContext

SERVER = str(Path(__file__).parent / "mcp_fixture_server.py")
ALICE = Principal(subject="alice", division="supply", roles=frozenset({"wiki.reader"}))


def stdio_config(**kwargs) -> MCP:
    return MCP(
        servers={
            "supplier-risk": {
                "transport": "stdio",
                "command": sys.executable,
                "args": [SERVER],
            }
        },
        **kwargs,
    )


def context() -> ToolContext:
    request = AgentRequest(principal=ALICE, task="t")
    return ToolContext(principal=ALICE, request=request)


class TestConfig:
    """Nothing is mounted unless it was asked for."""

    def test_nothing_configured_means_no_connections(self):
        assert MCP(url=None, servers={}).connections() == {}
        assert MCP(url=None, servers={}).configured() is False

    def test_a_url_produces_one_connection(self):
        config = MCP(url="https://mcp.internal.corp/mcp")
        connections = config.connections()
        assert list(connections) == ["mcp"]
        assert connections["mcp"]["url"] == "https://mcp.internal.corp/mcp"
        assert connections["mcp"]["transport"] == "streamable_http"

    def test_a_token_becomes_a_bearer_header(self):
        config = MCP(url="https://mcp.internal.corp/mcp", token="s3cret")
        assert connections_header(config) == "Bearer s3cret"

    def test_no_token_sends_no_auth_header(self):
        config = MCP(url="https://mcp.internal.corp/mcp")
        assert "headers" not in config.connections()["mcp"]

    def test_servers_overrides_url_rather_than_merging(self):
        """A half-merged connection map is worse to debug than an obviously
        ignored variable."""
        config = MCP(
            url="https://ignored.example/mcp",
            servers={"real": {"transport": "sse", "url": "https://real.example/mcp"}},
        )
        assert list(config.connections()) == ["real"]

    def test_the_token_is_not_printed_by_repr(self):
        config = MCP(url="https://x/mcp", token="s3cret")
        assert "s3cret" not in repr(config)


def connections_header(config: MCP) -> str:
    return config.connections()["mcp"]["headers"]["Authorization"]


class TestLoadingWithNothingConfigured:
    async def test_no_config_loads_no_tools(self):
        assert await load_mcp_tools(MCP(url=None, servers={})) == []

    async def test_no_config_yields_no_toolset(self):
        """None, not an empty toolset -- callers distinguish 'not configured'
        from 'configured but empty'."""
        assert await mcp_toolset_from_config(MCP(url=None, servers={})) is None

    async def test_an_unreachable_server_degrades_instead_of_raising(self):
        """An auxiliary data source being down should cost you its tools, not
        stop the agent from starting."""
        config = MCP(
            servers={"broken": {"transport": "stdio", "command": sys.executable,
                                "args": ["-c", "raise SystemExit(1)"]}}
        )
        assert await load_mcp_tools(config) == []


@pytest.mark.mcp_server
class TestAgainstARealServer:
    async def test_tools_are_discovered_with_their_schemas(self):
        loaded = await load_mcp_tools(stdio_config())
        by_name = {tool.name: tool for _, tool in loaded}
        assert {"get_supplier_risk_score", "list_open_audits"} <= set(by_name)
        assert "supplier_id" in by_name["get_supplier_risk_score"].args

    async def test_the_server_name_travels_with_each_tool(self):
        loaded = await load_mcp_tools(stdio_config())
        assert {server for server, _ in loaded} == {"supplier-risk"}

    async def test_a_tool_actually_runs(self):
        toolset = await mcp_toolset_from_config(stdio_config())
        tools = {t.name: t for t in toolset(context())}
        result = await tools["get_supplier_risk_score"].ainvoke({"supplier_id": "acme-semi"})
        assert "acme-semi" in str(result)
        assert "amber" in str(result)

    async def test_calling_a_tool_records_a_citation(self):
        """MCP data has to be attributable like everything else the agent
        reports, or the prompt's 'source every claim' rule has nothing to
        point at."""
        ctx = context()
        toolset = await mcp_toolset_from_config(stdio_config())
        tools = {t.name: t for t in toolset(ctx)}
        assert ctx.citations == []

        await tools["get_supplier_risk_score"].ainvoke({"supplier_id": "acme-semi"})

        assert len(ctx.citations) == 1
        assert ctx.citations[0].ref == "mcp://supplier-risk/get_supplier_risk_score"

    async def test_the_wrapper_keeps_the_advertised_name_and_description(self):
        """The model must see the tool the server advertised, not a wrapper."""
        loaded = await load_mcp_tools(stdio_config())
        original = {t.name: t for _, t in loaded}
        wrapped = {t.name: t for t in make_mcp_toolset(loaded)(context())}
        assert set(original) == set(wrapped)
        for name, tool in original.items():
            assert wrapped[name].description == tool.description
            assert wrapped[name].args == tool.args

    async def test_two_calls_in_a_row_both_work(self):
        """Tools reconnect per invocation; nothing holds a session open, so a
        long-lived server does not go stale between calls."""
        toolset = await mcp_toolset_from_config(stdio_config())
        tool = {t.name: t for t in toolset(context())}["get_supplier_risk_score"]
        assert "acme" in str(await tool.ainvoke({"supplier_id": "acme"}))
        assert "nordwind" in str(await tool.ainvoke({"supplier_id": "nordwind"}))

    async def test_mcp_tools_reach_the_agent_surface_but_not_the_subagent(self):
        """MCP tools mount on the main agent only: nothing tells us which of
        them mutate state, and the investigator is read-only by construction."""
        from skr_agent import build_mesh

        root = Path(__file__).resolve().parent.parent
        toolset = await mcp_toolset_from_config(stdio_config())
        mesh = build_mesh(
            fixtures=root / "fixtures", project_root=root, extra_toolsets=[toolset]
        )
        tools, subagents = mesh.report_agent.build_tools(context())

        assert "get_supplier_risk_score" in {t.name for t in tools}
        assert "get_supplier_risk_score" not in {t.name for t in subagents[0]["tools"]}

    async def test_the_agent_still_has_its_own_sources(self):
        """Adding MCP must not displace the built-in data sources."""
        from skr_agent import build_mesh

        root = Path(__file__).resolve().parent.parent
        toolset = await mcp_toolset_from_config(stdio_config())
        mesh = build_mesh(
            fixtures=root / "fixtures", project_root=root, extra_toolsets=[toolset]
        )
        names = {t.name for t in mesh.report_agent.build_tools(context())[0]}
        assert {"list_bom_companies", "search_news", "wiki_search"} <= names
