"""Checking that a draft's references are present, well-formed, and real.

This is a different question from the one ``fact-checker`` answers. That one
asks *does the cited source actually say this*, which is a semantic judgement
and needs a model. This asks *is a reference attached, and is it in the shape
we agreed* — which is parsing, and parsing should not be delegated to a model.

The distinction matters because the failure modes are different. Asking a
model to confirm that all twenty findings in a report carry sources is an
exhaustive-counting task, and it will quietly miss one; which one it misses
changes between runs. A parser misses none, costs nothing, and says exactly
where the gap is.

Four checks, in increasing order of what they are worth:

``format``
    A claim-bearing section carries a sources line at all.

``shape``
    Each reference parses as one of the forms this deployment uses — a URL, a
    ``namespace/slug`` page reference, a raw report id.

``resolvable``
    Each reference points at something in the run's retrieval store, and that
    document carries what the format needs — a title, a date. This is not a
    grounding check and does not try to be one: an unresolvable reference is
    reported as *"this cannot be formatted, because nothing loaded describes
    it"*, which is a formatting fact, not an accusation.

``declaration``
    The ``source_refs`` passed to the publish call and the references in the
    body agree, in both directions. A body reference missing from
    ``source_refs`` breaks provenance on the page; a declared ref that appears
    nowhere in the body is usually a leftover from an earlier draft.

Everything here is a pure function over text. ``reference_tools.py`` is what
binds it to a request's citation record and exposes it as a tool.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Sequence

__all__ = [
    "ReferenceFormat",
    "Violation",
    "ReferenceReport",
    "DEFAULT_FORMAT",
    "extract_references",
    "check_references",
]


Severity = Literal["error", "warning"]


@dataclass(frozen=True)
class ReferenceFormat:
    """What "properly referenced" means for this deployment.

    Defaults match ``skills/incident-report/SKILL.md``. A domain whose output
    looks different — a patent brief, a regulatory filing summary — overrides
    it through ``ResearchDomain.reference_format`` rather than editing this.
    """

    section_pattern: str = r"^###\s+(?P<title>.+?)\s*$"
    """A claim-bearing section. Sections are what must carry sources; prose
    outside them (a summary, a coverage note) is not held to it, because
    demanding a citation on "the following covers four suppliers" produces
    noise that trains people to ignore the checker."""

    sources_marker: str = "**Sources:**"
    """How a section introduces its references."""

    exempt_pattern: str = r"(?:—|--|-)\s*none\s*$"
    """Sections exempt from needing sources — a "nothing found" entry has
    nothing to cite. Absence is a finding, so it is not silently skipped:
    an exempt section must carry ``exempt_marker`` instead."""

    exempt_marker: str = "**Queries run:**"
    """What an exempt section must show instead, so a reader can tell
    "checked, found nothing" from "not checked"."""

    ref_patterns: dict[str, str] = field(default_factory=lambda: {
        # Order matters: URLs are consumed first, otherwise the page-reference
        # pattern matches the path inside them.
        "external_url": r"https?://[^\s,;)\]]+",
        # Uppercase is not optional here: this repo's ids look like
        # `rpt-supply-2026-W28`, and a lowercase-only class silently matched
        # none of them -- a checker blind to every raw report in the corpus.
        "raw_report": r"\brpt-[A-Za-z0-9][A-Za-z0-9-]*\b",
        # Namespace stays lowercase on purpose. Allowing uppercase here makes
        # ordinary prose ("AND/OR", "TCP/IP") parse as page references.
        "wiki_page": r"\b[a-z][a-z0-9_-]*/[A-Za-z0-9][A-Za-z0-9._-]*\b",
    })
    """The reference shapes this deployment accepts, tried in order."""

    required_document_fields: tuple[str, ...] = ()
    """Fields the loaded document must carry for a reference to be renderable
    in this format — e.g. ``("title", "published")`` for a house style that
    prints an outlet and a date.

    Empty by default: the shipped rubric asks for a bare reference, and
    demanding a date from a source that has none would report a defect the
    agent cannot fix. Set it when the format genuinely needs the field."""

    def compiled_sections(self) -> re.Pattern[str]:
        return re.compile(self.section_pattern, re.M)

    def is_exempt(self, section_title: str) -> bool:
        return re.search(self.exempt_pattern, section_title, re.I) is not None


DEFAULT_FORMAT = ReferenceFormat()


@dataclass(frozen=True)
class Violation:
    """One thing wrong, located precisely enough to fix without re-reading."""

    kind: str
    severity: Severity
    message: str
    line: int = 0
    section: str = ""

    def render(self) -> str:
        where = f"line {self.line}" if self.line else "document"
        section = f" [{self.section}]" if self.section else ""
        return f"{self.severity.upper()} {self.kind} ({where}){section}: {self.message}"


@dataclass(frozen=True)
class ReferenceReport:
    violations: tuple[Violation, ...]
    references: tuple[str, ...]
    """Every reference found in the body, deduplicated, in first-seen order."""

    @property
    def passed(self) -> bool:
        """Warnings do not fail the check.

        A warning is something a human should look at; an error is something
        that makes the page wrong. Blocking a publish on a warning would make
        the checker the thing people learn to route around.
        """
        return not any(v.severity == "error" for v in self.violations)

    def render(self) -> str:
        head = (
            f"{'PASS' if self.passed else 'FAIL'} — {len(self.references)} reference(s), "
            f"{sum(1 for v in self.violations if v.severity == 'error')} error(s), "
            f"{sum(1 for v in self.violations if v.severity == 'warning')} warning(s)"
        )
        if not self.violations:
            return head + "\nEvery claim-bearing section is sourced and every reference is well formed."
        return head + "\n" + "\n".join(v.render() for v in self.violations)


def extract_references(text: str, fmt: ReferenceFormat = DEFAULT_FORMAT) -> list[str]:
    """Every reference in ``text``, in first-seen order, deduplicated.

    Patterns are applied in declaration order and each match is blanked out
    before the next pattern runs, so a URL is not also reported as a
    ``namespace/slug`` page reference because of its path.
    """
    found: list[str] = []
    seen: set[str] = set()
    remaining = text
    for pattern in fmt.ref_patterns.values():
        for match in re.finditer(pattern, remaining):
            ref = match.group(0).rstrip(".,;:")
            if ref not in seen:
                seen.add(ref)
                found.append(ref)
        remaining = re.sub(pattern, lambda m: " " * len(m.group(0)), remaining)
    return found


def _sections(text: str, fmt: ReferenceFormat) -> list[tuple[str, int, str]]:
    """``(title, start_line, body)`` for each claim-bearing section."""
    lines = text.splitlines()
    pattern = fmt.compiled_sections()
    starts: list[tuple[str, int]] = []
    for index, line in enumerate(lines, start=1):
        match = pattern.match(line)
        if match:
            starts.append((match.group("title").strip(), index))

    out: list[tuple[str, int, str]] = []
    for position, (title, line_no) in enumerate(starts):
        end = starts[position + 1][1] - 1 if position + 1 < len(starts) else len(lines)
        out.append((title, line_no, "\n".join(lines[line_no:end])))
    return out


def check_references(
    content: str,
    *,
    corpus: Mapping[str, Mapping[str, Any]] | None = None,
    declared: Sequence[str] | None = None,
    fmt: ReferenceFormat = DEFAULT_FORMAT,
) -> ReferenceReport:
    """Check one draft.

    ``corpus`` maps a reference to what is known about the document behind it
    — ``title``, ``published``, whatever the source supplied. It comes from
    the run's retrieval store (``ToolContext.documents``) and is what lets the
    check say "this cannot be cited in the required form because the loaded
    copy has no date". ``None`` skips those checks, which is right for a
    caller checking a document this process did not produce.

    ``declared`` is the ``source_refs`` list the caller intends to pass to the
    publish call, when there is one. ``None`` means "not publishing yet", and
    that cross-check is skipped rather than reported as missing.
    """
    violations: list[Violation] = []
    body_refs = extract_references(content, fmt)

    for title, line_no, body in _sections(content, fmt):
        if fmt.is_exempt(title):
            if fmt.exempt_marker not in body:
                violations.append(Violation(
                    kind="unexplained_absence", severity="error", line=line_no, section=title,
                    message=(
                        f"a 'none' section must show {fmt.exempt_marker} so a reader can "
                        f"tell 'checked, found nothing' from 'not checked'"
                    ),
                ))
            continue

        if fmt.sources_marker not in body:
            violations.append(Violation(
                kind="unsourced_section", severity="error", line=line_no, section=title,
                message=f"no {fmt.sources_marker} line; every claim carries a source",
            ))
            continue

        marker_line = body.split(fmt.sources_marker, 1)[1].split("\n", 1)[0]
        if not extract_references(marker_line, fmt):
            violations.append(Violation(
                kind="empty_sources", severity="error", line=line_no, section=title,
                message=f"{fmt.sources_marker} is present but names no reference",
            ))

    # Resolvability. Not a grounding check: the question is whether the
    # reference can be rendered in the required form, which needs the document
    # the store holds. A reference nothing loaded describes is a warning --
    # the corpus is not guaranteed complete, and treating an incomplete corpus
    # as evidence of a bad citation would produce exactly the false positives
    # that get a checker ignored.
    if corpus is not None:
        for ref in body_refs:
            document = corpus.get(ref)
            if document is None:
                violations.append(Violation(
                    kind="unresolvable_reference", severity="warning",
                    line=_line_of(content, ref),
                    message=(
                        f"{ref!r} is not among the documents this run loaded, so it "
                        f"cannot be checked or reformatted -- confirm it is right"
                    ),
                ))
                continue
            for field_name in fmt.required_document_fields:
                if not document.get(field_name):
                    violations.append(Violation(
                        kind="incomplete_reference", severity="warning",
                        line=_line_of(content, ref),
                        message=(
                            f"{ref!r} has no {field_name}, so it cannot be cited in "
                            f"the required form"
                        ),
                    ))

    if declared is not None:
        declared_set = set(declared)
        if not declared_set:
            violations.append(Violation(
                kind="no_declared_sources", severity="error",
                message="source_refs is empty; the publish call will be rejected",
            ))
        for ref in body_refs:
            if ref not in declared_set:
                violations.append(Violation(
                    kind="undeclared_reference", severity="error",
                    line=_line_of(content, ref),
                    message=f"{ref!r} is cited in the body but missing from source_refs",
                ))
        for ref in sorted(declared_set - set(body_refs)):
            # Loaded but not quoted is provenance, not a leftover. Reading a
            # wiki page also returns the raw reports it was distilled from, and
            # declaring those is exactly what the provenance rule asks for --
            # warning about it would be the kind of noise that teaches people
            # to skip warnings.
            if corpus is not None and ref in corpus:
                continue
            violations.append(Violation(
                kind="unused_declaration", severity="warning",
                message=(
                    f"{ref!r} is in source_refs but was neither cited in the body "
                    f"nor loaded during this run -- usually a leftover from an "
                    f"earlier draft"
                ),
            ))

    return ReferenceReport(violations=tuple(violations), references=tuple(body_refs))


def _line_of(text: str, needle: str) -> int:
    for index, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return index
    return 0
