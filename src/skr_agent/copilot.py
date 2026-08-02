"""How copilot sees the mesh.

Copilot is the one agent users talk to. It owns conversation, dashboard
context, and routing — and deliberately owns no feature logic.

Two different kinds of thing hang off it, and the difference is the design:

* **The wiki is a toolset**, mounted directly. Reading a wiki page is one hop
  with a known shape; putting an LLM in front of it would add latency and a
  summarization step that loses citations, while enforcing nothing the tool
  layer does not already enforce. Copilot still never learns the namespace
  rules — those live in the wiki service's authorizer, next to the data.
* **The report agent is an agent**, mounted as a tool. It plans, fans out to
  subagents, and takes minutes. That is worth a process boundary.

Copilot calls the report agent directly rather than through the wiki, because
external search and multi-company fan-out are not wiki concerns; routing them
through the wiki would make it a proxy for capabilities it does not own.
"""

from __future__ import annotations

from .mesh import AgentRegistry, agents_as_toolserver
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
- `wiki_report` for supply-chain questions that need external research: BOM
  sweeps, "what happened with supplier X", incident roll-ups. It is slow and
  expensive, so reach for it when the question genuinely needs external
  sources plus internal cross-referencing, not for a plain internal lookup.

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
    effort: str = "medium",
    max_turns: int = 20,
    wiki_writable: bool = False,
) -> DeepAgent:
    """Copilot as a thin agent over the wiki toolset plus registered agents.

    Runs at lower effort than the report agent on purpose: picking the right
    tool and relaying the result is not the part of the system that needs deep
    reasoning.

    ``wiki_writable`` is off by default. Letting the conversational surface
    write to the wiki is a separate product decision from letting it read, and
    should be made deliberately.
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
        toolsets=[
            make_wiki_toolset(wiki_backend, wiki_authz, writable=wiki_writable),
            feature_tools,
        ],
        model=model,
        effort=effort,
        max_turns=max_turns,
    )
