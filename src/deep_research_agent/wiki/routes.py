"""Turning a wiki reference into a URL a reader can click.

A route has three parts — ``{namespace}/{kind}/{name}`` — because a namespace
holds more than one sort of thing. Today the only kind is ``pages``; ``raw``,
for the ingested source files a page was built from, is coming. The kind is in
the route from the start rather than added later because a two-part route
would make ``supply/acme`` ambiguous the moment a raw file shares a name with
a page, and by then the ambiguity would be baked into stored references.

``{base}`` itself is still a mock: it comes from configuration and defaults to
an obvious placeholder. When the real host is known that is the only thing
that changes.

A *URL* always states the kind. A *reference* need not: ``supply/acme`` and
``supply/pages/acme`` name the same page, and ``canonical`` folds them onto
the short one, since ``pages`` is what an omitted kind means anyway. Refs for
any other kind keep it — ``supply/raw/spec.pdf`` has to. Anything coming from
outside (an MCP wiki search, most importantly) should build refs with
``page_ref`` rather than assembling a string.

Nothing else in the codebase should build a wiki URL by string concatenation.
A route spelled out in three places is a route that will disagree with itself
the first time it changes.
"""

from __future__ import annotations

from deep_research_agent.config import get_wiki

__all__ = [
    "PAGE_KIND",
    "RAW_KIND",
    "KINDS",
    "page_url",
    "page_ref",
    "raw_report_url",
    "split_ref",
    "canonical",
    "ref_from_url",
]

PAGE_KIND = "pages"
"""A reader-facing wiki page. The kind assumed when a ref omits one."""

RAW_KIND = "raw"
"""An ingested source file. Not served yet — reserved so that refs written
today do not have to be rewritten when it is."""

KINDS = (PAGE_KIND, RAW_KIND)


def split_ref(ref: str) -> tuple[str, str, str] | None:
    """``ref`` -> ``(namespace, kind, name)``, or ``None`` if not a wiki ref.

    Accepts both the short form (``supply/acme``, kind assumed to be
    ``pages``) and the explicit one (``supply/pages/acme``). A name may itself
    contain slashes; only the first two segments are structural.

    An unrecognised kind is *not* treated as a kind — ``supply/a/b`` where
    ``a`` is not a known kind reads as the page named ``a/b``. Guessing that
    any second segment is a kind would silently rewrite page names that happen
    to contain a slash. The cost is that a page literally named ``raw/x``
    cannot be addressed; a reserved word is the cheaper of the two problems.

    A URL is not a ref. Rejecting it here rather than at each call site is
    what stops ``https://news.test/x`` — an external article, which keeps its
    own URL as its reference — from being read as namespace ``https:``.
    """
    if "://" in ref or "/" not in ref:
        return None
    namespace, remainder = ref.split("/", 1)
    if not namespace or not remainder:
        return None

    kind, sep, name = remainder.partition("/")
    if sep and kind in KINDS and name:
        return (namespace, kind, name)
    return (namespace, PAGE_KIND, remainder)


def canonical(ref: str) -> str:
    """One spelling per reference. Anything that is not a wiki ref is returned
    unchanged.

    The two spellings have to fold together somewhere: a draft cites a page
    through a link, which round-trips through ``ref_from_url`` carrying an
    explicit ``pages``, while ``source_refs`` and this repo's own fixtures use
    the short form. Compared as written, the cross-check would report a
    missing source for a page cited exactly right.

    Canonical is the **short** form for pages — ``pages`` is the assumed kind,
    so stating it adds nothing — and the explicit form for every other kind.
    That keeps ``supply/acme`` canonical, which is what the corpus, the
    retrieval store and the fixtures are all keyed by; canonicalising the
    other way would have meant re-keying all three to say something already
    implied.
    """
    parts = split_ref(ref)
    if parts is None:
        return ref
    namespace, kind, name = parts
    return f"{namespace}/{name}" if kind == PAGE_KIND else f"{namespace}/{kind}/{name}"


def page_ref(namespace: str, name: str, kind: str = PAGE_KIND) -> str:
    """Build a canonical ref from the parts a source hands you.

    This is the seam for anything outside this repo — an MCP wiki search
    returns a namespace and a page name, not a ref, and assembling one at the
    call site is how a route ends up spelled three different ways.
    """
    return canonical(f"{namespace}/{kind}/{name}")


def page_url(ref: str) -> str | None:
    """A URL for a wiki ref, or ``None`` when the ref is not one.

    ``None`` rather than a guessed URL: a link that 404s is worse than a
    reference that stayed in its original form, because a reader who follows
    it concludes the source does not exist.
    """
    parts = split_ref(ref)
    if parts is None:
        return None
    namespace, kind, name = parts
    return f"{get_wiki().resolved_base_url()}/{namespace}/{kind}/{name}"


def raw_report_url(report_id: str) -> str | None:
    """Raw weekly reports have no reader-facing page in this mock.

    They are provenance — the thing a published page was distilled from — and
    the supply-chain format deliberately keeps them out of the body and in
    ``source_refs`` only. Returning ``None`` is the honest answer rather than
    inventing a route nobody serves.

    Not to be confused with ``RAW_KIND``: that is an ingested source file in
    the wiki, this is a weekly report id (``rpt-...``) that the wiki does not
    serve at all.
    """
    return None


def ref_from_url(url: str) -> str | None:
    """The inverse of ``page_url``: a wiki URL back to a canonical ref.

    Needed because once references are rendered as markdown links, what a
    parser finds in the body is the link *target*, not the reference. Without
    this, a page cited correctly as
    ``[Name](https://wiki/.../supply/pages/acme)`` reads as a URL that matches
    nothing in ``source_refs`` — the check would report a defect in a draft
    that is exactly right.

    Returns ``None`` for anything that is not a wiki URL, so an external
    article keeps its own URL as its reference.
    """
    base = get_wiki().resolved_base_url()
    if not url.startswith(base + "/"):
        return None
    remainder = url[len(base) + 1:].strip("/")
    return canonical(remainder) if split_ref(remainder) else None
