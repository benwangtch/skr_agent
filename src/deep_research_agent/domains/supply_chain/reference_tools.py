"""``format_reference`` — the supply-chain domain's reference generator.

The counterpart to checking. A checker tells the agent its citation is wrong;
this hands it the right one, built from the document the run actually loaded.
That difference is worth a tool: a model asked to reproduce a format from a
description gets it almost right, and "almost right" costs a check-fix-recheck
round trip every time.

It is marked ``reference_authority`` so the reference-checker subagent is told
this deployment has its own reference tooling, and ``lookup`` so the subagent
can receive it at all.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool, StructuredTool

from deep_research_agent.capabilities import lookup
from deep_research_agent.core.reference_tools import reference_authority
from deep_research_agent.domains.supply_chain.references import (
    RAW_REPORT_PATTERN,
    format_reference,
)
from deep_research_agent.runtime import ToolBundle, ToolContext, ToolsetFactory

__all__ = ["make_reference_format_toolset", "FORMAT_TOOL_NAME"]

FORMAT_TOOL_NAME = "format_reference"


def make_reference_format_toolset() -> ToolsetFactory:
    def factory(ctx: ToolContext) -> ToolBundle:
        def corpus() -> dict[str, dict[str, object]]:
            return {
                d.ref: {"title": d.title, "kind": d.kind, **d.metadata}
                for d in ctx.documents
            }

        async def format_reference_tool(refs: list[str], **_ignored: object) -> str:
            store = corpus()
            lines: list[str] = []
            for ref in refs or []:
                markdown = format_reference(ref, store)
                if markdown:
                    lines.append(f"{ref} -> {markdown}")
                elif RAW_REPORT_PATTERN.fullmatch(ref):
                    lines.append(
                        f"{ref} -> (raw report: do NOT put this in the page. It goes "
                        f"in source_refs on the publish call. Cite the wiki page it "
                        f"backs instead.)"
                    )
                else:
                    lines.append(
                        f"{ref} -> (cannot format: nothing loaded this run describes "
                        f"it, so there is no name to link. Read it first, or drop it.)"
                    )
            return "\n".join(lines) or "(no references given)"

        tools: list[BaseTool] = [
            lookup(reference_authority(StructuredTool.from_function(
                coroutine=format_reference_tool,
                name=FORMAT_TOOL_NAME,
                description=(
                    "Turn references into the exact markdown this report format "
                    "requires, ready to paste onto a **Sources:** line. Pass every "
                    "reference for a section at once. Use this rather than writing "
                    "the link yourself -- it takes the link text from the document "
                    "as the source returned it, and knows which references must not "
                    "appear in the page at all."
                ),
                args_schema={
                    "type": "object",
                    "properties": {
                        "refs": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Wiki page refs, article URLs, or raw report ids."
                            ),
                        }
                    },
                    "required": ["refs"],
                },
            ))),
        ]
        return tools

    return factory
