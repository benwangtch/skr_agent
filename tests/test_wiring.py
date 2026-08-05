"""Wiring smoke tests: the agents build, and their tool surface is sane.

These stop short of calling the model — they catch the class of mistake that
otherwise only shows up as a runtime error several minutes into a paid run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skr_agent import build_mesh
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


class TestEmbeddingSkrAgentInAnotherAgent:
    """skr agent's in-process integration surface: `agent_as_tool` wraps it so
    a caller's model can invoke it. No agent in this repo does that any more —
    this is the seam a consumer outside it uses, so it stays covered."""

    def test_skr_agent_can_be_mounted_as_a_tool_on_another_agent(self, mesh):
        from skr_agent.mesh import agents_as_tools
        from skr_agent.runtime import DeepAgent

        def feature_tools(ctx):
            return agents_as_tools(
                mesh.registry.list(), principal=ctx.principal, parent="caller"
            )

        caller = DeepAgent(
            name="caller",
            description="an agent that delegates to skr agent",
            system_prompt="delegate",
            toolsets=[feature_tools],
        )
        names, _, _ = surface(caller)
        assert names == {"skr_agent"}

    def test_a_read_only_wiki_toolset_omits_the_write_tool(self, mesh):
        """`writable=False` removes the capability rather than refusing it —
        what a read-only consumer mounts."""
        from skr_agent.runtime import DeepAgent
        from skr_agent.wiki.tools import make_wiki_toolset

        reader = DeepAgent(
            name="reader",
            description="read-only wiki consumer",
            system_prompt="read",
            toolsets=[make_wiki_toolset(mesh.backend, mesh.authz, writable=False)],
        )
        names, _, _ = surface(reader)
        assert {"wiki_search", "wiki_read_page"} <= names
        assert "wiki_write_page" not in names


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


class TestAddingYourOwnSkill:
    """Adding a skill is: create `.claude/skills/<name>/SKILL.md`, then name it
    in `skills=`. These lock down that the second one is treated like the first
    -- inlined in full, not merely advertised."""

    def test_a_second_skill_is_inlined_too(self, tmp_path):
        from skr_agent.report import build_skr_agent
        from skr_agent.report.sources import FixtureBom, FixtureNewsFeed
        from skr_agent.wiki import InMemoryWikiBackend, WikiAuthorizer

        skill_dir = tmp_path / ".claude" / "skills" / "house-style"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: house-style\ndescription: how we write\n---\n\n"
            "# House style\n\nAlways lead with the risk.\n",
            encoding="utf-8",
        )
        # The built-in skill has to exist under the same root to be found.
        builtin = tmp_path / ".claude" / "skills" / "incident-report"
        builtin.mkdir(parents=True)
        (builtin / "SKILL.md").write_text("# Rubric\n\nSeverity rubric here.\n", encoding="utf-8")

        agent = build_skr_agent(
            bom=FixtureBom.from_fixtures(ROOT / "fixtures"),
            news=FixtureNewsFeed.from_fixtures(ROOT / "fixtures"),
            wiki_backend=InMemoryWikiBackend.from_fixtures(ROOT / "fixtures"),
            wiki_authz=WikiAuthorizer(),
            project_root=tmp_path,
            skills=["incident-report", "house-style"],
        )
        prompt = agent._full_system_prompt()
        assert "Always lead with the risk." in prompt
        assert "Severity rubric here." in prompt

    def test_a_missing_skill_fails_loudly_at_build_time(self, tmp_path):
        """Better than a run that silently ignores the rubric it was told to
        follow."""
        from skr_agent.runtime import load_skill

        with pytest.raises(FileNotFoundError):
            load_skill(tmp_path, "no-such-skill")
