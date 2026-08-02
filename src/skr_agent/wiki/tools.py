"""The wiki as a set of authorized tools, mounted directly by calling agents.

This is the primary integration path, and it is deliberately *not* an agent.
The reason the wiki needs a boundary is authorization, and authorization is
enforced here — in the tool layer, against a principal the caller cannot
forge. Putting an LLM in front of these tools would add a model hop, a
summarization step that loses citations, and latency, while enforcing nothing
the tool layer does not already enforce.

``coordinator.py`` still exists for the case where the wiki team wants to own
retrieval *quality* (query rewriting, multi-hop, reranking) behind a single
``wiki_ask`` call. That is a real reason to add an agent. "We need a service
boundary" is not.
"""

from __future__ import annotations

import logging
from typing import Any

from claude_agent_sdk import SdkMcpTool, ToolAnnotations, create_sdk_mcp_server, tool

from ..protocol import Citation, Denied
from ..runtime import ToolBundle, ToolContext
from .authz import WikiAuthorizer
from .backend import WikiBackend, WikiPage

log = logging.getLogger(__name__)

__all__ = ["make_wiki_toolset", "build_wiki_tools", "WIKI_TOOL_NAMES"]

WIKI_TOOL_NAMES = (
    "mcp__wiki__wiki_search",
    "mcp__wiki__wiki_read_page",
    "mcp__wiki__wiki_write_page",
)


def build_wiki_tools(
    backend: WikiBackend, authz: WikiAuthorizer, ctx: ToolContext, *, writable: bool = True
) -> list[SdkMcpTool]:
    """Build the raw tool objects for one request's principal.

    Split out from ``make_wiki_toolset`` so tests can call tool handlers
    directly (``tool.handler(args)``) instead of reaching into the SDK's MCP
    server object, which wraps them in a transport this module doesn't need to
    know about.

    ``writable=False`` drops the write tool entirely — used for subagents and
    for read-only callers, so the capability is absent rather than merely
    refused.
    """
    principal = ctx.principal

    @tool(
        "wiki_search",
        "Search the internal wiki. Use this whenever you need internal context — "
        "what a team shipped, a supplier's history, known issues, ownership — "
        "instead of answering from memory. Results are automatically limited to "
        "namespaces the current user may read; you cannot widen that.",
        {
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
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def wiki_search(args: dict[str, Any]) -> dict[str, Any]:
        requested = args.get("namespace")
        try:
            if requested:
                authz.check_read(principal, requested)
                namespaces = [requested]
            else:
                allowed = authz.readable_namespaces(principal)
                namespaces = ["*"] if "*" in allowed else sorted(allowed)
        except Denied as exc:
            return _denied(exc)

        pages = backend.search(args["query"], namespaces, limit=int(args.get("limit", 5)))
        if not pages:
            return _text(
                f"No pages matched within the namespaces you can read "
                f"({', '.join(namespaces)}). This means 'no internal record', "
                f"not 'the thing did not happen'."
            )
        lines = [
            f"- {p.ref} — {p.title}\n  {p.body.strip()[:180]}…\n  sources: "
            f"{', '.join(p.source_refs) or 'none'}"
            for p in pages
        ]
        return _text(f"{len(pages)} page(s):\n" + "\n".join(lines))

    @tool(
        "wiki_read_page",
        "Read one wiki page in full, by its `namespace/slug` reference. Do this "
        "before relying on a page found via search — the search preview is "
        "truncated. The returned source references point at the raw weekly "
        "reports the page was built from; cite those, not just the page.",
        {"ref": str},
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def wiki_read_page(args: dict[str, Any]) -> dict[str, Any]:
        ref = args["ref"]
        if "/" not in ref:
            return _error(f"ref must look like 'namespace/slug', got {ref!r}")
        namespace, slug = ref.split("/", 1)
        try:
            authz.check_read(principal, namespace)
        except Denied as exc:
            return _denied(exc)

        page = backend.get_page(namespace, slug)
        if page is None:
            return _error(f"No page at {ref!r}.")

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
        return _text("\n".join(body))

    tools = [wiki_search, wiki_read_page]

    if writable:

        @tool(
            "wiki_write_page",
            "Create or update a wiki page. Call this only once the content is "
            "finished. `source_refs` must list every raw report id and external "
            "URL the content rests on — a page without provenance is rejected, "
            "and that rejection is correct. Writes are restricted to namespaces "
            "the current user may write.",
            {
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
            annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
        )
        async def wiki_write_page(args: dict[str, Any]) -> dict[str, Any]:
            namespace = args["namespace"]
            try:
                authz.check_write(principal, namespace)
            except Denied as exc:
                return _denied(exc)

            source_refs = list(args.get("source_refs") or [])
            if not source_refs:
                return _error(
                    "source_refs is required and must name at least one raw "
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
                    slug=args["slug"],
                    title=args["title"],
                    body=args["body"],
                    source_refs=source_refs,
                )
            )
            log.info(
                "wiki.write page=%s subject=%s trace=%s",
                page.ref, principal.subject, ctx.request.trace_id,
            )
            ctx.cite(Citation(kind="wiki_page", ref=page.ref, title=page.title))
            return _text(
                f"Wrote {page.ref} with {len(page.source_refs)} source reference(s)."
            )

        tools.append(wiki_write_page)

    return tools


def make_wiki_toolset(backend: WikiBackend, authz: WikiAuthorizer, *, writable: bool = True):
    """The ``ToolsetFactory`` a ``DeepAgent`` takes: build tools, wrap as an
    in-process MCP server, and return ``(server, allowed_tool_names)``."""

    def factory(ctx: ToolContext) -> ToolBundle:
        tools = build_wiki_tools(backend, authz, ctx, writable=writable)
        names = [f"mcp__wiki__{t.name}" for t in tools]
        server = create_sdk_mcp_server(name="wiki", version="1.0.0", tools=tools)
        return server, names

    return factory


def _namespaces_of(backend: WikiBackend, source_refs: list[str]) -> set[str]:
    """Which wiki namespaces the cited raw reports belong to."""
    namespaces: set[str] = set()
    for ref in source_refs:
        report = backend.get_raw_report(ref)
        if report is not None:
            namespaces.add(report.namespace)
    return namespaces


def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def _denied(exc: Denied) -> dict[str, Any]:
    # Returned as a normal (errored) tool result so the model sees the reason
    # and adapts, rather than treating it as a transport failure to retry.
    return _error(f"Permission denied: {exc.reason}")
