"""The authorization rules the whole design rests on.

These call the wiki tool functions directly and run without API credentials —
they test the seam, not the model. Tools are built through
``make_wiki_toolset``, the same factory every agent (copilot, the report
agent, the optional wiki_ask) goes through, so a rule verified here holds
everywhere.
"""

from __future__ import annotations

import pytest

from skr_agent.principals import service_principal, user_principal
from skr_agent.protocol import AgentRequest, Denied
from skr_agent.runtime import ToolContext
from skr_agent.wiki.authz import EXEC_NAMESPACE, WikiAuthorizer
from skr_agent.wiki.backend import InMemoryWikiBackend, RawReport, WikiPage
from skr_agent.wiki.tools import build_wiki_tools


def reader(division="supply") -> "Principal":
    return user_principal(f"u@{division}", division)


def writer(division="supply") -> "Principal":
    return user_principal(f"w@{division}", division, roles={"wiki.reader", "wiki.writer"})


@pytest.fixture
def backend() -> InMemoryWikiBackend:
    return InMemoryWikiBackend(
        pages=[
            WikiPage(
                namespace="supply",
                slug="acme",
                title="Acme supplier profile",
                body="Sole source for the ASC-4400 controller.",
                source_refs=["rpt-supply-1"],
            ),
            WikiPage(
                namespace="finance",
                slug="acme-terms",
                title="Acme contract terms",
                body="Payment terms for the ASC-4400 controller programme.",
                source_refs=["rpt-finance-1"],
            ),
            WikiPage(
                namespace="shared",
                slug="severity",
                title="Severity definitions",
                body="Shared severity vocabulary for controller incidents.",
                source_refs=["rpt-supply-1"],
            ),
        ],
        reports=[
            RawReport("rpt-supply-1", "supply", "2026-07-06", "mei", "sole source review"),
            RawReport("rpt-finance-1", "finance", "2026-07-06", "sam", "contract terms review"),
        ],
    )


def toolset(backend, authz=None, *, principal, writable=True):
    """Build the tool dict for one principal, keyed by name for easy lookup.

    Each value invokes the LangChain tool and returns its text result, so the
    tests read as "call the tool, inspect what the model would see"."""
    request = AgentRequest(principal=principal, task="t")
    ctx = ToolContext(principal=principal, request=request)
    tools_list = build_wiki_tools(backend, authz or WikiAuthorizer(), ctx, writable=writable)
    return {tool.name: tool.ainvoke for tool in tools_list}, ctx


def is_error(text: str) -> bool:
    """A tool reports failure in-band, as text the model can reason about,
    rather than by raising — see ``wiki/tools.py``."""
    return text.startswith("Error:") or text.startswith("Permission denied:")


class TestNamespaceReads:
    def test_own_division_and_shared_are_readable(self):
        authz = WikiAuthorizer()
        assert authz.readable_namespaces(reader("supply")) == frozenset({"supply", "shared"})

    def test_other_division_is_not(self):
        authz = WikiAuthorizer()
        with pytest.raises(Denied):
            authz.check_read(reader("supply"), "finance")

    def test_admin_reads_everything(self):
        authz = WikiAuthorizer()
        admin = user_principal("a", "supply", roles={"wiki.reader", "wiki.admin"})
        assert "*" in authz.readable_namespaces(admin)

    def test_exec_namespace_needs_clearance_even_for_admins_own_division(self):
        """A gated namespace is not granted just by being in the right division."""
        authz = WikiAuthorizer()
        exec_division_reader = user_principal("e", EXEC_NAMESPACE)
        assert EXEC_NAMESPACE not in authz.readable_namespaces(exec_division_reader)

    def test_wiki_exec_role_grants_exec_namespace(self):
        authz = WikiAuthorizer()
        p = user_principal("e", "supply", roles={"wiki.reader", "wiki.exec"})
        assert EXEC_NAMESPACE in authz.readable_namespaces(p)

    def test_service_principal_reads_across_divisions(self):
        authz = WikiAuthorizer()
        svc = service_principal()
        assert "*" in authz.readable_namespaces(svc)
        authz.check_read(svc, "supply")
        authz.check_read(svc, "finance")
        authz.check_read(svc, EXEC_NAMESPACE)


class TestNamespaceWrites:
    def test_writer_writes_own_namespace(self):
        WikiAuthorizer().check_write(writer("supply"), "supply")  # no raise

    def test_writer_cannot_write_shared(self):
        """Readable is not writable — the asymmetry is deliberate."""
        with pytest.raises(Denied):
            WikiAuthorizer().check_write(writer("supply"), "shared")

    def test_reader_cannot_write_at_all(self):
        with pytest.raises(Denied, match="no wiki write role"):
            WikiAuthorizer().check_write(reader("supply"), "supply")

    def test_ordinary_writer_cannot_reach_exec_namespace(self):
        with pytest.raises(Denied):
            WikiAuthorizer().check_write(writer("supply"), EXEC_NAMESPACE)

    def test_service_principal_can_write_exec_but_not_other_divisions(self):
        authz = WikiAuthorizer()
        svc = service_principal()
        authz.check_write(svc, EXEC_NAMESPACE)  # no raise
        with pytest.raises(Denied):
            authz.check_write(svc, "supply")


class TestAggregation:
    """The rule that keeps a scheduled cross-division sweep from leaking.

    Every individual read and the final write can each be authorized on their
    own; the aggregation check is what catches the case where the combination
    is not.
    """

    def test_single_source_within_target_clearance_is_fine(self):
        authz = WikiAuthorizer()
        authz.check_aggregation("supply", {"supply"})  # no raise

    def test_gated_source_into_ungated_target_is_refused(self):
        authz = WikiAuthorizer()
        with pytest.raises(Denied, match="requires"):
            authz.check_aggregation("shared", {EXEC_NAMESPACE})

    def test_gated_source_into_equally_gated_target_is_fine(self):
        authz = WikiAuthorizer()
        authz.check_aggregation(EXEC_NAMESPACE, {EXEC_NAMESPACE})  # no raise

    def test_multi_division_aggregate_into_ungated_target_is_refused(self):
        """No single source is gated, but combining divisions still needs clearance."""
        authz = WikiAuthorizer()
        with pytest.raises(Denied, match="aggregates"):
            authz.check_aggregation("shared", {"supply", "finance"})

    def test_multi_division_aggregate_into_exec_is_fine(self):
        authz = WikiAuthorizer()
        authz.check_aggregation(EXEC_NAMESPACE, {"supply", "finance", "platform"})  # no raise

    def test_single_division_source_into_shared_is_fine(self):
        """Not every write into shared is an aggregation leak — one source is fine."""
        authz = WikiAuthorizer()
        authz.check_aggregation("shared", {"supply"})  # no raise


class TestSearchTool:
    async def test_search_is_scoped_to_readable_namespaces(self, backend):
        tools, _ = toolset(backend, principal=reader("supply"))
        result = await tools["wiki_search"]({"query": "ASC-4400 controller"})
        text = result
        assert "supply/acme" in text
        assert "finance/acme-terms" not in text

    async def test_explicit_cross_namespace_request_is_refused(self, backend):
        tools, _ = toolset(backend, principal=reader("supply"))
        result = await tools["wiki_search"]({"query": "contract terms", "namespace": "finance"})
        assert is_error(result)
        assert "permission denied" in result.lower()

    async def test_caller_cannot_widen_scope_via_extra_args(self, backend):
        """A confused or adversarial caller passing extra fields gains nothing —
        they are not in the tool's schema and are silently ignored."""
        tools, _ = toolset(backend, principal=reader("supply"))
        result = await tools["wiki_search"](
            {"query": "ASC-4400", "namespaces": ["finance"], "division": "finance"}
        )
        assert "finance/acme-terms" not in result

    async def test_empty_result_is_not_a_negative_finding(self, backend):
        tools, _ = toolset(backend, principal=reader("supply"))
        result = await tools["wiki_search"]({"query": "zzzqqq nonexistent topic"})
        assert "no internal record" in result.lower()

    async def test_service_principal_searches_across_divisions(self, backend):
        tools, _ = toolset(backend, principal=service_principal())
        result = await tools["wiki_search"]({"query": "ASC-4400 controller contract terms"})
        text = result
        assert "supply/acme" in text
        assert "finance/acme-terms" in text


class TestReadPageTool:
    async def test_read_carries_raw_report_citations(self, backend):
        tools, ctx = toolset(backend, principal=reader("supply"))
        await tools["wiki_read_page"]({"ref": "supply/acme"})
        kinds = {c.kind for c in ctx.citations}
        assert "wiki_page" in kinds
        assert "raw_report" in kinds

    async def test_read_across_namespace_is_refused(self, backend):
        tools, _ = toolset(backend, principal=reader("supply"))
        result = await tools["wiki_read_page"]({"ref": "finance/acme-terms"})
        assert is_error(result)

    async def test_malformed_ref_is_a_clean_error(self, backend):
        tools, _ = toolset(backend, principal=reader("supply"))
        result = await tools["wiki_read_page"]({"ref": "no-slash-here"})
        assert is_error(result)


class TestWriteTool:
    async def test_write_requires_provenance(self, backend):
        tools, _ = toolset(backend, principal=writer("supply"))
        result = await tools["wiki_write_page"](
            {
                "namespace": "supply",
                "slug": "incident-report-2026-30",
                "title": "Week 30",
                "body": "Something happened.",
                "source_refs": [],
            }
        )
        assert is_error(result)
        assert "source_refs" in result

    async def test_write_into_another_namespace_is_refused(self, backend):
        tools, _ = toolset(backend, principal=writer("supply"))
        result = await tools["wiki_write_page"](
            {
                "namespace": "finance",
                "slug": "x",
                "title": "T",
                "body": "B",
                "source_refs": ["rpt-supply-1"],
            }
        )
        assert is_error(result)
        assert "permission denied" in result.lower()

    async def test_successful_write_merges_source_refs(self, backend):
        tools, _ = toolset(backend, principal=writer("supply"))
        result = await tools["wiki_write_page"](
            {
                "namespace": "supply",
                "slug": "acme",
                "title": "Acme supplier profile",
                "body": "Updated.",
                "source_refs": ["rpt-supply-2"],
            }
        )
        assert not is_error(result)
        page = backend.get_page("supply", "acme")
        assert page is not None
        assert set(page.source_refs) == {"rpt-supply-1", "rpt-supply-2"}

    async def test_write_tool_is_absent_when_writable_is_false(self, backend):
        tools, _ = toolset(backend, principal=writer("supply"), writable=False)
        assert "wiki_write_page" not in tools

    async def test_service_principal_can_publish_exec_rollup(self, backend):
        tools, _ = toolset(backend, principal=service_principal())
        result = await tools["wiki_write_page"](
            {
                "namespace": EXEC_NAMESPACE,
                "slug": "weekly-rollup-2026-30",
                "title": "Weekly roll-up — week 30",
                "body": "Cross-division summary.",
                "source_refs": ["rpt-supply-1", "rpt-finance-1"],
            }
        )
        assert not is_error(result), result

    async def test_aggregation_leak_is_caught_even_when_write_access_is_broad(self, backend):
        """The scenario the design doc calls out: a caller who *is* allowed to
        write into an ungated namespace (here, an admin) tries to publish a
        page distilled from more than one division there. Namespace write
        permission alone is not enough — the aggregation check is a second,
        independent gate on the derived document."""
        admin = user_principal("root", "supply", roles={"wiki.reader", "wiki.admin"})
        tools, _ = toolset(backend, principal=admin)
        result = await tools["wiki_write_page"](
            {
                "namespace": "shared",  # admin can write here; check_write alone would pass
                "slug": "leak",
                "title": "Cross-division roll-up",
                "body": "Should not land here.",
                "source_refs": ["rpt-supply-1", "rpt-finance-1"],  # two divisions
            }
        )
        assert is_error(result)
        assert "aggregates" in result

    async def test_service_principal_cannot_route_around_write_scope_via_aggregation(
        self, backend
    ):
        """Belt and suspenders: even before aggregation is considered, the
        service account's own write scope (exec only) blocks this — the two
        checks are independent layers, and either one alone stops the leak."""
        tools, _ = toolset(backend, principal=service_principal())
        result = await tools["wiki_write_page"](
            {
                "namespace": "shared",
                "slug": "leak",
                "title": "Cross-division roll-up",
                "body": "Should not land here.",
                "source_refs": ["rpt-supply-1", "rpt-finance-1"],
            }
        )
        assert is_error(result)
