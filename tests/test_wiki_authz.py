"""The authorization rules the whole design rests on.

These run without API credentials — they test the seam, not the model.
"""

from __future__ import annotations

import pytest

from skr_agent.protocol import AgentRequest, Denied, Principal
from skr_agent.wiki import InMemoryWikiBackend, WikiAuthorizer, WikiCoordinator
from skr_agent.wiki.backend import RawReport, WikiPage


def principal(division="supply", roles=("wiki.reader",)) -> Principal:
    return Principal(subject=f"u@{division}", division=division, roles=frozenset(roles))


@pytest.fixture
def coordinator() -> WikiCoordinator:
    backend = InMemoryWikiBackend(
        pages=[
            WikiPage(
                namespace="supply",
                slug="acme",
                title="Acme supplier profile",
                body="Sole source for the ASC-4400 controller.",
                source_refs=["rpt-1"],
            ),
            WikiPage(
                namespace="finance",
                slug="acme-terms",
                title="Acme contract terms",
                body="Payment terms for the ASC-4400 controller programme.",
                source_refs=["rpt-2"],
            ),
            WikiPage(
                namespace="shared",
                slug="severity",
                title="Severity definitions",
                body="Shared severity vocabulary for controller incidents.",
                source_refs=["rpt-3"],
            ),
        ],
        reports=[
            RawReport("rpt-1", "supply", "2026-07-06", "mei", "sole source review"),
        ],
    )
    return WikiCoordinator(backend=backend, authz=WikiAuthorizer())


class TestNamespaceReads:
    def test_own_division_and_shared_are_readable(self):
        authz = WikiAuthorizer()
        assert authz.readable_namespaces(principal("supply")) == frozenset(
            {"supply", "shared"}
        )

    def test_other_division_is_not(self):
        authz = WikiAuthorizer()
        with pytest.raises(Denied):
            authz.check_read(principal("supply"), "finance")

    def test_admin_reads_everything(self):
        authz = WikiAuthorizer()
        admin = principal("supply", roles=("wiki.admin",))
        assert "*" in authz.readable_namespaces(admin)


class TestNamespaceWrites:
    def test_writer_writes_own_namespace(self):
        authz = WikiAuthorizer()
        p = principal("supply", roles=("wiki.reader", "wiki.writer"))
        authz.check_write(p, "supply")  # no raise

    def test_writer_cannot_write_shared(self):
        """Readable is not writable — the asymmetry is deliberate."""
        authz = WikiAuthorizer()
        p = principal("supply", roles=("wiki.reader", "wiki.writer"))
        with pytest.raises(Denied):
            authz.check_write(p, "shared")

    def test_reader_cannot_write_at_all(self):
        authz = WikiAuthorizer()
        with pytest.raises(Denied, match="no wiki write role"):
            authz.check_write(principal("supply"), "supply")


class TestCoordinatorEnforcement:
    async def test_search_is_scoped_to_readable_namespaces(self, coordinator):
        """The finance page mentions ASC-4400 too, and must not come back."""
        response = await coordinator.ask(
            AgentRequest(principal=principal("supply"), task="What about the ASC-4400 controller?")
        )
        assert response.status == "ok"
        refs = response.data["pages"]
        assert "supply/acme" in refs
        assert "finance/acme-terms" not in refs

    async def test_explicit_cross_namespace_request_is_refused(self, coordinator):
        response = await coordinator.ask(
            AgentRequest(
                principal=principal("supply"),
                task="contract terms",
                inputs={"namespace": "finance"},
            )
        )
        assert response.status == "refused"
        assert not response.ok

    async def test_caller_cannot_widen_scope_via_inputs(self, coordinator):
        """A compromised or confused caller passing extra fields gains nothing."""
        response = await coordinator.ask(
            AgentRequest(
                principal=principal("supply"),
                task="ASC-4400 controller",
                inputs={"namespaces": ["finance"], "division": "finance"},
            )
        )
        assert "finance/acme-terms" not in response.data["pages"]

    async def test_answers_carry_raw_report_citations(self, coordinator):
        response = await coordinator.ask(
            AgentRequest(principal=principal("supply"), task="ASC-4400 controller sole source")
        )
        kinds = {c.kind for c in response.citations}
        assert "wiki_page" in kinds
        assert "raw_report" in kinds

    async def test_empty_result_is_not_a_negative_finding(self, coordinator):
        response = await coordinator.ask(
            AgentRequest(principal=principal("supply"), task="zzzqqq nonexistent topic")
        )
        assert response.status == "ok"
        assert "no internal record" in response.output.lower()


class TestPublish:
    async def test_publish_requires_provenance(self, coordinator):
        p = principal("supply", roles=("wiki.reader", "wiki.writer"))
        response = await coordinator.publish(
            AgentRequest(
                principal=p,
                task="weekly report",
                inputs={
                    "namespace": "supply",
                    "slug": "incident-report-2026-30",
                    "title": "Week 30",
                    "body": "Something happened.",
                    "source_refs": [],
                },
            )
        )
        assert response.status == "failed"
        assert response.error is not None
        assert response.error.code == "missing_provenance"

    async def test_publish_into_another_namespace_is_refused(self, coordinator):
        p = principal("supply", roles=("wiki.reader", "wiki.writer"))
        response = await coordinator.publish(
            AgentRequest(
                principal=p,
                task="weekly report",
                inputs={
                    "namespace": "finance",
                    "slug": "x",
                    "title": "T",
                    "body": "B",
                    "source_refs": ["rpt-1"],
                },
            )
        )
        assert response.status == "refused"

    async def test_successful_publish_merges_source_refs(self, coordinator):
        p = principal("supply", roles=("wiki.reader", "wiki.writer"))
        request = AgentRequest(
            principal=p,
            task="update acme",
            inputs={
                "namespace": "supply",
                "slug": "acme",
                "title": "Acme supplier profile",
                "body": "Updated.",
                "source_refs": ["rpt-9"],
            },
        )
        response = await coordinator.publish(request)
        assert response.status == "ok"
        page = coordinator.backend.get_page("supply", "acme")
        assert page is not None
        # The pre-existing ref survives the update rather than being replaced.
        assert set(page.source_refs) == {"rpt-1", "rpt-9"}
