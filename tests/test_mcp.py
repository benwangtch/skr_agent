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

from deep_research_agent.capabilities import is_read_only
from deep_research_agent.config.mcp import MCP
from deep_research_agent.mcp import load_mcp_tools, make_mcp_toolset, mcp_toolset_from_config
from deep_research_agent.protocol import AgentRequest, Principal
from deep_research_agent.runtime import ToolContext

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


class TestCapabilityDeclarations:
    """Which MCP tools a subagent may be handed. Undeclared means unsafe."""

    def test_nothing_declared_by_default(self):
        assert MCP().capability_of("wiki", "search_wiki_pages") is None

    def test_a_bare_tool_name_matches_any_server(self):
        config = MCP(capabilities={"search_wiki_pages": "search"})
        assert config.capability_of("wiki", "search_wiki_pages") == "search"
        assert config.capability_of("other", "search_wiki_pages") == "search"

    def test_a_qualified_name_beats_a_bare_one(self):
        """Two servers exporting the same tool name is the reason the
        qualified form exists; if it lost, declaring one would declare both."""
        config = MCP(
            capabilities={"search": "mutating", "wiki/search": "search"}
        )
        assert config.capability_of("wiki", "search") == "search"
        assert config.capability_of("tickets", "search") == "mutating"

    def test_an_undeclared_tool_stays_undeclared(self):
        config = MCP(capabilities={"search_wiki_pages": "search"})
        assert config.capability_of("wiki", "write_wiki_page") is None

    def test_a_misspelt_level_is_rejected_rather_than_ignored(self):
        """Silently ignoring it would fail closed, which looks exactly like
        forgetting the entry -- a tool missing from a subagent with no reason
        given anywhere."""
        with pytest.raises(ValueError, match="read-only|unknown capability"):
            MCP(capabilities={"search_wiki_pages": "read-only"})

    def test_the_error_names_the_levels_that_do_work(self):
        with pytest.raises(ValueError, match="lookup"):
            MCP(capabilities={"x": "readonly"})


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
            # Stripped: StructuredTool.from_function trims trailing whitespace
            # off a multi-line docstring. That is not the model seeing a
            # different tool, which is what this test is about.
            assert wrapped[name].description.strip() == tool.description.strip()
            assert wrapped[name].args == tool.args

    async def test_two_calls_in_a_row_both_work(self):
        """Tools reconnect per invocation; nothing holds a session open, so a
        long-lived server does not go stale between calls."""
        toolset = await mcp_toolset_from_config(stdio_config())
        tool = {t.name: t for t in toolset(context())}["get_supplier_risk_score"]
        assert "acme" in str(await tool.ainvoke({"supplier_id": "acme"}))
        assert "nordwind" in str(await tool.ainvoke({"supplier_id": "nordwind"}))

    async def test_an_undeclared_mcp_tool_reaches_the_agent_but_no_subagent(self):
        """The default. Nothing in the MCP protocol says whether a tool
        mutates, so an undeclared one is treated as if it does, and the
        investigator is read-only by construction."""
        from deep_research_agent import build_mesh

        root = Path(__file__).resolve().parent.parent
        toolset = await mcp_toolset_from_config(stdio_config())
        mesh = build_mesh(
            fixtures=root / "fixtures", project_root=root, extra_toolsets=[toolset]
        )
        tools, subagents = mesh.agent.build_tools(context())

        assert "get_supplier_risk_score" in {t.name for t in tools}
        assert "get_supplier_risk_score" not in {t.name for t in subagents[0]["tools"]}

    async def test_a_tool_declared_search_reaches_researching_subagents(self):
        """The point of the declaration. A read-only MCP source is useless to
        this agent if only the lead can call it -- the specialists are where
        the research actually happens."""
        from deep_research_agent import build_mesh
        from deep_research_agent.capabilities import is_read_only

        root = Path(__file__).resolve().parent.parent
        config = stdio_config(capabilities={"get_supplier_risk_score": "search"})
        toolset = await mcp_toolset_from_config(config)
        mesh = build_mesh(
            fixtures=root / "fixtures", project_root=root, extra_toolsets=[toolset]
        )
        tools, subagents = mesh.agent.build_tools(context())
        by_name = {t.name: t for t in tools}

        assert is_read_only(by_name["get_supplier_risk_score"])
        researcher = next(s for s in subagents if s["name"] == "general-purpose")
        assert "get_supplier_risk_score" in {t.name for t in researcher["tools"]}

    async def test_search_is_still_withheld_from_the_reference_checker(self):
        """A checker that can search stops checking and starts researching --
        it "confirms" a claim from a source the report never cited. Declaring a
        tool `search` must not buy a way around that."""
        from deep_research_agent import build_mesh

        root = Path(__file__).resolve().parent.parent
        config = stdio_config(capabilities={"get_supplier_risk_score": "search"})
        toolset = await mcp_toolset_from_config(config)
        mesh = build_mesh(
            fixtures=root / "fixtures", project_root=root, extra_toolsets=[toolset]
        )
        _, subagents = mesh.agent.build_tools(context())
        checker = next(s for s in subagents if s["name"] == "reference-checker")
        assert "get_supplier_risk_score" not in {t.name for t in checker["tools"]}

    async def test_a_tool_declared_lookup_reaches_the_reference_checker(self):
        """`lookup` is the narrowest level and the only one the checker gets:
        fetching a named thing cannot turn into going and finding new material."""
        from deep_research_agent import build_mesh

        root = Path(__file__).resolve().parent.parent
        config = stdio_config(capabilities={"get_supplier_risk_score": "lookup"})
        toolset = await mcp_toolset_from_config(config)
        mesh = build_mesh(
            fixtures=root / "fixtures", project_root=root, extra_toolsets=[toolset]
        )
        _, subagents = mesh.agent.build_tools(context())
        checker = next(s for s in subagents if s["name"] == "reference-checker")
        assert "get_supplier_risk_score" in {t.name for t in checker["tools"]}

    async def test_declaring_a_tool_mutating_keeps_it_off_every_subagent(self):
        """Saying it out loud must land in the same place as saying nothing."""
        from deep_research_agent import build_mesh

        root = Path(__file__).resolve().parent.parent
        config = stdio_config(capabilities={"get_supplier_risk_score": "mutating"})
        toolset = await mcp_toolset_from_config(config)
        mesh = build_mesh(
            fixtures=root / "fixtures", project_root=root, extra_toolsets=[toolset]
        )
        tools, subagents = mesh.agent.build_tools(context())
        assert "get_supplier_risk_score" in {t.name for t in tools}
        for subagent in subagents:
            assert "get_supplier_risk_score" not in {t.name for t in subagent["tools"]}

    async def test_declaring_one_tool_does_not_declare_its_neighbour(self):
        """Per tool, not per server. A server that exports one safe tool and
        one dangerous one is the normal case."""
        from deep_research_agent.capabilities import is_read_only

        config = stdio_config(capabilities={"get_supplier_risk_score": "search"})
        toolset = await mcp_toolset_from_config(config)
        by_name = {t.name: t for t in toolset(context())}
        assert is_read_only(by_name["get_supplier_risk_score"])
        assert not is_read_only(by_name["list_open_audits"])

    async def test_the_provenance_metadata_survives_the_declaration(self):
        """Both sets of metadata keys live on one dict; an earlier version of
        this wrote capability over the top of `mcp_server` and the trace lost
        which server a call went to."""
        config = stdio_config(capabilities={"get_supplier_risk_score": "search"})
        toolset = await mcp_toolset_from_config(config)
        tool = {t.name: t for t in toolset(context())}["get_supplier_risk_score"]
        assert tool.metadata["mcp_server"] == "supplier-risk"
        assert tool.metadata["tool_source"] == "mcp"
        assert tool.metadata["mutates"] is False

    async def test_a_declared_tool_still_records_a_citation(self):
        """Declaring capability must not cost provenance."""
        ctx = context()
        config = stdio_config(capabilities={"get_supplier_risk_score": "search"})
        toolset = await mcp_toolset_from_config(config)
        tool = {t.name: t for t in toolset(ctx)}["get_supplier_risk_score"]
        await tool.ainvoke({"supplier_id": "acme-semi"})
        assert ctx.citations[0].ref == "mcp://supplier-risk/get_supplier_risk_score"

    async def test_a_wiki_search_result_enters_the_corpus(self):
        """Without this the agent can read a page through MCP, cite it
        correctly, and still have the check report the citation as matching
        nothing -- because the reference formatter has no title to use."""
        ctx = context()
        config = stdio_config(
            capabilities={"search_wiki_pages": "search"},
            wiki_page_tools=["search_wiki_pages"],
        )
        toolset = await mcp_toolset_from_config(config)
        tool = {t.name: t for t in toolset(ctx)}["search_wiki_pages"]

        await tool.ainvoke({"query": "acme"})

        assert {d.ref for d in ctx.documents} == {
            "supply/acme-semiconductor",
            "supply/nordwind-logistics",
        }

    async def test_the_recorded_page_can_be_cited(self):
        """End to end: what MCP returned goes through the same formatter the
        wiki tools feed, and comes out as the house citation format."""
        from deep_research_agent.domains.supply_chain.references import format_reference
        from deep_research_agent.wiki.routes import page_url

        ctx = context()
        config = stdio_config(wiki_page_tools=["search_wiki_pages"])
        toolset = await mcp_toolset_from_config(config)
        await {t.name: t for t in toolset(ctx)}["search_wiki_pages"].ainvoke({"query": "acme"})

        corpus = {d.ref: {"title": d.title, "kind": d.kind} for d in ctx.documents}
        ref = "supply/acme-semiconductor"
        assert format_reference(ref, corpus) == f"[acme-semiconductor]({page_url(ref)})"

    async def test_the_link_text_is_the_name_the_wiki_gave(self):
        """Not the model's paraphrase. Same rule as every other source."""
        ctx = context()
        config = stdio_config(wiki_page_tools=["search_wiki_pages"])
        toolset = await mcp_toolset_from_config(config)
        await {t.name: t for t in toolset(ctx)}["search_wiki_pages"].ainvoke({"query": "acme"})
        assert ctx.document("supply/acme-semiconductor").title == "acme-semiconductor"

    async def test_content_is_kept_not_just_the_reference(self):
        ctx = context()
        config = stdio_config(wiki_page_tools=["search_wiki_pages"])
        toolset = await mcp_toolset_from_config(config)
        await {t.name: t for t in toolset(ctx)}["search_wiki_pages"].ainvoke({"query": "acme"})
        assert "two fabs" in ctx.document("supply/acme-semiconductor").content

    async def test_a_hit_with_no_content_falls_back_to_its_description(self):
        """An empty body would make the page unquotable; the description is
        the most the source offered."""
        ctx = context()
        config = stdio_config(wiki_page_tools=["search_wiki_pages"])
        toolset = await mcp_toolset_from_config(config)
        await {t.name: t for t in toolset(ctx)}["search_wiki_pages"].ainvoke({"query": "acme"})
        assert ctx.document("supply/nordwind-logistics").content == "Freight partner."

    async def test_an_unlisted_tool_records_nothing(self):
        """Opt-in. Reading a result means knowing its shape, so it happens
        only where someone said the shape is known."""
        ctx = context()
        toolset = await mcp_toolset_from_config(stdio_config())
        await {t.name: t for t in toolset(ctx)}["search_wiki_pages"].ainvoke({"query": "acme"})
        assert ctx.documents == []

    async def test_an_unlisted_tool_still_records_its_citation(self):
        """Opting out of the corpus must not cost provenance."""
        ctx = context()
        toolset = await mcp_toolset_from_config(stdio_config())
        await {t.name: t for t in toolset(ctx)}["search_wiki_pages"].ainvoke({"query": "acme"})
        assert ctx.citations[0].ref == "mcp://supplier-risk/search_wiki_pages"

    async def test_a_listed_tool_whose_shape_does_not_match_warns(self, caplog):
        """The failure this guards: the service renames `hits`, nothing enters
        the corpus, no error is raised, and the only symptom is links quietly
        going missing from reports."""
        import logging

        ctx = context()
        config = stdio_config(wiki_page_tools=["search_nothing_useful"])
        toolset = await mcp_toolset_from_config(config)
        with caplog.at_level(logging.WARNING):
            await {t.name: t for t in toolset(ctx)}["search_nothing_useful"].ainvoke(
                {"query": "x"}
            )
        assert ctx.documents == []
        assert "search_nothing_useful" in caplog.text
        assert "hits" in caplog.text

    async def test_a_listed_tool_no_server_exports_is_reported_at_startup(self, caplog):
        """A typo in the config would otherwise be indistinguishable from a
        tool that simply never got called."""
        import logging

        config = stdio_config(wiki_page_tools=["serch_wiki_pages"])
        with caplog.at_level(logging.WARNING):
            await mcp_toolset_from_config(config)
        assert "serch_wiki_pages" in caplog.text

    async def test_recording_pages_is_independent_of_capability(self):
        """They answer different questions -- 'may a subagent call this' and
        'do I know how to read its output'. Coupling them would mean an
        undeclared tool could not contribute sources."""
        ctx = context()
        config = stdio_config(wiki_page_tools=["search_wiki_pages"])
        toolset = await mcp_toolset_from_config(config)
        tools = {t.name: t for t in toolset(ctx)}
        assert not is_read_only(tools["search_wiki_pages"])
        await tools["search_wiki_pages"].ainvoke({"query": "acme"})
        assert len(ctx.documents) == 2

    async def test_the_agent_still_has_its_own_sources(self):
        """Adding MCP must not displace the built-in data sources."""
        from deep_research_agent import build_mesh

        root = Path(__file__).resolve().parent.parent
        toolset = await mcp_toolset_from_config(stdio_config())
        mesh = build_mesh(
            fixtures=root / "fixtures", project_root=root, extra_toolsets=[toolset]
        )
        names = {t.name for t in mesh.agent.build_tools(context())[0]}
        assert {"list_bom_companies", "search_news", "wiki_search"} <= names
