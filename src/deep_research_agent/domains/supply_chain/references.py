"""The supply-chain report's citation format: markdown links on the Sources line.

The house rule, and where each part comes from:

* Every entry on a ``**Sources:**`` line is a markdown link, ``[text](target)``.
* **News** links to the article URL.
* **Wiki pages** link to the page's route (``wiki/routes.py`` — a mock until
  the real routing is known).
* The link text is the document's **name as the tool returned it** — the wiki
  page's title, the article's headline. Taken from the run's retrieval store
  rather than written by the model, so it matches the source rather than the
  model's memory of it.
* **Raw report ids stay out of the body.** They are provenance, not something
  a reader clicks: they belong in the publish call's ``source_refs``, where
  the aggregation check reads them. A ``rpt-`` id in the body is a defect.

Two tools implement this, and the split is the point:

``format_reference``
    Given a reference, returns the exact markdown. The agent does not have to
    reconstruct a format from a description, which is where a model reliably
    burns turns getting it almost right.

the domain's rules
    Merged into the one ``check_references`` call rather than shipped as a
    separate checker. Two checkers would mean two verdicts and two gates, and
    a draft that failed only the domain one would still be publishable.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any, Mapping

from deep_research_agent.core.references import (
    DEFAULT_FORMAT,
    ReferenceContext,
    ReferenceFormat,
    Violation,
)
from deep_research_agent.wiki.routes import page_url, ref_from_url

__all__ = [
    "SUPPLY_CHAIN_FORMAT",
    "canonical_ref",
    "format_reference",
    "SUPPLY_CHAIN_RULES",
    "markdown_links_only",
    "no_raw_reports_in_body",
    "RAW_REPORT_PATTERN",
    "MARKDOWN_LINK",
]

RAW_REPORT_PATTERN = re.compile(r"\brpt-[A-Za-z0-9][A-Za-z0-9-]*\b")
MARKDOWN_LINK = re.compile(r"\[(?P<text>[^\]]+)\]\((?P<target>[^)]+)\)")


def format_reference(ref: str, corpus: Mapping[str, Mapping[str, Any]]) -> str | None:
    """The markdown for one reference, or ``None`` if it does not belong in
    the body.

    ``None`` covers the two cases where inventing a link would be worse than
    declining: a raw report id, which belongs in ``source_refs`` only, and a
    reference nothing loaded describes, where the link text would have to be
    guessed.
    """
    if RAW_REPORT_PATTERN.fullmatch(ref):
        return None

    document = corpus.get(ref)
    if document is None:
        return None

    # The name as the source gave it. Falling back to the ref keeps the link
    # usable when a source supplied no title, which is better than an empty
    # `[]()` that renders as nothing.
    text = str(document.get("title") or ref).strip()

    if ref.startswith(("http://", "https://")):
        return f"[{text}]({ref})"

    url = page_url(ref)
    return f"[{text}]({url})" if url else None


def _sources_lines(content: str, context: ReferenceContext) -> list[tuple[int, str]]:
    """``(line_number, text_after_the_marker)`` for every Sources line."""
    marker = context.fmt.sources_marker
    out: list[tuple[int, str]] = []
    for number, line in enumerate(content.splitlines(), start=1):
        if marker in line:
            out.append((number, line.split(marker, 1)[1].strip()))
    return out


def markdown_links_only(content: str, context: ReferenceContext) -> list[Violation]:
    """Every entry on a Sources line must be a markdown link.

    Checked by looking at what is *left* once the links are removed, rather
    than by trying to parse the entries: a bare URL, a bare page ref and a
    half-written link all survive that removal, and enumerating their shapes
    up front would mean missing whichever one nobody thought of.
    """
    violations: list[Violation] = []
    for number, text in _sources_lines(content, context):
        if not text:
            continue  # empty_sources already reports this
        residue = MARKDOWN_LINK.sub("", text)
        leftover = residue.strip(" ,;.—-")
        if leftover:
            violations.append(Violation(
                kind="unlinked_source", severity="error", line=number,
                message=(
                    f"{leftover!r} is on a Sources line but is not a markdown link. "
                    f"Every source must be [name](url) -- call format_reference to "
                    f"get the exact form rather than writing it by hand"
                ),
            ))
    return violations


def no_raw_reports_in_body(content: str, context: ReferenceContext) -> list[Violation]:
    """Raw report ids belong in ``source_refs``, not in the page.

    They are what the page was distilled from, and the aggregation check reads
    them from the publish call. In the body they are an id a reader cannot
    follow, sitting where a link should be.
    """
    violations: list[Violation] = []
    for number, line in enumerate(content.splitlines(), start=1):
        for match in RAW_REPORT_PATTERN.finditer(line):
            violations.append(Violation(
                kind="raw_report_in_body", severity="error", line=number,
                message=(
                    f"{match.group(0)!r} is a raw report id. Those go in source_refs "
                    f"on the publish call, not in the page -- a reader cannot follow "
                    f"one. Cite the wiki page it backs instead"
                ),
            ))
    return violations


SUPPLY_CHAIN_RULES = (markdown_links_only, no_raw_reports_in_body)
"""Merged into ``check_references`` by the domain. One check, one verdict."""


def canonical_ref(extracted: str) -> str:
    """A wiki link target back to ``namespace/slug``; anything else unchanged.

    Once sources are rendered as links, the parser finds the target rather
    than the reference. Without this the check would report a correctly cited
    page as a URL matching nothing in ``source_refs``, which is the worst kind
    of defect report: one that is wrong about a draft that is right.
    """
    return ref_from_url(extracted) or extracted


SUPPLY_CHAIN_FORMAT = dataclasses.replace(DEFAULT_FORMAT, normalise_ref=canonical_ref)
"""The shipped structure, plus the link-target mapping this format needs."""
