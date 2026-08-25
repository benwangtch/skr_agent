"""The agent works on any research task, with or without a domain.

Two deployments, one agent. A *known* task on a schedule loads a domain; a
user typing an arbitrary question gets no domain at all. These cover that the
second one is a real configuration rather than a degraded one, and that the
safety properties hold in both — by capability, not by a list of tool names
somebody has to remember to update.
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path

import pytest
from langchain_core.tools import StructuredTool

from deep_research_agent import build_mesh
from deep_research_agent.capabilities import (
    is_lookup,
    is_read_only,
    lookup,
    mutating,
    search,
    select_read_only,
)
from deep_research_agent.core.domain import ResearchDomain, Specialist
from deep_research_agent.principals import user_principal
from deep_research_agent.protocol import AgentRequest
from deep_research_agent.runtime import ToolContext

ROOT = Path(__file__).resolve().parent.parent
ALICE = user_principal("alice", "supply", roles={"wiki.reader", "wiki.writer"})


def surface(agent, principal=ALICE):
    request = AgentRequest(principal=principal, task="t")
    ctx = ToolContext(principal=principal, request=request)
    tools, subagents = agent.build_tools(ctx)
    return {t.name for t in tools}, {s["name"]: s for s in subagents}


@pytest.fixture
def general():
    """No domain -- what a user typing an arbitrary question gets."""
    return build_mesh(fixtures=ROOT / "fixtures", project_root=ROOT, domain=None)


@pytest.fixture
def domained():
    """The scheduled deployment, with the supply-chain pack loaded."""
    return build_mesh(fixtures=ROOT / "fixtures", project_root=ROOT)


def a_tool(name: str):
    return StructuredTool.from_function(
        func=lambda **_: "", name=name, description=name,
        args_schema={"type": "object", "properties": {}},
    )


class TestTheAgentRunsWithoutADomain:
    def test_it_builds_and_has_a_working_tool_surface(self, general):
        names, _ = surface(general.agent)
        assert {"wiki_search", "wiki_read_page", "wiki_write_page",
                "check_references"} == names

    def test_the_research_machinery_is_present_anyway(self, general):
        """The parts that make this good at research are not the domain's."""
        prompt = general.agent._full_system_prompt()
        assert "fact-checker" in prompt
        assert "REVISE" in prompt
        assert "/findings" in prompt
        assert "disagree" in prompt
        assert "one source" in prompt or "single source" in prompt

    def test_the_core_subagents_exist(self, general):
        _, subs = surface(general.agent)
        assert set(subs) == {"general-purpose", "fact-checker", "reference-checker"}

    def test_the_prompt_says_nothing_about_supply_chains(self, general):
        prompt = general.agent._full_system_prompt().lower()
        for word in ("bill of materials", "supplier", "bom", "severity"):
            assert word not in prompt, word

    def test_it_still_accepts_free_text(self, general):
        schema = general.agent.as_spec().input_schema
        assert schema["required"] == ["task"]
        assert schema["properties"]["task"]["type"] == "string"

    def test_no_domain_means_no_domain_inputs(self, general, domained):
        assert "tier" not in general.agent.as_spec().input_schema["properties"]
        assert "tier" in domained.agent.as_spec().input_schema["properties"]


class TestADomainOnlyAdds:
    def test_the_domain_keeps_every_generic_section(self, general, domained):
        """A domain briefing must not be able to displace the research method
        -- that is the whole reason the prompt is assembled rather than
        written out per domain."""
        generic = general.agent._full_system_prompt()
        with_domain = domained.agent._full_system_prompt()
        for section in ("# How to work", "# Evidence discipline", "# Verify before publishing"):
            assert section in generic and section in with_domain

    def test_the_domain_adds_its_sources_and_specialist(self, domained):
        names, subs = surface(domained.agent)
        assert {"list_bom_companies", "search_news"} <= names
        assert "company-investigator" in subs

    def test_the_domains_skill_is_inlined(self, domained):
        assert "Severity rubric" in domained.agent._full_system_prompt()


class TestCapabilitiesDecideWhoGetsWhat:
    """Selection is by declared capability, not by a list of tool names. With
    domains and MCP servers a name list can never be complete, and being
    incomplete means a subagent silently holding a tool that mutates."""

    def test_no_subagent_gets_a_mutating_tool(self, domained, general):
        for mesh in (domained, general):
            _, subs = surface(mesh.agent)
            for name, spec in subs.items():
                assert "wiki_write_page" not in {t.name for t in spec["tools"]}, name

    def test_the_fact_checker_gets_lookups_but_not_searches(self, domained):
        _, subs = surface(domained.agent)
        tools = {t.name for t in subs["fact-checker"]["tools"]}
        assert {"wiki_read_page", "fetch_article", "get_bom_company",
                "check_references", "format_reference"} == tools

    def test_the_researcher_gets_every_read_only_tool(self, domained):
        names, subs = surface(domained.agent)
        tools = {t.name for t in subs["general-purpose"]["tools"]}
        assert tools == names - {"wiki_write_page"}

    def test_an_undeclared_tool_is_treated_as_mutating(self):
        """Fail closed. A tool from somewhere we do not control -- an MCP
        server, above all -- cannot be assumed safe because its name reads
        like a lookup."""
        undeclared = a_tool("get_supplier_risk_score")
        assert is_read_only(undeclared) is False
        assert is_lookup(undeclared) is False

    def test_the_three_declarations_mean_what_they_say(self):
        assert is_read_only(lookup(a_tool("x"))) and is_lookup(lookup(a_tool("x")))
        assert is_read_only(search(a_tool("x"))) and not is_lookup(search(a_tool("x")))
        assert not is_read_only(mutating(a_tool("x")))

    def test_declaring_a_capability_keeps_existing_metadata(self):
        """MCP stamps `tool_source` for tracing; declaring must not erase it."""
        tool = a_tool("x")
        tool.metadata = {"tool_source": "mcp"}
        assert lookup(tool).metadata["tool_source"] == "mcp"


class TestADomainCannotWidenTheRules:
    """A domain names the tools its specialist needs. That is a request, not
    an authority -- otherwise every new domain becomes a place the publish
    guarantee could be broken."""

    def test_a_specialist_asking_for_a_mutating_tool_is_refused(self, caplog):
        by_name = {"wiki_write_page": mutating(a_tool("wiki_write_page")),
                   "wiki_read_page": lookup(a_tool("wiki_read_page"))}
        with caplog.at_level(logging.WARNING):
            selected = select_read_only(
                ["wiki_write_page", "wiki_read_page"], by_name, requested_by="greedy"
            )
        assert [t.name for t in selected] == ["wiki_read_page"]
        assert "greedy" in caplog.text

    def test_it_reaches_the_built_agent_too(self):
        greedy = ResearchDomain(
            name="greedy",
            specialists=(
                Specialist(
                    name="publisher",
                    description="d",
                    system_prompt="p",
                    tools=("wiki_write_page", "wiki_read_page"),
                ),
            ),
        )
        mesh = build_mesh(
            fixtures=ROOT / "fixtures", project_root=ROOT, domain=lambda _: greedy
        )
        _, subs = surface(mesh.agent)
        assert {t.name for t in subs["publisher"]["tools"]} == {"wiki_read_page"}

    def test_a_specialist_naming_an_unmounted_tool_is_skipped_quietly(self):
        """A domain may legitimately name a tool this deployment did not
        mount -- that is not an error, it is a smaller agent."""
        assert select_read_only(["nope"], {}, requested_by="x") == []


class TestAReadOnlyDeployment:
    """`publishable=False` removes the capability *and* the prompt section
    describing it, so the agent does not spend turns on a call it cannot
    make."""

    @pytest.fixture
    def read_only_agent(self):
        from deep_research_agent.core.agent import build_research_agent
        from deep_research_agent.wiki import InMemoryWikiBackend, WikiAuthorizer

        return build_research_agent(
            wiki_backend=InMemoryWikiBackend.from_fixtures(ROOT / "fixtures"),
            wiki_authz=WikiAuthorizer(),
            project_root=ROOT,
            publishable=False,
        )

    def test_the_write_tool_is_absent(self, read_only_agent):
        names, _ = surface(read_only_agent)
        assert "wiki_write_page" not in names

    def test_the_publishing_section_is_absent(self, read_only_agent):
        assert "# Publishing" not in read_only_agent._full_system_prompt()

    def test_the_answer_becomes_the_deliverable(self, read_only_agent):
        """With nowhere to publish, a short summary would just lose the work."""
        assert "final message is the deliverable" in read_only_agent._full_system_prompt()

    def test_it_still_verifies(self, read_only_agent):
        """Not publishing is not a reason to stop checking claims."""
        _, subs = surface(read_only_agent)
        assert "fact-checker" in subs


class TestAddingANewDomain:
    """The test of whether the split is real: a new domain is a value, not an
    edit to core."""

    def test_a_domain_defined_here_works_end_to_end(self):
        briefed = ResearchDomain(
            name="patents",
            summary="Researches patent exposure.",
            briefing="# What you are researching\n\nYou research patent filings.",
            specialists=(
                Specialist(
                    name="claim-reader",
                    description="Reads one patent's claims.",
                    system_prompt="Read the claims.",
                    tools=("wiki_read_page",),
                ),
            ),
            inputs={"jurisdiction": {"type": "string"}},
        )
        mesh = build_mesh(
            fixtures=ROOT / "fixtures", project_root=ROOT, domain=lambda _: briefed
        )
        prompt = mesh.agent._full_system_prompt()
        assert "You research patent filings." in prompt
        assert "# Evidence discipline" in prompt
        assert mesh.agent.description == "Researches patent exposure."
        assert "jurisdiction" in mesh.agent.as_spec().input_schema["properties"]

        _, subs = surface(mesh.agent)
        assert set(subs) == {
            "general-purpose", "fact-checker", "reference-checker", "claim-reader"
        }

    def test_a_domain_is_immutable_so_it_can_be_shared(self):
        domain = ResearchDomain(name="x")
        with pytest.raises(dataclasses.FrozenInstanceError):
            domain.name = "y"
