"""The supply-chain citation format: markdown links, built not written.

The house rule is that every entry on a Sources line is `[name](target)` —
news to the article URL, a wiki page to its route — and that raw report ids
stay out of the page entirely.

Two properties carry the design and both are covered here: the generator
produces something the checker accepts (otherwise the agent is stuck in a
loop between two tools that disagree), and a correctly written draft produces
silence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deep_research_agent import build_mesh
from deep_research_agent.core.references import check_references
from deep_research_agent.domains.supply_chain.references import (
    SUPPLY_CHAIN_FORMAT,
    SUPPLY_CHAIN_RULES,
    canonical_ref,
    format_reference,
)
from deep_research_agent.principals import user_principal
from deep_research_agent.protocol import AgentRequest
from deep_research_agent.runtime import ToolContext
from deep_research_agent.wiki.routes import page_url, ref_from_url

ROOT = Path(__file__).resolve().parent.parent

PAGE = "supply/acme-semiconductor"
ARTICLE = "https://news.test/acme-fire"
RAW = "rpt-supply-2026-W28"

CORPUS = {
    PAGE: {"title": "Acme Semiconductor — supplier profile", "kind": "wiki_page"},
    ARTICLE: {"title": "Fire halts production at Acme", "kind": "external_url"},
    RAW: {"title": "supply weekly, week of 2026-W28", "kind": "raw_report"},
}


def check(content, **kwargs):
    return check_references(
        content, corpus=CORPUS, fmt=SUPPLY_CHAIN_FORMAT, rules=SUPPLY_CHAIN_RULES, **kwargs
    )


def kinds(report):
    return [v.kind for v in report.violations]


class TestTheGenerator:
    def test_a_wiki_page_links_to_its_route(self):
        markdown = format_reference(PAGE, CORPUS)
        assert markdown == f"[Acme Semiconductor — supplier profile]({page_url(PAGE)})"

    def test_news_links_to_the_article_url(self):
        assert format_reference(ARTICLE, CORPUS) == f"[Fire halts production at Acme]({ARTICLE})"

    def test_the_link_text_is_the_name_the_source_gave(self):
        """Not the model's paraphrase of it. That is the whole reason this is
        a tool and not a prompt instruction."""
        assert "Acme Semiconductor — supplier profile" in format_reference(PAGE, CORPUS)

    def test_a_raw_report_gets_no_link(self):
        """It belongs in source_refs, not in the page."""
        assert format_reference(RAW, CORPUS) is None

    def test_a_reference_nothing_loaded_gets_no_link(self):
        """The link text would have to be invented, and a link with a made-up
        name is worse than no link."""
        assert format_reference("supply/never-read", CORPUS) is None

    def test_a_source_with_no_title_falls_back_to_its_ref(self):
        """An empty `[]()` renders as nothing at all -- worse than a plain
        ref, which at least identifies the source."""
        assert format_reference(PAGE, {PAGE: {"title": ""}}) == f"[{PAGE}]({page_url(PAGE)})"


class TestTheGeneratorSatisfiesTheChecker:
    """The property that keeps the agent out of a loop: what the generator
    produces is what the checker accepts. If these ever disagree, the agent
    alternates between two tools and never converges."""

    def test_a_draft_built_from_the_generator_passes(self):
        links = ", ".join(format_reference(r, CORPUS) for r in (PAGE, ARTICLE))
        draft = f"### Acme — critical\n- **What:** a fire.\n- **Sources:** {links}\n"
        report = check(draft, declared=[PAGE, ARTICLE, RAW])
        assert report.violations == (), report.render()

    def test_it_holds_through_the_wired_tools(self):
        """Same property, but through the real tool objects and the real
        retrieval store rather than a fixture corpus."""
        import asyncio

        async def run():
            principal = user_principal("a", "supply", roles={"wiki.reader", "wiki.writer"})
            ctx = ToolContext(
                principal=principal, request=AgentRequest(principal=principal, task="t")
            )
            mesh = build_mesh(fixtures=ROOT / "fixtures", project_root=ROOT)
            by_name = {t.name: t for t in mesh.agent.build_tools(ctx)[0]}

            await by_name["wiki_read_page"].ainvoke({"ref": PAGE})
            rendered = await by_name["format_reference"].ainvoke({"refs": [PAGE]})
            link = rendered.split(" -> ", 1)[1]

            draft = f"### Acme — critical\n- **What:** a fire.\n- **Sources:** {link}\n"
            return await by_name["check_references"].ainvoke({"content": draft})

        assert asyncio.run(run()).startswith("PASS")


class TestTheRules:
    def test_a_bare_reference_on_a_sources_line_is_an_error(self):
        draft = f"### Acme — critical\n- **What:** x.\n- **Sources:** {PAGE}\n"
        assert "unlinked_source" in kinds(check(draft))

    def test_a_bare_url_is_caught_too(self):
        """The rule is checked by what survives removing the links, so a shape
        nobody enumerated is still caught."""
        draft = f"### Acme — critical\n- **What:** x.\n- **Sources:** {ARTICLE}\n"
        assert "unlinked_source" in kinds(check(draft))

    def test_a_mix_of_linked_and_bare_reports_only_the_bare_one(self):
        draft = (
            f"### Acme — critical\n- **What:** x.\n"
            f"- **Sources:** {format_reference(ARTICLE, CORPUS)}, {PAGE}\n"
        )
        violation = next(v for v in check(draft).violations if v.kind == "unlinked_source")
        assert PAGE in violation.message
        assert "news.test" not in violation.message

    def test_a_raw_report_in_the_body_is_an_error(self):
        draft = (
            f"### Acme — critical\n- **What:** x, per {RAW}.\n"
            f"- **Sources:** {format_reference(PAGE, CORPUS)}\n"
        )
        report = check(draft)
        assert "raw_report_in_body" in kinds(report)
        assert not report.passed

    def test_a_raw_report_in_source_refs_is_fine(self):
        """That is exactly where it belongs -- the aggregation check reads it
        from there."""
        draft = f"### Acme — critical\n- **What:** x.\n- **Sources:** {format_reference(PAGE, CORPUS)}\n"
        assert check(draft, declared=[PAGE, RAW]).violations == ()

    def test_the_defect_message_says_what_to_do_instead(self):
        """A defect report that does not say the fix costs a turn of guessing."""
        draft = f"### Acme — critical\n- **What:** x.\n- **Sources:** {PAGE}\n"
        message = next(v for v in check(draft).violations if v.kind == "unlinked_source").message
        assert "format_reference" in message


class TestLinkTargetsMapBackToReferences:
    """Once sources are links, a parser finds the link target, not the
    reference. Without the mapping, a page cited exactly right reads as a URL
    matching nothing in source_refs -- a defect report about a correct draft."""

    def test_a_wiki_url_maps_back(self):
        assert canonical_ref(page_url(PAGE)) == PAGE

    def test_an_article_url_is_left_alone(self):
        assert canonical_ref(ARTICLE) == ARTICLE

    def test_a_bare_ref_is_left_alone(self):
        assert canonical_ref(PAGE) == PAGE

    def test_a_url_under_a_different_host_is_not_mistaken_for_a_page(self):
        assert ref_from_url("https://elsewhere.test/supply/acme") is None

    def test_the_cross_check_against_source_refs_now_agrees(self):
        """The failure this exists to prevent, stated directly."""
        draft = (
            f"### Acme — critical\n- **What:** x.\n"
            f"- **Sources:** {format_reference(PAGE, CORPUS)}\n"
        )
        assert "undeclared_reference" not in kinds(check(draft, declared=[PAGE]))

    def test_a_page_cited_twice_in_two_shapes_counts_once(self):
        """Normalising before deduplication -- otherwise the same source shows
        up twice and puts a phantom entry in every cross-check."""
        draft = (
            f"### Acme — critical\n- **What:** see {PAGE}.\n"
            f"- **Sources:** {format_reference(PAGE, CORPUS)}\n"
        )
        assert list(check(draft).references).count(PAGE) == 1


class TestItIsWiredIntoTheDomain:
    @pytest.fixture
    def surface(self):
        principal = user_principal("a", "supply", roles={"wiki.reader", "wiki.writer"})
        ctx = ToolContext(
            principal=principal, request=AgentRequest(principal=principal, task="t")
        )
        mesh = build_mesh(fixtures=ROOT / "fixtures", project_root=ROOT)
        tools, subagents = mesh.agent.build_tools(ctx)
        return mesh, {t.name for t in tools}, {s["name"]: s for s in subagents}

    def test_the_generator_is_mounted(self, surface):
        _, names, _ = surface
        assert "format_reference" in names

    def test_the_reference_checker_gets_it(self, surface):
        _, _, subs = surface
        assert "format_reference" in {t.name for t in subs["reference-checker"]["tools"]}

    def test_the_subagent_is_told_the_deployment_has_its_own_tooling(self, surface):
        """Discovery happens in the wiring, so the prompt states which tool
        applies rather than leaving the model to go looking."""
        _, _, subs = surface
        assert "format_reference" in subs["reference-checker"]["system_prompt"]

    def test_the_domain_rules_reach_the_live_checker(self, surface):
        """A bare ref must fail through the mounted tool, not just in a unit
        test of the rule function."""
        import asyncio

        mesh, _, _ = surface
        principal = user_principal("a", "supply", roles={"wiki.reader"})
        ctx = ToolContext(
            principal=principal, request=AgentRequest(principal=principal, task="t")
        )
        by_name = {t.name: t for t in mesh.agent.build_tools(ctx)[0]}
        draft = f"### Acme — critical\n- **What:** x.\n- **Sources:** {PAGE}\n"
        result = asyncio.run(by_name["check_references"].ainvoke({"content": draft}))
        assert "unlinked_source" in result

    def test_the_shipped_rubric_teaches_the_same_format(self, surface):
        """The rubric is inlined into the prompt. If it still described bare
        refs, the agent would be told to produce what the checker rejects."""
        mesh, _, _ = surface
        prompt = mesh.agent._full_system_prompt()
        assert "format_reference" in prompt
        assert "[name](target)" in prompt
        assert "never appear in the page" in prompt
