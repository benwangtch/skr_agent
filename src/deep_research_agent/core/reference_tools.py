"""The reference check as a tool, and how a deployment overrides it.

Two things live here.

**The general checker.** ``check_references`` binds ``core/references.py`` to
one request: the run's retrieval store, so the check can say "the loaded copy
of this source has no date, so it cannot be cited in the required form", and
the approval record, so a passing draft can be published at all.

**Discovery of a better one.** A deployment may have its own reference
authority — a house-style linter, a citation formatter, an MCP server that
knows the corporate format. It marks such a tool with ``reference_authority``
and the wiring finds it, exactly the way subagent tool selection finds
capabilities rather than matching names:

    reference_authority(tool)            # this deployment's own checker

Discovery is done in the wiring, not by asking the model to look around. A
model told to "check whether a better tool exists" sometimes decides one does
not, and a research run that silently used the fallback format is a run whose
output is wrong in a way nobody notices. So the subagent is *handed* whichever
applies, and its prompt names it.

The general checker is not dropped when a deployment tool is present. They
answer overlapping but different questions — ours knows the section structure
and the publish call's ``source_refs``, theirs knows the house style — and the
subagent is told which takes precedence on a disagreement.

**Why the approval record exists.** A prompt instruction to "check before
publishing" is a request the model can skip; on a long run it eventually will.
The check therefore records a fingerprint of the exact text that passed, and
``wiki_write_page`` refuses a body whose fingerprint is absent. That is the
same move the ``source_refs`` requirement already makes — refuse the write and
say what would make it succeed — rather than a new kind of enforcement.
"""

from __future__ import annotations

from typing import Any, Sequence

from langchain_core.tools import BaseTool, StructuredTool

from deep_research_agent.capabilities import lookup
from deep_research_agent.core.references import (
    DEFAULT_FORMAT,
    ReferenceFormat,
    check_references,
)
from deep_research_agent.protocol import RetrievedDocument
from deep_research_agent.runtime import ToolBundle, ToolContext, ToolsetFactory

__all__ = [
    "make_reference_toolset",
    "reference_toolsets",
    "reference_authority",
    "is_reference_authority",
    "authority_names",
    "REFERENCE_TOOL_NAME",
]

REFERENCE_TOOL_NAME = "check_references"
_AUTHORITY = "reference_authority"


def reference_authority(tool: BaseTool) -> BaseTool:
    """Mark a tool as this deployment's own reference checker or formatter.

    Wrap whatever you already have — a house-style linter, a citation
    formatter, a tool from an MCP server — and the reference-checker subagent
    receives it and is told to prefer it over the general check.

    It still has to be capability-declared (``lookup`` / ``search``) to reach
    a subagent at all; this marker says what it is *for*, not what it may do.
    """
    tool.metadata = {**(tool.metadata or {}), _AUTHORITY: True}
    return tool


def is_reference_authority(tool: BaseTool) -> bool:
    return bool((tool.metadata or {}).get(_AUTHORITY))


def authority_names(tools: Sequence[BaseTool]) -> list[str]:
    """Deployment-supplied reference tools, excluding the general fallback."""
    return [
        t.name for t in tools if is_reference_authority(t) and t.name != REFERENCE_TOOL_NAME
    ]


def _corpus(documents: Sequence[RetrievedDocument]) -> dict[str, dict[str, Any]]:
    """The retrieval store as the plain mapping the checker takes.

    Flattened rather than passed as objects so ``references.py`` stays a pure
    module over data and can be used on a corpus that did not come from this
    runtime.
    """
    return {
        d.ref: {"title": d.title, "kind": d.kind, "content": d.content, **d.metadata}
        for d in documents
    }


def make_reference_toolset(fmt: ReferenceFormat = DEFAULT_FORMAT) -> ToolsetFactory:
    """A toolset holding the general reference checker for one format."""

    def factory(ctx: ToolContext) -> ToolBundle:
        async def check_references_tool(
            content: str,
            source_refs: list[str] | None = None,
            **_ignored: object,
        ) -> str:
            report = check_references(
                content,
                corpus=_corpus(ctx.documents),
                declared=source_refs,
                fmt=fmt,
            )
            if report.passed:
                # Approving here rather than in the caller is what makes the
                # publish gate meaningful: the only way to obtain an approval
                # is for this exact text to have passed.
                ctx.approve(content)
                return (
                    report.render()
                    + "\n\nThis exact content is now cleared to publish. Editing it "
                    "afterwards means running this again."
                )
            return report.render()

        tools: list[BaseTool] = [
            # A lookup: it reads nothing new and changes nothing outside the
            # request. Declaring it is what lets the subagents receive it.
            lookup(StructuredTool.from_function(
                coroutine=check_references_tool,
                name=REFERENCE_TOOL_NAME,
                description=(
                    "Check that a draft's references are attached in the required "
                    "format. Run this on your finished draft BEFORE publishing, "
                    "passing the same source_refs you intend to publish with. It "
                    "reports every unsourced section and every malformed or "
                    "unresolvable reference, and it is a parser, not an opinion -- "
                    "do not try to satisfy it by reasoning about the draft yourself. "
                    "Publishing is blocked until the exact text you intend to "
                    "publish has passed this."
                ),
                args_schema={
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": (
                                "The full draft, exactly as you intend to publish it."
                            ),
                        },
                        "source_refs": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "The source_refs you intend to pass to the publish "
                                "call. Omit while still drafting; include it once you "
                                "are about to publish, so the two can be cross-checked."
                            ),
                        },
                    },
                    "required": ["content"],
                },
            )),
        ]
        return tools

    return factory


def reference_toolsets(fmt: ReferenceFormat | None) -> Sequence[ToolsetFactory]:
    """Zero or one toolset — ``None`` turns the general check off entirely.

    A deployment whose output has no citation convention should not be handed
    a tool that will report every section as unsourced.
    """
    return () if fmt is None else (make_reference_toolset(fmt),)
