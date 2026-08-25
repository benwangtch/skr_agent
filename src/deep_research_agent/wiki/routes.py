"""Turning a wiki reference into a URL a reader can click.

**This is a mock.** ``page_url`` builds ``{base}/{namespace}/{slug}`` from a
configured base, which is a plausible shape and almost certainly not the real
one. It is one function on purpose: when the real routing is known, this is
the only thing that changes, and everything that renders a link — the
supply-chain reference formatter, anything added later — follows.

Nothing else in the codebase should build a wiki URL by string concatenation.
A route spelled out in three places is a route that will disagree with itself
the first time it changes.
"""

from __future__ import annotations

from deep_research_agent.config import get_wiki

__all__ = ["page_url", "raw_report_url", "split_ref", "ref_from_url"]


def split_ref(ref: str) -> tuple[str, str] | None:
    """``"supply/acme"`` -> ``("supply", "acme")``, or ``None`` if malformed."""
    if "/" not in ref:
        return None
    namespace, slug = ref.split("/", 1)
    return (namespace, slug) if namespace and slug else None


def page_url(ref: str) -> str | None:
    """A URL for ``namespace/slug``, or ``None`` when the ref is not a page.

    ``None`` rather than a guessed URL: a link that 404s is worse than a
    reference that stayed in its original form, because a reader who follows
    it concludes the source does not exist.
    """
    parts = split_ref(ref)
    if parts is None:
        return None
    namespace, slug = parts
    return f"{get_wiki().resolved_base_url()}/{namespace}/{slug}"


def raw_report_url(report_id: str) -> str | None:
    """Raw weekly reports have no reader-facing page in this mock.

    They are provenance — the thing a published page was distilled from — and
    the supply-chain format deliberately keeps them out of the body and in
    ``source_refs`` only. Returning ``None`` is the honest answer rather than
    inventing a route nobody serves.
    """
    return None


def ref_from_url(url: str) -> str | None:
    """The inverse of ``page_url``: a wiki URL back to ``namespace/slug``.

    Needed because once references are rendered as markdown links, what a
    parser finds in the body is the link *target*, not the reference. Without
    this, a page cited correctly as ``[Name](https://wiki/.../supply/acme)``
    reads as a URL that matches nothing in ``source_refs`` — the check would
    report a defect in a draft that is exactly right.

    Returns ``None`` for anything that is not a wiki page URL, so an external
    article keeps its own URL as its reference.
    """
    base = get_wiki().resolved_base_url()
    if not url.startswith(base + "/"):
        return None
    remainder = url[len(base) + 1:].strip("/")
    return remainder if split_ref(remainder) else None
