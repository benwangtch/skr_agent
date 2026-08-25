"""The reference check: format, shape, resolvability, declaration.

A checker is only useful if it is trusted, and it is trusted only if it does
not cry wolf. So these cover both directions with equal weight — that a real
defect is caught, and that a correct draft produces silence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deep_research_agent import build_mesh
from deep_research_agent.core.references import (
    DEFAULT_FORMAT,
    ReferenceFormat,
    check_references,
    extract_references,
)
from deep_research_agent.principals import user_principal
from deep_research_agent.protocol import AgentRequest
from deep_research_agent.runtime import ToolContext

ROOT = Path(__file__).resolve().parent.parent

CORPUS = {
    "supply/acme-semiconductor": {"title": "Acme Semiconductor", "kind": "wiki_page"},
    "rpt-supply-2026-W28": {"title": "supply weekly, week of 2026-W28", "kind": "raw_report"},
    "https://news.test/acme-fire": {
        "title": "Fire at Acme fab", "kind": "external_url", "published": "2026-08-01",
    },
}

CLEAN = """# Report

## Summary
One critical finding.

## Findings

### Acme Semiconductor Ltd — critical
- **What happened:** fire at the fab.
- **Sources:** supply/acme-semiconductor, https://news.test/acme-fire

### Gamma Industries — none
- **Queries run:** "Gamma Industries", "Gamma Ind."
- **Result:** no external signal found.

## Coverage
Two companies scanned.
"""


def kinds(report):
    return [v.kind for v in report.violations]


class TestACorrectDraftIsSilent:
    """The half that decides whether anyone keeps using this."""

    def test_a_clean_draft_passes_with_nothing_to_say(self):
        report = check_references(CLEAN, corpus=CORPUS)
        assert report.violations == ()
        assert report.passed

    def test_prose_outside_a_findings_section_needs_no_citation(self):
        """Demanding a source on "two companies scanned" produces noise that
        trains people to ignore the checker."""
        assert "unsourced_section" not in kinds(check_references(CLEAN, corpus=CORPUS))

    def test_a_none_section_with_its_queries_is_accepted(self):
        assert "unexplained_absence" not in kinds(check_references(CLEAN, corpus=CORPUS))

    def test_a_retrieved_but_unquoted_source_is_not_a_leftover(self):
        """Reading a wiki page also returns the raw reports behind it.
        Declaring those is what the provenance rule asks for -- warning about
        it would be exactly the noise this checker must not produce."""
        report = check_references(
            CLEAN, corpus=CORPUS,
            declared=["supply/acme-semiconductor", "https://news.test/acme-fire",
                      "rpt-supply-2026-W28"],
        )
        assert report.violations == ()


class TestResolvability:
    """Not a grounding check, deliberately. The question is whether a
    reference can be rendered in the required form -- which needs the loaded
    document -- not whether the agent was entitled to cite it."""

    def test_a_reference_nothing_loaded_describes_is_a_warning(self):
        """The corpus is not guaranteed complete, so this cannot be an error
        without producing the false positives that get a checker ignored."""
        draft = CLEAN.replace("https://news.test/acme-fire", "https://elsewhere.test/story")
        report = check_references(draft, corpus=CORPUS)
        assert "unresolvable_reference" in kinds(report)
        assert report.passed, "an incomplete corpus must not block a publish"

    def test_the_message_names_the_reference_and_the_line(self):
        """A defect you have to re-read the draft to locate is half a defect."""
        draft = CLEAN.replace("https://news.test/acme-fire", "https://elsewhere.test/story")
        violation = next(
            v for v in check_references(draft, corpus=CORPUS).violations
            if v.kind == "unresolvable_reference"
        )
        assert "elsewhere.test" in violation.message
        assert violation.line > 0

    def test_a_format_needing_a_date_reports_a_source_without_one(self):
        """The reason the store keeps documents rather than refs: "this cannot
        be cited in the required form" is a fact about the document."""
        fmt = ReferenceFormat(required_document_fields=("published",))
        report = check_references(CLEAN, corpus=CORPUS, fmt=fmt)
        defects = [v for v in report.violations if v.kind == "incomplete_reference"]
        assert [v.message for v in defects] and "published" in defects[0].message
        # The news article has a date; the wiki page does not.
        assert all("acme-semiconductor" in v.message for v in defects)

    def test_the_default_format_demands_no_document_fields(self):
        """The shipped rubric asks for a bare reference. Demanding a date from
        a source that has none reports a defect the agent cannot fix."""
        assert DEFAULT_FORMAT.required_document_fields == ()
        assert check_references(CLEAN, corpus=CORPUS).violations == ()

    def test_no_corpus_skips_the_check(self):
        assert "unresolvable_reference" not in kinds(check_references(CLEAN, corpus=None))


class TestFormat:
    def test_a_section_with_no_sources_line_is_caught(self):
        draft = CLEAN.replace(
            "- **Sources:** supply/acme-semiconductor, https://news.test/acme-fire", ""
        )
        assert "unsourced_section" in kinds(check_references(draft, corpus=CORPUS))

    def test_a_sources_line_naming_nothing_is_caught(self):
        draft = CLEAN.replace(
            "- **Sources:** supply/acme-semiconductor, https://news.test/acme-fire",
            "- **Sources:** see above",
        )
        assert "empty_sources" in kinds(check_references(draft, corpus=CORPUS))

    def test_a_none_section_without_its_queries_is_caught(self):
        """Absence is a finding. Without the queries a reader cannot tell
        "checked, found nothing" from "not checked"."""
        draft = CLEAN.replace('- **Queries run:** "Gamma Industries", "Gamma Ind."\n', "")
        assert "unexplained_absence" in kinds(check_references(draft, corpus=CORPUS))

    def test_a_none_section_is_not_asked_for_sources(self):
        draft = CLEAN.replace('- **Queries run:** "Gamma Industries", "Gamma Ind."\n', "")
        assert "unsourced_section" not in kinds(check_references(draft, corpus=CORPUS))

    def test_every_defective_section_is_reported_not_just_the_first(self):
        """The reason this is a parser and not a model: exhaustive counting."""
        draft = CLEAN + "\n".join(
            f"### Company {n} — high\n- **What happened:** something.\n" for n in range(8)
        )
        assert kinds(check_references(draft, corpus=CORPUS)).count("unsourced_section") == 8


class TestDeclaration:
    def test_a_body_reference_missing_from_source_refs_is_an_error(self):
        report = check_references(
            CLEAN, corpus=CORPUS, declared=["supply/acme-semiconductor"]
        )
        assert "undeclared_reference" in kinds(report)

    def test_an_empty_declaration_is_an_error(self):
        assert "no_declared_sources" in kinds(
            check_references(CLEAN, corpus=CORPUS, declared=[])
        )

    def test_a_stale_declaration_is_only_a_warning(self):
        """It makes the page untidy, not wrong. Blocking a publish on it would
        make the checker the thing people route around."""
        report = check_references(
            CLEAN, corpus=CORPUS,
            declared=[*CORPUS, "rpt-old-2019-W01"],
        )
        assert kinds(report) == ["unused_declaration"]
        assert report.passed

    def test_no_declaration_means_the_cross_check_is_skipped(self):
        """Mid-draft there is no publish call to compare against."""
        assert check_references(CLEAN, corpus=CORPUS, declared=None).violations == ()


class TestReferenceShapes:
    def test_the_three_shapes_are_recognised(self):
        found = extract_references(
            "see https://x.test/a and supply/acme-semiconductor and rpt-supply-2026-W28"
        )
        assert set(found) == {
            "https://x.test/a", "supply/acme-semiconductor", "rpt-supply-2026-W28"
        }

    def test_a_url_path_is_not_also_read_as_a_page_reference(self):
        """Without consuming URLs first, "example.com/path" inside a link
        matches the namespace/slug pattern and the same source is reported
        twice under two shapes."""
        assert extract_references("https://news.test/acme/fire") == ["https://news.test/acme/fire"]

    def test_trailing_punctuation_is_not_part_of_a_reference(self):
        assert extract_references("per https://x.test/a, and more") == ["https://x.test/a"]

    def test_references_are_deduplicated_in_first_seen_order(self):
        text = "supply/b and supply/a and supply/b"
        assert extract_references(text) == ["supply/b", "supply/a"]


class TestTheFormatIsDefinable:
    """A domain whose deliverable is laid out differently supplies its own
    rather than editing the core one."""

    def test_a_custom_section_marker_is_honoured(self):
        fmt = ReferenceFormat(section_pattern=r"^##\s+(?P<title>.+?)\s*$")
        draft = "## Claim one\nSomething happened.\n"
        assert "unsourced_section" in kinds(check_references(draft, fmt=fmt))

    def test_a_custom_sources_marker_is_honoured(self):
        fmt = ReferenceFormat(sources_marker="Refs:")
        draft = "### Claim\nSomething.\nRefs: supply/a\n"
        assert check_references(draft, fmt=fmt).violations == ()

    def test_the_default_matches_the_shipped_rubric(self):
        """The repo's own incident-report rubric asks for exactly these."""
        rubric = (ROOT / "skills" / "incident-report" / "SKILL.md").read_text()
        assert DEFAULT_FORMAT.sources_marker in rubric
        assert DEFAULT_FORMAT.exempt_marker in rubric


class TestItIsWiredIntoTheAgent:
    @pytest.fixture
    def surface(self):
        principal = user_principal("a", "supply", roles={"wiki.reader", "wiki.writer"})
        ctx = ToolContext(
            principal=principal,
            request=AgentRequest(principal=principal, task="t"),
        )
        mesh = build_mesh(fixtures=ROOT / "fixtures", project_root=ROOT)
        tools, subagents = mesh.agent.build_tools(ctx)
        return mesh, {t.name for t in tools}, {s["name"]: s for s in subagents}

    def test_the_tool_is_mounted(self, surface):
        _, names, _ = surface
        assert "check_references" in names

    def test_the_subagent_exists_and_holds_the_tool(self, surface):
        """Without it the subagent would be reduced to eyeballing the draft --
        the exact thing its prompt tells it not to do."""
        _, _, subs = surface
        assert "reference-checker" in subs
        assert "check_references" in {t.name for t in subs["reference-checker"]["tools"]}

    def test_the_checker_cannot_publish(self, surface):
        _, _, subs = surface
        assert "wiki_write_page" not in {t.name for t in subs["reference-checker"]["tools"]}

    def test_the_lead_is_told_to_run_it(self, surface):
        mesh, _, _ = surface
        prompt = mesh.agent._full_system_prompt()
        assert "check_references" in prompt
        assert "reference-checker" in prompt

    def test_turning_it_off_removes_the_tool_the_subagent_and_the_prompt(self):
        """A deployment with no citation convention should not be handed a
        checker that reports every section as unsourced."""
        from deep_research_agent.core.agent import build_research_agent
        from deep_research_agent.wiki import InMemoryWikiBackend, WikiAuthorizer

        agent = build_research_agent(
            wiki_backend=InMemoryWikiBackend.from_fixtures(ROOT / "fixtures"),
            wiki_authz=WikiAuthorizer(),
            project_root=ROOT,
            check_references=False,
        )
        principal = user_principal("a", "supply", roles={"wiki.reader"})
        ctx = ToolContext(
            principal=principal, request=AgentRequest(principal=principal, task="t")
        )
        tools, subagents = agent.build_tools(ctx)
        assert "check_references" not in {t.name for t in tools}
        assert "reference-checker" not in {s["name"] for s in subagents}
        assert "check_references" not in agent._full_system_prompt()


class TestTheToolSeesWhatTheRunLoaded:
    """The checker's corpus is the retrieval store the tools write to."""

    async def test_a_page_read_during_the_run_becomes_resolvable(self):
        principal = user_principal("a", "supply", roles={"wiki.reader"})
        ctx = ToolContext(
            principal=principal, request=AgentRequest(principal=principal, task="t")
        )
        mesh = build_mesh(fixtures=ROOT / "fixtures", project_root=ROOT)
        by_name = {t.name: t for t in mesh.agent.build_tools(ctx)[0]}

        draft = (
            "### Acme — critical\n- **What happened:** a fire.\n"
            "- **Sources:** supply/acme-semiconductor\n"
        )

        before = await by_name["check_references"].ainvoke({"content": draft})
        assert "unresolvable_reference" in before

        await by_name["wiki_read_page"].ainvoke({"ref": "supply/acme-semiconductor"})

        after = await by_name["check_references"].ainvoke({"content": draft})
        assert "unresolvable_reference" not in after

    async def test_reading_a_page_stores_its_content_not_just_its_ref(self):
        """The store exists so something can work on the documents afterwards;
        a list of refs would not support formatting a citation."""
        principal = user_principal("a", "supply", roles={"wiki.reader"})
        ctx = ToolContext(
            principal=principal, request=AgentRequest(principal=principal, task="t")
        )
        mesh = build_mesh(fixtures=ROOT / "fixtures", project_root=ROOT)
        by_name = {t.name: t for t in mesh.agent.build_tools(ctx)[0]}

        await by_name["wiki_read_page"].ainvoke({"ref": "supply/acme-semiconductor"})

        page = ctx.document("supply/acme-semiconductor")
        assert page is not None
        assert page.title and page.content
        assert page.metadata["namespace"] == "supply"

    async def test_the_raw_reports_behind_a_page_are_stored_too(self):
        principal = user_principal("a", "supply", roles={"wiki.reader"})
        ctx = ToolContext(
            principal=principal, request=AgentRequest(principal=principal, task="t")
        )
        mesh = build_mesh(fixtures=ROOT / "fixtures", project_root=ROOT)
        by_name = {t.name: t for t in mesh.agent.build_tools(ctx)[0]}

        await by_name["wiki_read_page"].ainvoke({"ref": "supply/acme-semiconductor"})
        assert any(d.kind == "raw_report" and d.content for d in ctx.documents)


class TestThePublishGate:
    """A prompt instruction to "check before publishing" is a request the
    model can skip. This is the same move source_refs already makes: refuse
    the write, and say what would make it succeed."""

    @pytest.fixture
    def wiki(self):
        from deep_research_agent.wiki import InMemoryWikiBackend, WikiAuthorizer
        from deep_research_agent.wiki.tools import build_wiki_tools

        principal = user_principal("a", "supply", roles={"wiki.reader", "wiki.writer"})
        ctx = ToolContext(
            principal=principal, request=AgentRequest(principal=principal, task="t")
        )
        tools = build_wiki_tools(
            InMemoryWikiBackend.from_fixtures(ROOT / "fixtures"),
            WikiAuthorizer(),
            ctx,
            require_reference_check=True,
        )
        return ctx, {t.name: t for t in tools}

    def write_args(self, body):
        return {
            "namespace": "supply", "slug": "r1", "title": "R",
            "body": body, "source_refs": ["rpt-supply-2026-W28"],
        }

    async def test_an_unchecked_body_is_refused(self, wiki):
        _, by_name = wiki
        result = await by_name["wiki_write_page"].ainvoke(self.write_args("# Draft\nbody"))
        assert "has not passed check_references" in result

    async def test_an_approved_body_goes_through(self, wiki):
        ctx, by_name = wiki
        ctx.approve("# Draft\nbody")
        result = await by_name["wiki_write_page"].ainvoke(self.write_args("# Draft\nbody"))
        assert result.startswith("Wrote supply/r1")

    async def test_editing_after_approval_invalidates_it(self, wiki):
        """Otherwise the check covers a draft that was never published."""
        ctx, by_name = wiki
        ctx.approve("# Draft\nbody")
        result = await by_name["wiki_write_page"].ainvoke(
            self.write_args("# Draft\nbody, plus an unchecked claim")
        )
        assert "has not passed check_references" in result

    async def test_the_gate_is_off_when_no_checker_is_mounted(self):
        """A caller mounting the wiki without the checker must not get an
        unpassable gate."""
        from deep_research_agent.wiki import InMemoryWikiBackend, WikiAuthorizer
        from deep_research_agent.wiki.tools import build_wiki_tools

        principal = user_principal("a", "supply", roles={"wiki.reader", "wiki.writer"})
        ctx = ToolContext(
            principal=principal, request=AgentRequest(principal=principal, task="t")
        )
        by_name = {
            t.name: t for t in build_wiki_tools(
                InMemoryWikiBackend.from_fixtures(ROOT / "fixtures"), WikiAuthorizer(), ctx
            )
        }
        result = await by_name["wiki_write_page"].ainvoke(self.write_args("# Draft\nbody"))
        assert result.startswith("Wrote supply/r1")

    async def test_passing_the_check_is_what_grants_the_approval(self):
        """End to end: the only way to obtain an approval is for that exact
        text to have passed."""
        principal = user_principal("a", "supply", roles={"wiki.reader", "wiki.writer"})
        ctx = ToolContext(
            principal=principal, request=AgentRequest(principal=principal, task="t")
        )
        mesh = build_mesh(fixtures=ROOT / "fixtures", project_root=ROOT)
        by_name = {t.name: t for t in mesh.agent.build_tools(ctx)[0]}

        # The supply-chain domain is loaded here, so the draft has to satisfy
        # its markdown-link rule as well as the structural check.
        await by_name["wiki_read_page"].ainvoke({"ref": "supply/acme-semiconductor"})
        links = await by_name["format_reference"].ainvoke(
            {"refs": ["supply/acme-semiconductor"]}
        )
        link = links.split(" -> ", 1)[1]

        draft = (
            f"### Acme — critical\n- **What happened:** a fire.\n"
            f"- **Sources:** {link}\n"
        )
        assert not ctx.is_approved(draft)
        await by_name["check_references"].ainvoke({"content": draft})
        assert ctx.is_approved(draft), "a draft meeting the domain format must pass"


class TestDeploymentSuppliedReferenceTools:
    """A deployment may have its own reference authority. Discovery happens in
    the wiring, not by asking the model to look around -- a model told to
    check whether a better tool exists sometimes concludes it does not."""

    def test_a_marked_tool_is_discovered(self):
        from deep_research_agent.core.reference_tools import (
            authority_names,
            is_reference_authority,
            reference_authority,
        )

        house = reference_authority(a_tool("house_style_lint"))
        assert is_reference_authority(house)
        assert authority_names([house, a_tool("wiki_read_page")]) == ["house_style_lint"]

    def test_the_general_checker_is_not_reported_as_a_deployment_tool(self):
        """It is the fallback, so naming it as the deployment's own authority
        would make the prompt claim a custom format that does not exist."""
        from deep_research_agent.core.reference_tools import (
            REFERENCE_TOOL_NAME,
            authority_names,
            reference_authority,
        )

        assert authority_names([reference_authority(a_tool(REFERENCE_TOOL_NAME))]) == []

    def test_the_subagent_prompt_names_the_discovered_authority(self):
        from deep_research_agent.core.subagents import reference_checker_prompt

        assert "house_style_lint" in reference_checker_prompt(["house_style_lint"])
        assert "no reference tool of its own" in reference_checker_prompt([])


def a_tool(name: str):
    from langchain_core.tools import StructuredTool

    return StructuredTool.from_function(
        func=lambda **_: "", name=name, description=name,
        args_schema={"type": "object", "properties": {}},
    )
