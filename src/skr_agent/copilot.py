"""A thin routing agent over the mesh.

Not the focus of this repo — skr agent (``report/agent.py``) is. This exists so
there is a worked example of calling skr agent as a tool from another agent in
the same process, and so a dashboard has something conversational to talk to.
It owns conversation and routing, and deliberately owns no feature logic.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from .mesh import AgentRegistry, agents_as_tools
from .runtime import DeepAgent, ToolBundle, ToolContext
from .wiki.authz import WikiAuthorizer
from .wiki.backend import WikiBackend
from .wiki.tools import make_wiki_toolset

__all__ = ["build_copilot", "COPILOT_SYSTEM_PROMPT"]


COPILOT_SYSTEM_PROMPT = """\
You are the copilot behind the dashboard. Users from every division ask you
about the data on screen and about anything the platform knows.

Route rather than improvise.

- `wiki_search` then `wiki_read_page` for anything about internal
  documentation, weekly reports, team history, ownership, or prior incidents.
  Prefer them over answering from memory, and pass the source references
  through to the user — a page cites the raw weekly reports behind it, and
  that is usually what they actually want.
- `skr_agent` for questions that need real research: BOM sweeps, "what
  happened with supplier X", incident roll-ups. It is slow and expensive, so
  reach for it when the question genuinely needs external sources plus
  internal cross-referencing, not for a plain internal lookup.

Results are scoped to what this user may read, and you cannot widen that. A
refusal is an answer, not an error: say plainly that they do not have access
and suggest who does. Do not rephrase and try again to get around it, and do
not speculate about what the inaccessible content might say.

An empty search result means "no record you can see" — say that, rather than
"there is no such record".

Keep replies short and lead with the answer. Include the sources the tools
returned. When a tool found nothing, say so rather than filling the gap with a
plausible guess.
"""


def build_copilot(
    registry: AgentRegistry,
    *,
    wiki_backend: WikiBackend,
    wiki_authz: WikiAuthorizer,
    model: str | None = None,
    max_turns: int = 20,
    wiki_writable: bool = False,
) -> DeepAgent:
    """Copilot as a thin agent over the wiki toolset plus registered agents.

    ``wiki_writable`` is off by default. Letting the conversational surface
    write is a separate product decision from letting it read, and should be
    made deliberately.
    """

    def feature_tools(ctx: ToolContext) -> ToolBundle:
        tools: list[BaseTool] = agents_as_tools(
            registry.list(),
            principal=ctx.principal,
            parent="copilot",
            on_response=lambda r: [ctx.cite(c) for c in r.citations],
        )
        return tools

    return DeepAgent(
        name="copilot",
        description="User-facing assistant for the dashboard.",
        system_prompt=COPILOT_SYSTEM_PROMPT,
        toolsets=[
            make_wiki_toolset(wiki_backend, wiki_authz, writable=wiki_writable),
            feature_tools,
        ],
        model=model,
        max_turns=max_turns,
    )
