"""Wiring smoke tests: the agents build, and their tool surface is sane.

These stop short of calling the model — they catch the class of mistake that
otherwise only shows up as a runtime error several minutes into a paid run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skr_agent import build_copilot, build_mesh
from skr_agent.principals import service_principal, user_principal
from skr_agent.protocol import AgentRequest, Budget
from skr_agent.runtime import ToolContext, _extract_json, load_skill
from skr_agent.wiki.authz import EXEC_NAMESPACE

ROOT = Path(__file__).resolve().parent.parent
ALICE = user_principal("alice", "supply", roles={"wiki.reader", "wiki.writer"})


@pytest.fixture
def mesh():
    return build_mesh(fixtures=ROOT / "fixtures", project_root=ROOT)


@pytest.fixture
def mesh_with_wiki_agent():
    return build_mesh(fixtures=ROOT / "fixtures", project_root=ROOT, with_wiki_agent=True)


def surface(agent, principal=ALICE, budget=None):
    """The tool names and subagent specs one request would actually get."""
    request = AgentRequest(
        principal=principal, task="t", budget=budget or Budget(max_turns=40)
    )
    ctx = ToolContext(principal=principal, request=request)
    tools, subagents = agent.build_tools(ctx)
    return {t.name for t in tools}, subagents, ctx


class TestSkrAgentWiring:
    def test_registry_exposes_skr_agent_by_default(self, mesh):
        names = {s.name for s in mesh.registry.list()}
        assert names == {"skr_agent"}
        assert mesh.coordinator is None

    def test_wiki_agent_is_opt_in(self, mesh_with_wiki_agent):
        names = {s.name for s in mesh_with_wiki_agent.registry.list()}
        assert names == {"skr_agent", "wiki_ask"}
        assert mesh_with_wiki_agent.coordinator is not None

    def test_wiki_is_reached_only_through_its_own_authorized_tools(self, mesh):
        names, _, _ = surface(mesh.report_agent)
        assert {n for n in names if n.startswith("wiki_")} == {
            "wiki_read_page",
            "wiki_search",
            "wiki_write_page",
        }

    def test_every_data_source_is_mounted(self, mesh):
        """BOM, news and wiki are peers — the agent is not a wiki client."""
        names, _, _ = surface(mesh.report_agent)
        assert {"list_bom_companies", "get_bom_company"} <= names
        assert {"search_news", "fetch_article"} <= names
        assert {"wiki_search", "wiki_read_page"} <= names

    def test_report_rubric_is_inlined_into_the_system_prompt(self, mesh):
        """Not left to progressive disclosure: a mandatory rubric the model
        might forget to read is a rubric that does not exist. See runtime.py."""
        prompt = mesh.report_agent._full_system_prompt()
        assert "Severity rubric" in prompt
        assert "source_refs" in prompt

    def test_skill_file_exists_where_the_loader_will_look(self, mesh):
        assert (ROOT / ".claude" / "skills" / "incident-report" / "SKILL.md").is_file()

    def test_load_skill_strips_frontmatter(self):
        body = load_skill(ROOT, "incident-report")
        assert not body.startswith("---")
        assert "name: incident-report" not in body
        assert body.startswith("#")

    def test_subagent_tools_are_a_subset_of_the_parent_surface(self, mesh):
        names, subagents, _ = surface(mesh.report_agent)
        investigator = subagents[0]
        assert {t.name for t in investigator["tools"]} <= names

    def test_subagent_cannot_write(self, mesh):
        """Only the top-level agent publishes; investigators are read-only."""
        _, subagents, _ = surface(mesh.report_agent)
        sub_tools = {t.name for t in subagents[0]["tools"]}
        assert "wiki_write_page" not in sub_tools
        assert "wiki_search" in sub_tools


class TestBudgetPropagation:
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

    def test_request_budget_can_only_lower_the_turn_ceiling(self):
        parent = Budget(max_turns=10)
        assert parent.child(max_turns=50).max_turns == 10


class TestCopilotWiring:
    def test_copilot_mounts_wiki_tools_directly_plus_registered_agents(self, mesh):
        copilot = build_copilot(mesh.registry, wiki_backend=mesh.backend, wiki_authz=mesh.authz)
        names, _, _ = surface(copilot)
        assert {"wiki_search", "wiki_read_page", "skr_agent"} <= names

    def test_copilot_wiki_write_is_off_by_default(self, mesh):
        copilot = build_copilot(mesh.registry, wiki_backend=mesh.backend, wiki_authz=mesh.authz)
        names, _, _ = surface(copilot)
        assert "wiki_write_page" not in names

    def test_copilot_wiki_write_can_be_turned_on(self, mesh):
        copilot = build_copilot(
            mesh.registry, wiki_backend=mesh.backend, wiki_authz=mesh.authz, wiki_writable=True
        )
        names, _, _ = surface(copilot)
        assert "wiki_write_page" in names

    def test_copilot_loads_no_skills_from_disk(self, mesh):
        copilot = build_copilot(mesh.registry, wiki_backend=mesh.backend, wiki_authz=mesh.authz)
        assert copilot.skills == []


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
