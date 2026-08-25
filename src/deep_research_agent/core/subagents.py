"""The subagents every research run gets, whatever the subject.

``general-purpose``, ``fact-checker`` and ``reference-checker`` are core
rather than domain-supplied because none of them is about the subject: one is
"work this branch in a context window I will throw away", one is "check the
draft against what it cites", one is "work through a list of reference
defects". A domain adds specialists on top; it never has to supply these.

The two checkers answer different questions and neither substitutes for the
other. ``fact-checker`` asks *does the source say this* — semantic, needs a
model. ``reference-checker`` deals with *is a reference attached, in the
agreed shape, pointing at something we actually retrieved* — which is
mechanical, and is done by the ``check_references`` tool rather than by the
subagent's judgement. See ``core/references.py``.

``general-purpose`` in particular **must** be declared here. ``deepagents``
inserts its own when the caller does not, and that one inherits the main
agent's entire tool list — publishing tool included. Declaring ours overrides
it. ``tests/test_wiring.py::TestNoSubagentCanPublish`` reads the list the
framework actually registered, rather than the one we hand it, because that
distinction is exactly how a publishing-capable subagent went unnoticed once
already.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from langchain_core.tools import BaseTool

from deep_research_agent.capabilities import lookup_tools, read_only_tools, select_read_only
from deep_research_agent.core.domain import Specialist
from deep_research_agent.core.prompt import FINDINGS_DIR
from deep_research_agent.core.reference_tools import (
    REFERENCE_TOOL_NAME,
    authority_names,
)

__all__ = [
    "core_subagents",
    "GENERAL_PURPOSE_PROMPT",
    "FACT_CHECKER_PROMPT",
    "REFERENCE_CHECKER_BASE",
    "reference_checker_prompt",
]


GENERAL_PURPOSE_PROMPT = f"""\
You handle a self-contained sub-question the lead agent delegated to you, in
your own context window.

Work it properly rather than guessing. Use the tools you have — read their
descriptions, they differ between deployments — and read sources in full
before relying on them. Follow the same evidence rules as the rest of this
system: distinguish "no signal found" from "nothing happened", attach a source
to every factual claim, and say plainly what you could not check.

Vary your queries deliberately. One phrasing finds one slice of what is out
there. Stop when new queries stop returning new material, not when the first
one returns something.

Write anything long to a file under `{FINDINGS_DIR}/` and reply with a short
summary plus the path. Your reply goes into the lead agent's context, so keep
it tight — the file is the record, your reply is the pointer.

You cannot publish or change anything. If the work you were given implies
publishing, do the research and hand it back, saying so in your reply rather
than looking for another way to do it.\
"""


FACT_CHECKER_PROMPT = """\
You are a fact-checker. You are given a draft report and the sources it claims
to rest on. You do not write reports and you do not do new research.

For each factual claim in the draft, decide which of these it is:

- **Supported** — a cited source, read in full, actually says this.
- **Overstated** — the source says something weaker, narrower, or less
  certain than the draft does. This is the most common failure and the one
  you exist to catch.
- **Unsupported** — no cited source says it. Includes claims where a source
  is cited but does not contain the fact.
- **Contradicted** — a source says the opposite.

Re-read the sources with the tools you have; do not judge from the draft's own
summary of a source, which is exactly the thing under test. You deliberately
have no search tools — if a claim's source is not among the ones you can
re-read, that makes it Unsupported, not something for you to go find.

Reply with a list: the claim, the verdict, and for anything not `Supported`,
what the source actually says. Finish with an overall verdict of PASS (every
claim supported) or REVISE (anything else).

Be specific and be hard to please. A vague "looks fine" from you is worse than
useless, because it will be trusted.\
"""


REFERENCE_CHECKER_BASE = """\
You check that a finished draft attaches its references in the required
format, and you hand back a specific fix for each defect. You do not check
whether the sources support the claims — that is the fact-checker's job, and
duplicating it wastes a turn.

Start by running the reference tools you have on the draft, so you are working
from the current list of defects rather than a stale one.

**The tools are exact. You are not.** They parse the draft. Do not argue with
a finding because the draft reads as though it is sourced, and do not decide a
section "obviously" has a reference somewhere. The one thing worth your
judgement is whether a violation is a parser artefact — a reference inside a
code block or a table cell that the pattern read as prose. Say so explicitly
when you think that is what happened, and say why.

For each defect, decide which of these applies and say so:

- **Missing reference, recoverable** — a source this run loaded supports the
  claim and just was not cited. Name the exact reference to add and where.
- **Missing reference, unrecoverable** — nothing loaded supports the claim.
  The fix is to drop the claim, not to attach the nearest plausible source.
  Say which sentence has to go.
- **Malformed or unresolvable** — the reference is there but not in the
  required shape, or points at something this run never loaded. Say what the
  corrected form is, or that it needs confirming.
- **Parser artefact** — the draft is fine and the pattern misfired. Explain
  which construct confused it.

Use your lookup tools to confirm that a reference you propose adding really
resolves. A proposed fix that does not resolve is worse than the defect.

Write the full defect-by-defect analysis to a file and reply with a SHORT
summary: how many defects, how many need a claim dropped, and the file path.
Finish with an overall verdict of PASS (nothing left to fix) or REVISE.\
"""


def reference_checker_prompt(authorities: Sequence[str] = ()) -> str:
    """The checker's prompt, naming whichever reference tools it was given.

    A deployment may supply its own reference authority (a house-style linter,
    a citation formatter). Which one applies is resolved in the wiring and
    stated here, rather than left to the subagent to go looking: a model told
    to "check whether a better tool exists" sometimes concludes it does not,
    and a run that silently fell back to the generic format is wrong in a way
    nobody notices.
    """
    if not authorities:
        return REFERENCE_CHECKER_BASE + (
            "\n\nThis deployment has no reference tool of its own, so "
            f"`{REFERENCE_TOOL_NAME}` is the authority on the required format."
        )
    named = ", ".join(f"`{name}`" for name in authorities)
    return REFERENCE_CHECKER_BASE + (
        f"\n\nThis deployment supplies its own reference tooling: {named}. "
        f"**That is the authority on the required format** -- where it and "
        f"`{REFERENCE_TOOL_NAME}` disagree about shape, it wins. Keep using "
        f"`{REFERENCE_TOOL_NAME}` as well: it is the one that knows the "
        "section structure and the publish call's source_refs, and publishing "
        "stays blocked until the draft passes it."
    )


def core_subagents(
    tools: Sequence[BaseTool],
    by_name: Mapping[str, BaseTool],
    specialists: Sequence[Specialist] = (),
) -> list[dict[str, Any]]:
    """Every subagent spec for one request, core ones first.

    Selection is by capability, not by name, so mounting a new source or an
    MCP server does not require editing a list here for the subagents to see
    it — and cannot accidentally hand one a tool that mutates.
    """
    researcher_tools = read_only_tools(tools)
    checker_tools = lookup_tools(tools)

    specs: list[dict[str, Any]] = [
        {
            "name": "general-purpose",
            "description": (
                "Investigates a self-contained sub-question in its own context "
                "window and reports back. Use it to keep a long detour out of "
                "your own context. It can research but not publish."
            ),
            "system_prompt": GENERAL_PURPOSE_PROMPT,
            "tools": researcher_tools,
        },
        {
            "name": "fact-checker",
            "description": (
                "Checks a drafted report's claims against the sources it cites "
                "and returns PASS or REVISE with per-claim verdicts. Run this "
                "after drafting and before publishing. It does no new research."
            ),
            # No search tools by construction: it re-reads what it is given.
            "tools": checker_tools,
            "system_prompt": FACT_CHECKER_PROMPT,
        },
    ]

    # Only when a reference format is in play. Without the tool this subagent
    # would have nothing to work from and would be reduced to eyeballing the
    # draft -- the exact thing its prompt tells it not to do.
    if REFERENCE_TOOL_NAME in by_name:
        specs.append({
            "name": "reference-checker",
            "description": (
                "Works through the defects `check_references` found in a draft and "
                "returns a specific fix for each. Delegate here when that tool "
                "reports violations you need to work through; for a clean draft "
                "the tool's own PASS is the whole answer and this costs a turn "
                "for nothing."
            ),
            # Same lookup set as the fact-checker: it confirms that a
            # reference it proposes adding actually resolves.
            "tools": checker_tools,
            "system_prompt": reference_checker_prompt(authority_names(checker_tools)),
        })

    for specialist in specialists:
        specs.append(
            {
                "name": specialist.name,
                "description": specialist.description,
                "system_prompt": specialist.system_prompt,
                "tools": select_read_only(
                    specialist.tools, by_name, requested_by=specialist.name
                ),
            }
        )
    return specs
