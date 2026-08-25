"""The reference check as a tool, bound to one request's retrieval record.

Why this is a toolset factory rather than a plain function: the grounding
check needs to know what *this run* actually read, and that lives in
``ToolContext.citations``. Closing over the context is the same mechanism the
authorized tools use for the principal — the model cannot pass in its own
idea of what it retrieved, because there is no parameter for it.

That the citation record is shared between the lead and its subagents is what
makes the check work on a delegated sweep: an investigator's ``fetch_article``
lands in the same list, so a reference the lead inherited from a finding file
is grounded, while one it invented while drafting is not.
"""

from __future__ import annotations

from typing import Sequence

from langchain_core.tools import BaseTool, StructuredTool

from deep_research_agent.capabilities import lookup
from deep_research_agent.core.references import (
    DEFAULT_FORMAT,
    ReferenceFormat,
    check_references,
)
from deep_research_agent.runtime import ToolBundle, ToolContext, ToolsetFactory

__all__ = ["make_reference_toolset", "REFERENCE_TOOL_NAME"]

REFERENCE_TOOL_NAME = "check_references"


def make_reference_toolset(fmt: ReferenceFormat = DEFAULT_FORMAT) -> ToolsetFactory:
    """A toolset holding the reference checker for one reference format."""

    def factory(ctx: ToolContext) -> ToolBundle:
        async def check_references_tool(
            content: str,
            source_refs: list[str] | None = None,
            **_ignored: object,
        ) -> str:
            report = check_references(
                content,
                retrieved=[c.ref for c in ctx.citations],
                declared=source_refs,
                fmt=fmt,
            )
            return report.render()

        tools: list[BaseTool] = [
            # A lookup: it reads nothing new and changes nothing. Declaring it
            # is what lets the reference-checker subagent receive it.
            lookup(StructuredTool.from_function(
                coroutine=check_references_tool,
                name=REFERENCE_TOOL_NAME,
                description=(
                    "Check that a draft's references are attached in the required "
                    "format and are real. Run this on your finished draft BEFORE "
                    "publishing, passing the same source_refs you intend to publish "
                    "with. It reports every unsourced section, every malformed "
                    "reference, and -- most importantly -- every reference that was "
                    "never actually retrieved during this run, which is how a "
                    "citation written from memory gets caught. This is a mechanical "
                    "check and it is exact; do not try to satisfy it by reasoning "
                    "about the draft yourself."
                ),
                args_schema={
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "The full draft, as you would publish it.",
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
    """Zero or one toolset — ``None`` turns the check off entirely.

    A deployment whose output has no citation convention at all should not be
    handed a tool that will report every section as unsourced.
    """
    return () if fmt is None else (make_reference_toolset(fmt),)
