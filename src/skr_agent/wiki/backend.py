"""Storage interface for the wiki, plus a fixture-backed stand-in.

The real backend is being built by another team. What matters for this design
is the shape of the interface they need to satisfy — specifically that every
page carries ``source_refs`` pointing at the raw weekly reports it was
distilled from, because the whole feature rests on being able to answer "where
did this claim come from".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Protocol

__all__ = ["WikiPage", "RawReport", "WikiBackend", "InMemoryWikiBackend"]


@dataclass(slots=True)
class WikiPage:
    namespace: str
    slug: str
    title: str
    body: str
    source_refs: list[str] = field(default_factory=list)
    """Raw weekly-report ids this page was built from. Never empty in practice."""
    updated: str = ""

    @property
    def ref(self) -> str:
        return f"{self.namespace}/{self.slug}"


@dataclass(slots=True)
class RawReport:
    report_id: str
    namespace: str
    week_of: str
    author: str
    body: str


class WikiBackend(Protocol):
    """What the wiki team implements. Deliberately dumb — no authorization here.

    Authorization is a separate concern layered above (see ``authz``); a storage
    layer that also decides permissions is one that has to be re-audited every
    time either changes.
    """

    def search(self, query: str, namespaces: list[str], limit: int = 5) -> list[WikiPage]: ...
    def get_page(self, namespace: str, slug: str) -> WikiPage | None: ...
    def get_raw_report(self, report_id: str) -> RawReport | None: ...
    def upsert_page(self, page: WikiPage) -> WikiPage: ...
    def list_namespaces(self) -> list[str]: ...


class InMemoryWikiBackend:
    """Fixture-backed stand-in with naive keyword scoring.

    Good enough to exercise the agent end to end; replace with the real service
    without touching anything above this line.
    """

    def __init__(
        self,
        pages: list[WikiPage] | None = None,
        reports: list[RawReport] | None = None,
    ) -> None:
        self._pages: dict[str, WikiPage] = {p.ref: p for p in (pages or [])}
        self._reports: dict[str, RawReport] = {r.report_id: r for r in (reports or [])}

    # -- construction -------------------------------------------------------

    @classmethod
    def from_fixtures(cls, root: str | Path) -> "InMemoryWikiBackend":
        root = Path(root)
        pages = [
            WikiPage(**p) for p in json.loads((root / "wiki_pages.json").read_text())
        ]
        reports = [
            RawReport(**r) for r in json.loads((root / "raw_reports.json").read_text())
        ]
        return cls(pages, reports)

    # -- WikiBackend --------------------------------------------------------

    def search(self, query: str, namespaces: list[str], limit: int = 5) -> list[WikiPage]:
        terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 2]
        if not terms:
            return []
        scored: list[tuple[int, WikiPage]] = []
        for page in self._pages.values():
            if namespaces and "*" not in namespaces and page.namespace not in namespaces:
                continue
            haystack = f"{page.title} {page.body}".lower()
            score = sum(haystack.count(t) for t in terms)
            # A title hit is worth more than a body mention.
            score += 5 * sum(1 for t in terms if t in page.title.lower())
            if score:
                scored.append((score, page))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [page for _, page in scored[:limit]]

    def get_page(self, namespace: str, slug: str) -> WikiPage | None:
        return self._pages.get(f"{namespace}/{slug}")

    def get_raw_report(self, report_id: str) -> RawReport | None:
        return self._reports.get(report_id)

    def upsert_page(self, page: WikiPage) -> WikiPage:
        existing = self._pages.get(page.ref)
        if existing is not None:
            # Merge rather than replace source refs: a page accumulates the
            # reports it was built from across weeks.
            merged = list(dict.fromkeys(existing.source_refs + page.source_refs))
            page.source_refs = merged
        page.updated = page.updated or date.today().isoformat()
        self._pages[page.ref] = page
        return page

    def list_namespaces(self) -> list[str]:
        return sorted({p.namespace for p in self._pages.values()})
