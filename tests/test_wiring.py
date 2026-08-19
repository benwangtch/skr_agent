"""Wiring smoke tests: the agents build, and their tool surface is sane.

These stop short of calling the model — they catch the class of mistake that
otherwise only shows up as a runtime error several minutes into a paid run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deep_research_agent import build_mesh
from deep_research_agent.principals import service_principal, user_principal
from deep_research_agent.protocol import AgentRequest, Budget
from deep_research_agent.runtime import ToolContext, _extract_json, load_skill
from deep_research_agent.wiki.authz import EXEC_NAMESPACE

ROOT = Path(__file__).resolve().parent.parent
ALICE = user_principal("alice", "supply", roles={"wiki.reader", "wiki.writer"})


@pytest.fixture
def mesh():
    return build_mesh(fixtures=ROOT / "fixtures", project_root=ROOT)


@pytest.fixture
def mesh_with_wiki_agent():
    return build_mesh(fixtures=ROOT / "fixtures", project_root=ROOT, with_wiki_agent=True)


def by_name(subagents):
    return {s["name"]: s for s in subagents}


def surface(agent, principal=ALICE, budget=None):
    """The tool names and subagent specs one request would actually get."""
    request = AgentRequest(
        principal=principal, task="t", budget=budget or Budget(max_turns=40)
    )
    ctx = ToolContext(principal=principal, request=request)
    tools, subagents = agent.build_tools(ctx)
    return {t.name for t in tools}, subagents, ctx


class TestDeepResearchAgentWiring:
    def test_registry_exposes_deep_research_agent_by_default(self, mesh):
        names = {s.name for s in mesh.registry.list()}
        assert names == {"deep_research_agent"}
        assert mesh.coordinator is None

    def test_wiki_agent_is_opt_in(self, mesh_with_wiki_agent):
        names = {s.name for s in mesh_with_wiki_agent.registry.list()}
        assert names == {"deep_research_agent", "wiki_ask"}
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
        assert (ROOT / "skills" / "incident-report" / "SKILL.md").is_file()

    def test_load_skill_strips_frontmatter(self):
        body = load_skill(ROOT, "incident-report")
        assert not body.startswith("---")
        assert "name: incident-report" not in body
        assert body.startswith("#")

    def test_subagent_tools_are_a_subset_of_the_parent_surface(self, mesh):
        names, subagents, _ = surface(mesh.report_agent)
        for sub in subagents:
            assert {t.name for t in sub["tools"]} <= names, sub["name"]

    def test_no_subagent_can_publish(self, mesh):
        """Only the top-level agent publishes."""
        _, subagents, _ = surface(mesh.report_agent)
        for sub in subagents:
            assert "wiki_write_page" not in {t.name for t in sub["tools"]}, sub["name"]

    def test_the_investigator_can_still_research(self, mesh):
        _, subagents, _ = surface(mesh.report_agent)
        investigator = by_name(subagents)["company-investigator"]
        assert {"wiki_search", "search_news"} <= {t.name for t in investigator["tools"]}


class TestDeepResearchStructure:
    """The parts that make this a research agent rather than a lookup: a
    verification pass, notes on a scratchpad, and an explicit stopping check."""

    def test_a_fact_checker_subagent_exists(self, mesh):
        _, subagents, _ = surface(mesh.report_agent)
        assert "fact-checker" in by_name(subagents)

    def test_the_fact_checker_cannot_search(self, mesh):
        """A checker that can go find new material starts researching instead
        of checking, and 'confirms' claims from sources the report never
        cited."""
        _, subagents, _ = surface(mesh.report_agent)
        checker_tools = {t.name for t in by_name(subagents)["fact-checker"]["tools"]}
        assert "search_news" not in checker_tools
        assert "wiki_search" not in checker_tools
        # It can still re-read a cited source, which is the whole job.
        assert {"fetch_article", "wiki_read_page"} <= checker_tools

    def test_the_lead_is_told_to_verify_before_publishing(self, mesh):
        prompt = mesh.report_agent._full_system_prompt()
        assert "fact-checker" in prompt
        assert "REVISE" in prompt

    def test_the_lead_is_told_to_read_the_findings_files(self, mesh):
        """The scratchpad only helps if the lead synthesises from the files
        rather than from the subagents' short replies."""
        prompt = mesh.report_agent._full_system_prompt()
        assert "read_file" in prompt
        assert "/findings/" in prompt

    def test_the_investigator_is_told_to_write_its_findings_file(self):
        from deep_research_agent.report.agent import INVESTIGATOR_PROMPT

        assert "/findings/" in INVESTIGATOR_PROMPT

    def test_the_lead_has_an_explicit_stopping_check(self, mesh):
        prompt = mesh.report_agent._full_system_prompt()
        assert "single source" in prompt or "one source" in prompt

    def test_contradictions_must_be_surfaced_not_resolved_silently(self, mesh):
        prompt = mesh.report_agent._full_system_prompt()
        assert "disagree" in prompt


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


class TestEmbeddingDeepResearchAgentInAnotherAgent:
    """The agent's in-process integration surface: `agent_as_tool` wraps it so
    a caller's model can invoke it. No agent in this repo does that any more —
    this is the seam a consumer outside it uses, so it stays covered."""

    def test_deep_research_agent_can_be_mounted_as_a_tool_on_another_agent(self, mesh):
        from deep_research_agent.mesh import agents_as_tools
        from deep_research_agent.runtime import DeepAgent

        def feature_tools(ctx):
            return agents_as_tools(
                mesh.registry.list(), principal=ctx.principal, parent="caller"
            )

        caller = DeepAgent(
            name="caller",
            description="an agent that delegates to the research agent",
            system_prompt="delegate",
            toolsets=[feature_tools],
        )
        names, _, _ = surface(caller)
        assert names == {"deep_research_agent"}

    def test_a_read_only_wiki_toolset_omits_the_write_tool(self, mesh):
        """`writable=False` removes the capability rather than refusing it —
        what a read-only consumer mounts."""
        from deep_research_agent.runtime import DeepAgent
        from deep_research_agent.wiki.tools import make_wiki_toolset

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
    """Adding a skill is: create `skills/<name>/SKILL.md`, then name it in
    `skills=`. These lock down that the second one is treated like the first
    -- inlined in full, not merely advertised."""

    def test_a_second_skill_is_inlined_too(self, tmp_path):
        from deep_research_agent.report import build_deep_research_agent
        from deep_research_agent.report.sources import FixtureBom, FixtureNewsFeed
        from deep_research_agent.wiki import InMemoryWikiBackend, WikiAuthorizer

        skill_dir = tmp_path / "skills" / "house-style"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: house-style\ndescription: how we write\n---\n\n"
            "# House style\n\nAlways lead with the risk.\n",
            encoding="utf-8",
        )
        # The built-in skill has to exist under the same root to be found.
        builtin = tmp_path / "skills" / "incident-report"
        builtin.mkdir(parents=True)
        (builtin / "SKILL.md").write_text("# Rubric\n\nSeverity rubric here.\n", encoding="utf-8")

        agent = build_deep_research_agent(
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
        from deep_research_agent.runtime import load_skill

        with pytest.raises(FileNotFoundError):
            load_skill(tmp_path, "no-such-skill")


class TestImportStyle:
    """Imports inside the package are absolute (`from deep_research_agent.x import y`).

    A convention with no check drifts back within a few commits, and the
    failure is silent — a relative import works fine until someone moves the
    module. This keeps it honest.
    """

    def test_no_relative_imports_in_the_package(self):
        import re

        src = ROOT / "src"
        relative = re.compile(r"^\s*from \.", re.M)
        offenders = [
            str(f.relative_to(ROOT))
            for f in src.rglob("*.py")
            if relative.search(f.read_text())
        ]
        assert offenders == [], (
            "relative imports found; this package uses absolute imports "
            f"(from deep_research_agent.…): {offenders}"
        )


class TestRetrievingThePublishedReport:
    """The agent's final message is a short summary by design -- the report is
    the wiki page it wrote. With an in-memory backend that page is unreachable
    once the process exits unless something reads it back out, which is what
    `page_refs()` / `get()` and `run_report.py --out` exist for."""

    def test_page_refs_snapshots_what_exists(self, mesh):
        before = mesh.backend.page_refs()
        assert before, "fixtures should provide some pages"
        assert all("/" in ref for ref in before)

    def test_a_new_page_shows_up_in_the_diff(self, mesh):
        from deep_research_agent.wiki.backend import WikiPage

        before = mesh.backend.page_refs()
        mesh.backend.upsert_page(
            WikiPage(
                namespace="supply",
                slug="incident-report-2026-32",
                title="Report",
                body="body",
                source_refs=["rpt-1"],
            )
        )
        assert mesh.backend.page_refs() - before == {"supply/incident-report-2026-32"}

    def test_the_published_page_can_be_read_back_in_full(self, mesh):
        from deep_research_agent.wiki.backend import WikiPage

        mesh.backend.upsert_page(
            WikiPage(
                namespace="supply",
                slug="r1",
                title="Report",
                body="## Summary\nAcme is down.",
                source_refs=["rpt-1"],
            )
        )
        page = mesh.backend.get("supply/r1")
        assert page is not None
        assert "Acme is down." in page.body
        assert page.source_refs == ["rpt-1"]

    def test_get_returns_none_for_an_unknown_ref(self, mesh):
        assert mesh.backend.get("supply/no-such-page") is None


class TestLoadingASkillYouMaintain:
    """A skill you maintain usually lives outside this repo. Copying it in
    works until the copy and the original disagree, so names resolve across a
    search path and paths resolve literally."""

    @pytest.fixture
    def external(self, tmp_path):
        d = tmp_path / "skills" / "house-style"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            "---\nname: house-style\ndescription: how we write\n---\n\n"
            "# House style\n\nLead with the exposure.\n",
            encoding="utf-8",
        )
        return tmp_path / "skills"

    def test_a_directory_path_resolves_literally(self, external):
        from deep_research_agent.runtime import load_skill

        assert "Lead with the exposure." in load_skill(".", str(external / "house-style"))

    def test_a_skill_md_path_resolves_literally(self, external):
        from deep_research_agent.runtime import load_skill

        body = load_skill(".", str(external / "house-style" / "SKILL.md"))
        assert "Lead with the exposure." in body

    def test_frontmatter_is_stripped_from_an_external_skill(self, external):
        from deep_research_agent.runtime import load_skill

        body = load_skill(".", str(external / "house-style"))
        assert "description: how we write" not in body
        assert body.startswith("#")

    def test_skills_path_makes_a_name_resolvable(self, external, monkeypatch):
        from deep_research_agent.config import reset_settings_cache
        from deep_research_agent.runtime import load_skill

        monkeypatch.setenv("SKILLS_PATH", str(external))
        reset_settings_cache()
        try:
            assert "Lead with the exposure." in load_skill(".", "house-style")
        finally:
            reset_settings_cache()

    def test_skills_path_shadows_a_built_in_of_the_same_name(self, tmp_path, monkeypatch):
        """Override a built-in rubric without editing the repo's copy."""
        from deep_research_agent.config import reset_settings_cache
        from deep_research_agent.runtime import load_skill

        d = tmp_path / "incident-report"
        d.mkdir()
        (d / "SKILL.md").write_text("# OVERRIDDEN", encoding="utf-8")
        monkeypatch.setenv("SKILLS_PATH", str(tmp_path))
        reset_settings_cache()
        try:
            assert load_skill(ROOT, "incident-report") == "# OVERRIDDEN"
        finally:
            reset_settings_cache()

    def test_skills_enabled_adds_without_replacing_the_default(self, external, monkeypatch):
        """An env var that could silently drop the report rubric would be a
        footgun -- SKILLS_ENABLED is additive."""
        from deep_research_agent.config import reset_settings_cache

        monkeypatch.setenv("SKILLS_PATH", str(external))
        monkeypatch.setenv("SKILLS_ENABLED", "house-style")
        reset_settings_cache()
        try:
            mesh = build_mesh(fixtures=ROOT / "fixtures", project_root=ROOT)
            assert mesh.report_agent.skills == ["incident-report", "house-style"]
            prompt = mesh.report_agent._full_system_prompt()
            assert "Lead with the exposure." in prompt
            assert "Severity rubric" in prompt
        finally:
            reset_settings_cache()

    def test_a_name_listed_twice_is_not_loaded_twice(self, monkeypatch):
        from deep_research_agent.config import reset_settings_cache

        monkeypatch.setenv("SKILLS_ENABLED", "incident-report")
        reset_settings_cache()
        try:
            mesh = build_mesh(fixtures=ROOT / "fixtures", project_root=ROOT)
            assert mesh.report_agent.skills == ["incident-report"]
        finally:
            reset_settings_cache()

    def test_a_missing_skill_names_every_path_it_tried(self, tmp_path):
        from deep_research_agent.runtime import load_skill

        with pytest.raises(FileNotFoundError) as exc:
            load_skill(tmp_path, "no-such-skill")
        assert "skills/no-such-skill/SKILL.md" in str(exc.value)


class TestTheRepoSkillsFolder:
    """A skill a scheduled job depends on belongs in the repo, versioned with
    the code. These cover that the folder works with no configuration at all,
    and that a skill dropped in but never wired up is visible rather than
    silently ignored."""

    def test_the_repo_folder_needs_no_configuration(self):
        from deep_research_agent.runtime import skill_roots

        roots = [str(r) for r in skill_roots(ROOT)]
        assert str(ROOT / "skills") in roots

    def test_the_legacy_claude_folder_still_resolves(self):
        """Older checkouts and Claude Code both use .claude/skills."""
        from deep_research_agent.runtime import skill_roots

        assert str(ROOT / ".claude/skills") in [str(r) for r in skill_roots(ROOT)]

    def test_the_repo_skill_is_discovered(self):
        from deep_research_agent.runtime import discover_skills

        assert "incident-report" in discover_skills(ROOT)

    def test_discovery_finds_a_dropped_in_folder(self, tmp_path):
        from deep_research_agent.runtime import discover_skills

        d = tmp_path / "skills" / "house-style"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("# House style", encoding="utf-8")
        assert set(discover_skills(tmp_path)) == {"house-style"}

    def test_a_directory_without_a_skill_md_is_not_a_skill(self, tmp_path):
        from deep_research_agent.runtime import discover_skills

        (tmp_path / "skills" / "notes").mkdir(parents=True)
        assert discover_skills(tmp_path) == {}

    def test_skills_beats_dot_claude_for_the_same_name(self, tmp_path):
        """Both folders resolve; the visible one wins."""
        from deep_research_agent.runtime import load_skill

        for parent, body in (("skills", "# NEW"), (".claude/skills", "# OLD")):
            d = tmp_path / parent / "dup"
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(body, encoding="utf-8")
        assert load_skill(tmp_path, "dup") == "# NEW"

    def test_an_unloaded_repo_skill_is_warned_about(self, tmp_path, caplog):
        """The failure mode of a folder convention is a file that looks
        installed and silently is not."""
        import logging

        from deep_research_agent.report.agent import _warn_about_unloaded_skills

        d = tmp_path / "skills" / "house-style"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("# House style", encoding="utf-8")

        with caplog.at_level(logging.WARNING):
            _warn_about_unloaded_skills(tmp_path, ["incident-report"])
        assert "house-style" in caplog.text

    def test_no_warning_when_everything_present_is_loaded(self, tmp_path, caplog):
        import logging

        from deep_research_agent.report.agent import _warn_about_unloaded_skills

        d = tmp_path / "skills" / "house-style"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("# House style", encoding="utf-8")

        with caplog.at_level(logging.WARNING):
            _warn_about_unloaded_skills(tmp_path, ["house-style"])
        assert "not loaded" not in caplog.text
