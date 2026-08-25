"""Langfuse tracing: off by default, never fatal, and carrying what we need.

No network. The tests that need tracing "on" point it at a dead host, because
the property worth proving is that an unreachable Langfuse degrades to no
tracing rather than failing the run.
"""

from __future__ import annotations

import pytest

from deep_research_agent.config import get_langfuse, reset_settings_cache
from deep_research_agent.config.langfuse import Langfuse
from deep_research_agent.observability import langfuse_handler, run_config, trace_metadata
from deep_research_agent.principals import service_principal, user_principal
from deep_research_agent.protocol import AgentRequest, Budget


@pytest.fixture
def clean_config():
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def configured(monkeypatch, clean_config):
    """Tracing on, pointed somewhere that does not answer."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://127.0.0.1:9/nowhere")
    reset_settings_cache()


def request_for(principal, **kwargs):
    return AgentRequest(principal=principal, task="t", **kwargs)


class TestConfig:
    def test_off_when_nothing_is_set(self, clean_config):
        assert Langfuse(secret_key="", public_key="").configured() is False

    def test_the_internal_host_is_the_default(self, clean_config):
        assert get_langfuse().base_url == "http://langfuse-ai4bi.cpoap-dev.dev.tsmc.com"

    def test_both_keys_turn_it_on(self):
        assert Langfuse(secret_key="sk-lf-x", public_key="pk-lf-x").configured() is True

    def test_one_key_alone_is_reported_not_half_enabled(self):
        """A single key is a misconfiguration -- someone meant to turn this on."""
        only_secret = Langfuse(secret_key="sk-lf-x", public_key="")
        assert only_secret.configured() is False
        assert "LANGFUSE_PUBLIC_KEY" in only_secret.problems()[0]

        only_public = Langfuse(secret_key="", public_key="pk-lf-x")
        assert only_public.configured() is False
        assert "LANGFUSE_SECRET_KEY" in only_public.problems()[0]

    def test_a_correct_setup_reports_no_problems(self):
        assert Langfuse(secret_key="sk-lf-x", public_key="pk-lf-x").problems() == []

    def test_the_secret_key_is_not_printed_by_repr(self):
        assert "sk-lf-hunter2" not in repr(Langfuse(secret_key="sk-lf-hunter2"))


class TestHandler:
    def test_no_handler_when_unconfigured(self, clean_config):
        assert langfuse_handler() is None

    def test_a_handler_appears_once_configured(self, configured):
        assert langfuse_handler() is not None

    def test_an_unreachable_host_still_yields_a_handler_or_none_never_raises(self, configured):
        """The host in `configured` does not answer. Either outcome is fine;
        raising is not -- tracing must not be able to fail a run."""
        langfuse_handler()  # must not raise


class TestRunConfig:
    def test_unconfigured_passes_through_only_what_the_graph_needs(self, clean_config):
        config = run_config("agent", request_for(user_principal("a", "supply")),
                            recursion_limit=30)
        assert config == {"recursion_limit": 30}
        assert "callbacks" not in config

    def test_configured_adds_callbacks_and_metadata(self, configured):
        config = run_config("agent", request_for(user_principal("a", "supply")),
                            recursion_limit=30)
        assert config["recursion_limit"] == 30
        assert len(config["callbacks"]) == 1
        assert config["metadata"]["langfuse_trace_name"] == "agent"


class TestTraceMetadata:
    """What makes a trace findable in Langfuse afterwards."""

    def test_the_principal_becomes_the_langfuse_user(self):
        md = trace_metadata("agent", request_for(user_principal("bob@supply", "supply")))
        assert md["langfuse_user_id"] == "bob@supply"

    def test_the_trace_id_is_the_session_so_a_run_groups_together(self):
        """A2A threads its task_id through as trace_id, so an A2A task and the
        run it triggered land in one session."""
        request = request_for(user_principal("a", "supply"))
        md = trace_metadata("agent", request)
        assert md["langfuse_session_id"] == request.trace_id
        assert md["trace_id"] == request.trace_id

    def test_scheduled_and_user_runs_are_distinguishable_by_tag(self):
        """The same question under two principals is the same shape with
        different scope -- the tags are what let you tell them apart."""
        scheduled = trace_metadata("agent", request_for(service_principal()))
        triggered = trace_metadata("agent", request_for(user_principal("bob", "supply")))
        assert "actor:service" in scheduled["langfuse_tags"]
        assert "actor:user" in triggered["langfuse_tags"]
        assert "division:exec" in scheduled["langfuse_tags"]
        assert "division:supply" in triggered["langfuse_tags"]

    def test_roles_are_recorded(self):
        """Which roles a run carried is the difference between two otherwise
        identical traces returning different content."""
        md = trace_metadata("agent", request_for(service_principal()))
        assert "wiki.reader.all" in md["principal_roles"]

    def test_budget_and_inputs_are_recorded(self):
        request = request_for(
            user_principal("a", "supply"), inputs={"tier": "critical"},
            budget=Budget(max_turns=80),
        )
        md = trace_metadata("agent", request)
        assert md["budget_max_turns"] == 80
        assert md["inputs"] == {"tier": "critical"}

    def test_delegation_records_the_parent(self):
        request = AgentRequest(
            principal=user_principal("a", "supply"), task="t", parent_agent="caller"
        )
        assert trace_metadata("agent", request)["parent_agent"] == "caller"


class TestMcpToolsAreIdentifiableInATrace:
    """A tool span alone cannot say whether it came from MCP -- the wrapper
    stamps it so 'which MCP server did this run touch' is answerable."""

    @pytest.mark.mcp_server
    async def test_mcp_tools_carry_their_source_and_server(self):
        import sys
        from pathlib import Path

        from deep_research_agent.config.mcp import MCP
        from deep_research_agent.mcp import mcp_toolset_from_config
        from deep_research_agent.protocol import Principal
        from deep_research_agent.runtime import ToolContext

        server = str(Path(__file__).parent / "mcp_fixture_server.py")
        config = MCP(servers={"supplier-risk": {
            "transport": "stdio", "command": sys.executable, "args": [server]}})
        principal = Principal(subject="a", division="supply", roles=frozenset())
        ctx = ToolContext(principal=principal,
                          request=AgentRequest(principal=principal, task="t"))

        for tool in (await mcp_toolset_from_config(config))(ctx):
            assert tool.metadata["tool_source"] == "mcp"
            assert tool.metadata["mcp_server"] == "supplier-risk"

    def test_built_in_tools_are_not_labelled_as_mcp(self, mesh_tools):
        for tool in mesh_tools:
            assert (tool.metadata or {}).get("tool_source") != "mcp", tool.name


@pytest.fixture
def mesh_tools():
    from pathlib import Path

    from deep_research_agent import build_mesh
    from deep_research_agent.protocol import Principal
    from deep_research_agent.runtime import ToolContext

    root = Path(__file__).resolve().parent.parent
    principal = Principal(subject="a", division="supply", roles=frozenset({"wiki.reader"}))
    mesh = build_mesh(fixtures=root / "fixtures", project_root=root)
    ctx = ToolContext(principal=principal,
                      request=AgentRequest(principal=principal, task="t"))
    return mesh.agent.build_tools(ctx)[0]
