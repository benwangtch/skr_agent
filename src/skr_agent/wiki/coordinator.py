"""The wiki coordinator — mocked, but behind the real contract.

Another team owns the production implementation. What this file pins down is
the *seam*: two agent specs, ``wiki_ask`` and ``wiki_publish``, that enforce
namespace authorization against a re-derived principal and return citations
that reach back to raw weekly reports.

The mock answers by stitching together retrieved pages instead of reasoning
over them. That is on purpose — swapping in a real coordinator (a deep agent
with query/search/load/write tools, or anything else) should require changing
only this module, and the report agent and copilot should not notice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..protocol import (
    AgentRequest,
    AgentResponse,
    AgentSpec,
    Citation,
    Denied,
)
from .authz import WikiAuthorizer
from .backend import WikiBackend, WikiPage

log = logging.getLogger(__name__)

__all__ = ["WikiCoordinator"]

ASK_DESCRIPTION = (
    "Ask a question about the internal wiki and get an answer grounded in wiki "
    "pages, with citations back to the raw weekly reports behind them. Call this "
    "whenever you need internal context — what a team shipped, known issues, "
    "prior incidents, ownership — rather than guessing or relying on external "
    "sources. Results are automatically scoped to what the current user may read."
)

PUBLISH_DESCRIPTION = (
    "Create or update a wiki page. Call this once you have a finished, "
    "source-backed document to persist. You must supply the raw report or "
    "external references the content came from; a page without provenance is "
    "rejected. Writes are restricted to the current user's own division "
    "namespace."
)


@dataclass
class WikiCoordinator:
    """Mock implementation of the wiki service's agent-facing surface."""

    backend: WikiBackend
    authz: WikiAuthorizer

    # -- specs --------------------------------------------------------------

    def ask_spec(self) -> AgentSpec:
        return AgentSpec(
            name="wiki_ask",
            description=ASK_DESCRIPTION,
            handler=self.ask,
            tags=frozenset({"wiki", "read"}),
            input_schema={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "The question, in full sentences.",
                    },
                    "namespace": {
                        "type": "string",
                        "description": (
                            "Optional division namespace to restrict the search to. "
                            "Omit to search everything you are allowed to read."
                        ),
                    },
                },
                "required": ["task"],
            },
        )

    def publish_spec(self) -> AgentSpec:
        return AgentSpec(
            name="wiki_publish",
            description=PUBLISH_DESCRIPTION,
            handler=self.publish,
            tags=frozenset({"wiki", "write"}),
            input_schema={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "One line describing the change, for the audit log.",
                    },
                    "namespace": {"type": "string"},
                    "slug": {"type": "string", "description": "URL-safe page identifier."},
                    "title": {"type": "string"},
                    "body": {"type": "string", "description": "Full page content, markdown."},
                    "source_refs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Raw report ids and external URLs this page is built from. "
                            "Required and must be non-empty."
                        ),
                    },
                },
                "required": ["task", "namespace", "slug", "title", "body", "source_refs"],
            },
        )

    def specs(self) -> list[AgentSpec]:
        return [self.ask_spec(), self.publish_spec()]

    # -- handlers -----------------------------------------------------------

    async def ask(self, request: AgentRequest) -> AgentResponse:
        principal = request.principal
        requested = request.inputs.get("namespace")

        try:
            if requested:
                # An explicit namespace is a *narrowing* request, still checked.
                self.authz.check_read(principal, requested)
                namespaces = [requested]
            else:
                allowed = self.authz.readable_namespaces(principal)
                namespaces = (
                    ["*"] if "*" in allowed else sorted(allowed)
                )
        except Denied as exc:
            return AgentResponse.refuse(exc.reason, trace_id=request.trace_id)

        pages = self.backend.search(request.task, namespaces, limit=4)
        if not pages:
            return AgentResponse(
                status="ok",
                output=(
                    "No wiki pages matched that question within the namespaces "
                    f"you can read ({', '.join(namespaces)}). Treat this as "
                    "'no internal record', not as a negative finding."
                ),
                trace_id=request.trace_id,
            )

        citations: list[Citation] = []
        chunks: list[str] = []
        for page in pages:
            chunks.append(f"## {page.title}  ({page.ref})\n{page.body.strip()}")
            citations.append(
                Citation(
                    kind="wiki_page",
                    ref=page.ref,
                    title=page.title,
                    snippet=page.body.strip()[:200],
                )
            )
            for report_id in page.source_refs:
                report = self.backend.get_raw_report(report_id)
                citations.append(
                    Citation(
                        kind="raw_report",
                        ref=report_id,
                        title=(
                            f"{report.namespace} weekly, week of {report.week_of}"
                            if report
                            else report_id
                        ),
                    )
                )

        return AgentResponse(
            status="ok",
            output="\n\n".join(chunks),
            data={"pages": [p.ref for p in pages]},
            citations=tuple(citations),
            trace_id=request.trace_id,
        )

    async def publish(self, request: AgentRequest) -> AgentResponse:
        principal = request.principal
        inputs = request.inputs
        namespace = inputs.get("namespace") or principal.division

        try:
            self.authz.check_write(principal, namespace)
        except Denied as exc:
            return AgentResponse.refuse(exc.reason, trace_id=request.trace_id)

        missing = [k for k in ("slug", "title", "body") if not inputs.get(k)]
        if missing:
            return AgentResponse.fail(
                "invalid_input",
                f"missing required field(s): {', '.join(missing)}",
                trace_id=request.trace_id,
            )

        source_refs = list(inputs.get("source_refs") or [])
        if not source_refs:
            # The rule that makes the wiki trustworthy. Enforced here rather
            # than left to the prompt, because a prompt is a suggestion.
            return AgentResponse.fail(
                "missing_provenance",
                "source_refs is required and must list at least one raw report "
                "id or external URL supporting this page",
                trace_id=request.trace_id,
            )

        page = self.backend.upsert_page(
            WikiPage(
                namespace=namespace,
                slug=inputs["slug"],
                title=inputs["title"],
                body=inputs["body"],
                source_refs=source_refs,
            )
        )
        log.info(
            "wiki.publish page=%s subject=%s trace=%s",
            page.ref, principal.subject, request.trace_id,
        )
        return AgentResponse(
            status="ok",
            output=f"Published {page.ref} with {len(page.source_refs)} source reference(s).",
            data={"ref": page.ref, "namespace": page.namespace, "slug": page.slug},
            citations=(Citation(kind="wiki_page", ref=page.ref, title=page.title),),
            trace_id=request.trace_id,
        )
