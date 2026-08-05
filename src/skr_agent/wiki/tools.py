"""The wiki as a set of authorized tools.

The wiki is *one* of the agent's data sources — the internal-knowledge one,
alongside the BOM and external news in ``report/``. It gets its own module
because it is the only source with an authorization model worth enforcing, not
because it is the centre of the system.

That authorization is enforced here, in the tool layer, against a principal
the caller cannot forge. Putting an LLM in front of these tools would add a
model hop, a summarization step that loses citations, and latency, while
enforcing nothing the tool layer does not already enforce — see
``docs/design/00-architecture.md`` §2.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from ..protocol import Citation, Denied
from ..runtime import ToolBundle, ToolContext
from .authz import WikiAuthorizer
from .backend import WikiBackend, WikiPage

log = logging.getLogger(__name__)

__all__ = ["make_wiki_toolset", "build_wiki_tools", "WIKI_TOOL_NAMES"]

WIKI_TOOL_NAMES = ("wiki_search", "wiki_read_page", "wiki_write_page")


def build_wiki_tools(
    backend: WikiBackend, authz: WikiAuthorizer, ctx: ToolContext, *, writable: bool = True
) -> list[BaseTool]:
    """Build the tools for one request's principal.

    Split out from ``make_wiki_toolset`` so tests can invoke a tool directly
    (``await tool.ainvoke({...})``) without standing up an agent.

    ``writable=False`` drops the write tool entirely — used for subagents and
    read-only callers, so the capability is absent rather than merely refused.
    """
    principal = ctx.principal

    async def wiki_search(
        query: str, namespace: str | None = None, limit: int = 5, **_ignored: object
    ) -> str:
        try:
            if namespace:
                authz.check_read(principal, namespace)
                namespaces = [namespace]
            else:
                allowed = authz.readable_namespaces(principal)
                namespaces = ["*"] if "*" in allowed else sorted(allowed)
        except Denied as exc:
            return _denied(exc)

        pages = backend.search(query, namespaces, limit=int(limit))
        if not pages:
            return (
                f"No pages matched within the namespaces you can read "
                f"({', '.join(namespaces)}). This means 'no internal record', "
                f"not 'the thing did not happen'."
            )
        lines = [
            f"- {p.ref} — {p.title}\n  {p.body.strip()[:180]}…\n  sources: "
            f"{', '.join(p.source_refs) or 'none'}"
            for p in pages
        ]
        return f"{len(pages)} page(s):\n" + "\n".join(lines)

    async def wiki_read_page(ref: str, **_ignored: object) -> str:
        if "/" not in ref:
            return f"Error: ref must look like 'namespace/slug', got {ref!r}"
        namespace, slug = ref.split("/", 1)
        try:
            authz.check_read(principal, namespace)
        except Denied as exc:
            return _denied(exc)

        page = backend.get_page(namespace, slug)
        if page is None:
            return f"Error: no page at {ref!r}."

        ctx.cite(Citation(kind="wiki_page", ref=page.ref, title=page.title))
        body = [f"# {page.title}\n({page.ref}, updated {page.updated})\n\n{page.body}"]
        for report_id in page.source_refs:
            report = backend.get_raw_report(report_id)
            ctx.cite(
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
            if report:
                body.append(
                    f"\n## Source {report_id} ({report.author}, week of "
                    f"{report.week_of})\n{report.body}"
                )
        return "\n".join(body)

    tools: list[BaseTool] = [
        StructuredTool.from_function(
            coroutine=wiki_search,
            name="wiki_search",
            description=(
                "Search the internal wiki. Use this whenever you need internal context — "
                "what a team shipped, a supplier's history, known issues, ownership — "
                "instead of answering from memory. Results are automatically limited to "
                "namespaces the current user may read; you cannot widen that."
            ),
            args_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "namespace": {
                        "type": "string",
                        "description": "Optional: narrow to one division namespace.",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": ["query"],
            },
        ),
        StructuredTool.from_function(
            coroutine=wiki_read_page,
            name="wiki_read_page",
            description=(
                "Read one wiki page in full, by its `namespace/slug` reference. Do this "
                "before relying on a page found via search — the search preview is "
                "truncated. The returned source references point at the raw weekly "
                "reports the page was built from; cite those, not just the page."
            ),
            args_schema={
                "type": "object",
                "properties": {"ref": {"type": "string"}},
                "required": ["ref"],
            },
        ),
    ]

    if writable:

        async def wiki_write_page(
            namespace: str,
            slug: str,
            title: str,
            body: str,
            source_refs: list[str],
            **_ignored: object,
        ) -> str:
            try:
                authz.check_write(principal, namespace)
            except Denied as exc:
                return _denied(exc)

            source_refs = list(source_refs or [])
            if not source_refs:
                return (
                    "Error: source_refs is required and must name at least one raw "
                    "report id or external URL supporting this page."
                )

            # The leak guard. A report distilled from several namespaces must
            # not land somewhere with a wider readership than its sources.
            source_namespaces = _namespaces_of(backend, source_refs)
            try:
                authz.check_aggregation(namespace, source_namespaces)
            except Denied as exc:
                return _denied(exc)

            page = backend.upsert_page(
                WikiPage(
                    namespace=namespace,
                    slug=slug,
                    title=title,
                    body=body,
                    source_refs=source_refs,
                )
            )
            log.info(
                "wiki.write page=%s subject=%s trace=%s",
                page.ref, principal.subject, ctx.request.trace_id,
            )
            ctx.cite(Citation(kind="wiki_page", ref=page.ref, title=page.title))
            return f"Wrote {page.ref} with {len(page.source_refs)} source reference(s)."

        tools.append(
            StructuredTool.from_function(
                coroutine=wiki_write_page,
                name="wiki_write_page",
                description=(
                    "Create or update a wiki page. Call this only once the content is "
                    "finished. `source_refs` must list every raw report id and external "
                    "URL the content rests on — a page without provenance is rejected, "
                    "and that rejection is correct. Writes are restricted to namespaces "
                    "the current user may write."
                ),
                args_schema={
                    "type": "object",
                    "properties": {
                        "namespace": {"type": "string"},
                        "slug": {"type": "string"},
                        "title": {"type": "string"},
                        "body": {"type": "string", "description": "Markdown."},
                        "source_refs": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Raw report ids and external URLs. Required.",
                        },
                    },
                    "required": ["namespace", "slug", "title", "body", "source_refs"],
                },
            )
        )

    return tools


def make_wiki_toolset(backend: WikiBackend, authz: WikiAuthorizer, *, writable: bool = True):
    """The ``ToolsetFactory`` a ``DeepAgent`` takes."""

    def factory(ctx: ToolContext) -> ToolBundle:
        return build_wiki_tools(backend, authz, ctx, writable=writable)

    return factory


def _namespaces_of(backend: WikiBackend, source_refs: list[str]) -> set[str]:
    """Which wiki namespaces the cited raw reports belong to."""
    namespaces: set[str] = set()
    for ref in source_refs:
        report = backend.get_raw_report(ref)
        if report is not None:
            namespaces.add(report.namespace)
    return namespaces


def _denied(exc: Denied) -> str:
    # Returned as an ordinary tool result so the model sees the reason and
    # adapts, rather than as an exception it cannot reason about.
    return f"Permission denied: {exc.reason}"
