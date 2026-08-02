"""How copilot sees the mesh.

Copilot is the one agent users talk to. It owns conversation, dashboard
context, and routing — and deliberately owns no feature logic. Every feature it
fronts appears as one tool per capability, produced by the same
``agents_as_toolserver`` call, so adding the next feature is a registry entry
rather than a change to copilot.

Two consequences worth being explicit about:

Copilot never learns the wiki's namespace rules. It forwards a principal; the
wiki coordinator decides. When a new division appears, copilot does not change.

Copilot calls the report agent directly rather than through the wiki
coordinator. Report generation needs external search and multi-company
fan-out, which are not wiki concerns — routing it through the wiki would make
the coordinator a proxy for capabilities it does not own.
"""

from __future__ import annotations

from .mesh import AgentRegistry, agents_as_toolserver
from .runtime import DeepAgent, ToolBundle, ToolContext

__all__ = ["build_copilot", "COPILOT_SYSTEM_PROMPT"]


COPILOT_SYSTEM_PROMPT = """\
You are the copilot behind the dashboard. Users from every division ask you
about the data on screen and about anything the platform knows.

Route rather than improvise. Each feature is available to you as a tool:

- `wiki_ask` for anything about internal documentation, weekly reports, team
  history, ownership, or prior incidents. Prefer it over answering from
  memory — it returns citations, and you should pass them through.
- `wiki_report` for supply-chain questions that need external research: BOM
  sweeps, "what happened with supplier X", incident roll-ups. It is slow and
  expensive, so use it when the question genuinely needs external sources plus
  internal cross-referencing, not for a plain internal lookup.

A tool refusal is an answer, not an error. If the wiki declines because the
user cannot read a namespace, tell them that plainly and suggest who can help;
do not try another phrasing to get around it.

Keep replies short and lead with the answer. Include the sources the tools
returned. When a tool found nothing, say so — do not fill the gap with a
plausible guess.
"""


def build_copilot(
    registry: AgentRegistry,
    *,
    model: str | None = None,
    effort: str = "medium",
    max_turns: int = 20,
) -> DeepAgent:
    """Copilot as a thin agent over the registry.

    Runs at lower effort than the report agent on purpose: its job is to pick
    the right tool and relay the result, which is not the part of the system
    that needs deep reasoning.
    """

    def feature_tools(ctx: ToolContext) -> ToolBundle:
        return agents_as_toolserver(
            registry.list(),
            principal=ctx.principal,
            parent="copilot",
            server_name="features",
            on_response=lambda r: [ctx.cite(c) for c in r.citations],
        )

    return DeepAgent(
        name="copilot",
        description="User-facing assistant for the dashboard.",
        system_prompt=COPILOT_SYSTEM_PROMPT,
        toolsets=[feature_tools],
        model=model,
        effort=effort,
        max_turns=max_turns,
    )
