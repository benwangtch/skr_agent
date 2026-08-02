"""Wiring smoke tests: the agents build, and their SDK options are sane.

These stop short of calling the model — they catch the class of mistake that
otherwise only shows up as a runtime error several minutes into a paid run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skr_agent import build_copilot, build_mesh
from skr_agent.principals import service_principal, user_principal
from skr_agent.protocol import AgentRequest, Budget
from skr_agent.runtime import ToolContext, _extract_json
from skr_agent.wiki.authz import EXEC_NAMESPACE

ROOT = Path(__file__).resolve().parent.parent
ALICE = user_principal("alice", "supply", roles={"wiki.reader", "wiki.writer"})


@pytest.fixture
def mesh():
    return build_mesh(fixtures=ROOT / "fixtures", project_root=ROOT)


@pytest.fixture
def mesh_with_wiki_agent():
    return build_mesh(fixtures=ROOT / "fixtures", project_root=ROOT, with_wiki_agent=True)


def options_for(agent, principal=ALICE, budget=None):
    request = AgentRequest(
        principal=principal, task="t", budget=budget or Budget(max_turns=40)
    )
    ctx = ToolContext(principal=principal, request=request)
    return agent._build_options(ctx), ctx


class TestReportAgentWiring:
    def test_registry_exposes_the_report_agent_by_default(self, mesh):
        names = {s.name for s in mesh.registry.list()}
        assert names == {"wiki_report"}
        assert mesh.coordinator is None

    def test_wiki_agent_is_opt_in(self, mesh_with_wiki_agent):
        names = {s.name for s in mesh_with_wiki_agent.registry.list()}
        assert names == {"wiki_report", "wiki_ask"}
        assert mesh_with_wiki_agent.coordinator is not None

    def test_report_agent_reaches_the_wiki_only_through_its_own_tools(self, mesh):
        options, _ = options_for(mesh.report_agent)
        wiki_tools = sorted(t for t in options.allowed_tools if "wiki" in t)
        assert wiki_tools == [
            "mcp__wiki__wiki_read_page",
            "mcp__wiki__wiki_search",
            "mcp__wiki__wiki_write_page",
        ]

    def test_report_agent_has_bom_and_news_tools(self, mesh):
        options, _ = options_for(mesh.report_agent)
        assert "mcp__bom__list_bom_companies" in options.allowed_tools
        assert "mcp__news__search_news" in options.allowed_tools

    def test_skill_tool_is_enabled_and_project_settings_load(self, mesh):
        options, _ = options_for(mesh.report_agent)
        assert "Skill" in options.allowed_tools
        # Skills live in .claude/skills; the SDK only finds them via project settings.
        assert options.setting_sources == ["project"]

    def test_skill_file_exists_where_the_sdk_will_look(self, mesh):
        assert (ROOT / ".claude" / "skills" / "wiki-report" / "SKILL.md").is_file()

    def test_subagent_tools_are_a_subset_of_the_parent_surface(self, mesh):
        options, _ = options_for(mesh.report_agent)
        investigator = options.agents["company-investigator"]
        assert set(investigator.tools) <= set(options.allowed_tools)

    def test_subagent_cannot_write(self, mesh):
        """Only the top-level agent writes; investigators are read-only."""
        options, _ = options_for(mesh.report_agent)
        investigator = options.agents["company-investigator"]
        assert "mcp__wiki__wiki_write_page" not in investigator.tools
        assert "mcp__wiki__wiki_search" in investigator.tools


class TestBudgetPropagation:
    def test_request_budget_can_only_lower_the_turn_ceiling(self, mesh):
        options, _ = options_for(mesh.report_agent, budget=Budget(max_turns=5))
        assert options.max_turns == 5

    def test_agent_ceiling_wins_over_a_larger_request_budget(self, mesh):
        options, _ = options_for(mesh.report_agent, budget=Budget(max_turns=10_000))
        assert options.max_turns == mesh.report_agent.max_turns

    async def test_expired_deadline_fails_before_spending_anything(self, mesh):
        response = await mesh.report_agent.run(
            AgentRequest(
                principal=ALICE,
                task="t",
                budget=Budget(max_turns=10, deadline_ts=0.0),
            )
        )
        assert response.status == "failed"
        assert response.error is not None
        assert response.error.code == "budget_exhausted"


class TestCopilotWiring:
    def test_copilot_mounts_wiki_tools_directly_plus_registered_agents(self, mesh):
        copilot = build_copilot(mesh.registry, wiki_backend=mesh.backend, wiki_authz=mesh.authz)
        options, _ = options_for(copilot)
        assert "mcp__wiki__wiki_search" in options.allowed_tools
        assert "mcp__wiki__wiki_read_page" in options.allowed_tools
        assert "mcp__features__wiki_report" in options.allowed_tools

    def test_copilot_wiki_write_is_off_by_default(self, mesh):
        copilot = build_copilot(mesh.registry, wiki_backend=mesh.backend, wiki_authz=mesh.authz)
        options, _ = options_for(copilot)
        assert "mcp__wiki__wiki_write_page" not in options.allowed_tools

    def test_copilot_wiki_write_can_be_turned_on(self, mesh):
        copilot = build_copilot(
            mesh.registry, wiki_backend=mesh.backend, wiki_authz=mesh.authz, wiki_writable=True
        )
        options, _ = options_for(copilot)
        assert "mcp__wiki__wiki_write_page" in options.allowed_tools

    def test_copilot_loads_nothing_from_disk(self, mesh):
        copilot = build_copilot(mesh.registry, wiki_backend=mesh.backend, wiki_authz=mesh.authz)
        options, _ = options_for(copilot)
        assert options.setting_sources == []

    def test_copilot_has_no_builtin_file_or_shell_tools(self, mesh):
        copilot = build_copilot(mesh.registry, wiki_backend=mesh.backend, wiki_authz=mesh.authz)
        options, _ = options_for(copilot)
        assert not any(t in options.allowed_tools for t in ("Bash", "Write", "Edit", "Read"))


class TestPrincipalConstructors:
    def test_service_principal_can_read_and_write_exec(self, mesh):
        svc = service_principal()
        assert EXEC_NAMESPACE in mesh.authz.readable_namespaces(svc)
        assert EXEC_NAMESPACE in mesh.authz.writable_namespaces(svc)

    def test_user_principal_defaults_to_read_only(self):
        p = user_principal("bob", "platform")
        assert p.roles == frozenset({"wiki.reader"})

    def test_user_principal_never_gets_exec_by_default(self):
        p = user_principal("bob", "platform", roles={"wiki.reader", "wiki.writer"})
        assert not p.has_role("wiki.writer.exec")


class TestStructuredOutputParsing:
    def test_last_json_block_wins(self):
        text = '```json\n{"draft": true}\n```\nlater\n```json\n{"final": true}\n```'
        assert _extract_json(text) == {"final": True}

    def test_malformed_block_is_skipped(self):
        text = '```json\n{"good": 1}\n```\n```json\n{not json}\n```'
        assert _extract_json(text) == {"good": 1}

    def test_no_block_returns_empty(self):
        assert _extract_json("just prose") == {}
