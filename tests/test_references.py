"""The reference check: format, shape, grounding, declaration.

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

RETRIEVED = [
    "supply/acme-semiconductor",
    "rpt-supply-2026-W28",
    "https://news.test/acme-fire",
]

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
        report = check_references(CLEAN, retrieved=RETRIEVED)
        assert report.violations == ()
        assert report.passed

    def test_prose_outside_a_findings_section_needs_no_citation(self):
        """Demanding a source on "two companies scanned" produces noise that
        trains people to ignore the checker."""
        assert "unsourced_section" not in kinds(check_references(CLEAN, retrieved=RETRIEVED))

    def test_a_none_section_with_its_queries_is_accepted(self):
        assert "unexplained_absence" not in kinds(check_references(CLEAN, retrieved=RETRIEVED))

    def test_a_retrieved_but_unquoted_source_is_not_a_leftover(self):
        """Reading a wiki page also returns the raw reports behind it.
        Declaring those is what the provenance rule asks for -- warning about
        it would be exactly the noise this checker must not produce."""
        report = check_references(
            CLEAN, retrieved=RETRIEVED,
            declared=["supply/acme-semiconductor", "https://news.test/acme-fire",
                      "rpt-supply-2026-W28"],
        )
        assert report.violations == ()


class TestGrounding:
    """The check that could not be done anywhere else: the tool layer records
    what was actually read, so a reference written from memory is decidable
    rather than a matter of judgement."""

    def test_a_reference_never_retrieved_is_an_error(self):
        draft = CLEAN.replace("https://news.test/acme-fire", "https://invented.test/story")
        report = check_references(draft, retrieved=RETRIEVED)
        assert "ungrounded_reference" in kinds(report)
        assert not report.passed

    def test_the_message_names_the_reference_and_the_line(self):
        """A defect you have to re-read the draft to locate is half a defect."""
        draft = CLEAN.replace("https://news.test/acme-fire", "https://invented.test/story")
        violation = next(
            v for v in check_references(draft, retrieved=RETRIEVED).violations
            if v.kind == "ungrounded_reference"
        )
        assert "invented.test" in violation.message
        assert violation.line > 0

    def test_a_reference_in_prose_is_grounded_too(self):
        """A URL dropped into a sentence is still a citation to a reader, so
        checking only the Sources line would miss the more deceptive case."""
        draft = CLEAN.replace(
            "fire at the fab.", "fire at the fab, per https://invented.test/x."
        )
        assert "ungrounded_reference" in kinds(check_references(draft, retrieved=RETRIEVED))

    def test_no_retrieval_record_at_all_skips_the_check(self):
        """`None` is "this process did not produce the document, so there is
        nothing to compare against" -- reporting every reference then would be
        worse than silence."""
        assert "ungrounded_reference" not in kinds(check_references(CLEAN, retrieved=None))

    def test_a_run_that_retrieved_nothing_grounds_nothing(self):
        """`[]` is a different claim from `None`: this run read nothing, so
        every reference in the draft came from somewhere other than a source.
        Treating the two the same would make the check silently useless in the
        one case where every citation is fabricated."""
        assert kinds(check_references(CLEAN, retrieved=[])).count("ungrounded_reference") == 2


class TestFormat:
    def test_a_section_with_no_sources_line_is_caught(self):
        draft = CLEAN.replace(
            "- **Sources:** supply/acme-semiconductor, https://news.test/acme-fire", ""
        )
        assert "unsourced_section" in kinds(check_references(draft, retrieved=RETRIEVED))

    def test_a_sources_line_naming_nothing_is_caught(self):
        draft = CLEAN.replace(
            "- **Sources:** supply/acme-semiconductor, https://news.test/acme-fire",
            "- **Sources:** see above",
        )
        assert "empty_sources" in kinds(check_references(draft, retrieved=RETRIEVED))

    def test_a_none_section_without_its_queries_is_caught(self):
        """Absence is a finding. Without the queries a reader cannot tell
        "checked, found nothing" from "not checked"."""
        draft = CLEAN.replace('- **Queries run:** "Gamma Industries", "Gamma Ind."\n', "")
        assert "unexplained_absence" in kinds(check_references(draft, retrieved=RETRIEVED))

    def test_a_none_section_is_not_asked_for_sources(self):
        draft = CLEAN.replace('- **Queries run:** "Gamma Industries", "Gamma Ind."\n', "")
        assert "unsourced_section" not in kinds(check_references(draft, retrieved=RETRIEVED))

    def test_every_defective_section_is_reported_not_just_the_first(self):
        """The reason this is a parser and not a model: exhaustive counting."""
        draft = CLEAN + "\n".join(
            f"### Company {n} — high\n- **What happened:** something.\n" for n in range(8)
        )
        assert kinds(check_references(draft, retrieved=RETRIEVED)).count("unsourced_section") == 8


class TestDeclaration:
    def test_a_body_reference_missing_from_source_refs_is_an_error(self):
        report = check_references(
            CLEAN, retrieved=RETRIEVED, declared=["supply/acme-semiconductor"]
        )
        assert "undeclared_reference" in kinds(report)

    def test_an_empty_declaration_is_an_error(self):
        assert "no_declared_sources" in kinds(
            check_references(CLEAN, retrieved=RETRIEVED, declared=[])
        )

    def test_a_stale_declaration_is_only_a_warning(self):
        """It makes the page untidy, not wrong. Blocking a publish on it would
        make the checker the thing people route around."""
        report = check_references(
            CLEAN, retrieved=RETRIEVED,
            declared=[*RETRIEVED, "rpt-old-2019-W01"],
        )
        assert kinds(report) == ["unused_declaration"]
        assert report.passed

    def test_no_declaration_means_the_cross_check_is_skipped(self):
        """Mid-draft there is no publish call to compare against."""
        assert check_references(CLEAN, retrieved=RETRIEVED, declared=None).violations == ()


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


class TestTheToolSeesWhatTheRunActuallyRetrieved:
    """The grounding check is only as good as its retrieval record, and that
    record comes from the citation sink the tools already write to."""

    async def test_a_reference_becomes_grounded_once_it_is_read(self):
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
        assert "ungrounded_reference" in before

        await by_name["wiki_read_page"].ainvoke({"ref": "supply/acme-semiconductor"})

        after = await by_name["check_references"].ainvoke({"content": draft})
        assert "ungrounded_reference" not in after
        assert after.startswith("PASS")
