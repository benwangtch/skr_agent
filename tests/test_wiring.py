"""Wiring smoke tests: the agents build, and their SDK options are sane.

These stop short of calling the model — they catch the class of mistake that
otherwise only shows up as a runtime error several minutes into a paid run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skr_agent import Principal, build_copilot, build_mesh
from skr_agent.protocol import AgentRequest, Budget
from skr_agent.runtime import ToolContext, _extract_json

ROOT = Path(__file__).resolve().parent.parent
ALICE = Principal(
    subject="alice",
    division="supply",
    roles=frozenset({"wiki.reader", "wiki.writer"}),
)


@pytest.fixture
def mesh():
    return build_mesh(fixtures=ROOT / "fixtures", project_root=ROOT)


def options_for(agent, principal=ALICE, budget=None):
    request = AgentRequest(
        principal=principal, task="t", budget=budget or Budget(max_turns=40)
    )
    ctx = ToolContext(principal=principal, request=request)
    return agent._build_options(ctx), ctx


class TestReportAgentWiring:
    def test_registry_exposes_all_three_capabilities(self, mesh):
        names = {s.name for s in mesh.registry.list()}
        assert names == {"wiki_ask", "wiki_publish", "wiki_report"}

    def test_report_agent_reaches_wiki_only_through_the_coordinator(self, mesh):
        options, _ = options_for(mesh.report_agent)
        wiki_tools = [t for t in options.allowed_tools if "wiki" in t]
        assert sorted(wiki_tools) == ["mcp__wiki__wiki_ask", "mcp__wiki__wiki_publish"]
        # No direct database-shaped tools leak through.
        assert not any("page" in t or "namespace" in t for t in options.allowed_tools)

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

    def test_subagent_cannot_publish(self, mesh):
        """Only the top-level agent writes; investigators are read-only."""
        options, _ = options_for(mesh.report_agent)
        investigator = options.agents["company-investigator"]
        assert "mcp__wiki__wiki_publish" not in investigator.tools


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
    def test_copilot_sees_every_registered_feature_as_one_tool(self, mesh):
        copilot = build_copilot(mesh.registry)
        options, _ = options_for(copilot)
        assert set(options.allowed_tools) == {
            "mcp__features__wiki_ask",
            "mcp__features__wiki_publish",
            "mcp__features__wiki_report",
        }

    def test_copilot_loads_nothing_from_disk(self, mesh):
        copilot = build_copilot(mesh.registry)
        options, _ = options_for(copilot)
        assert options.setting_sources == []

    def test_copilot_has_no_builtin_file_or_shell_tools(self, mesh):
        copilot = build_copilot(mesh.registry)
        options, _ = options_for(copilot)
        assert not any(t in options.allowed_tools for t in ("Bash", "Write", "Edit", "Read"))


class TestStructuredOutputParsing:
    def test_last_json_block_wins(self):
        text = '```json\n{"draft": true}\n```\nlater\n```json\n{"final": true}\n```'
        assert _extract_json(text) == {"final": True}

    def test_malformed_block_is_skipped(self):
        text = '```json\n{"good": 1}\n```\n```json\n{not json}\n```'
        assert _extract_json(text) == {"good": 1}

    def test_no_block_returns_empty(self):
        assert _extract_json("just prose") == {}
