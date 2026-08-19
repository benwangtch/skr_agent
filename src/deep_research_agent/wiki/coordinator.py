"""An optional LLM synthesis layer over the wiki tools. Off by default.

The primary integration path is ``wiki/tools.py`` — authorized tools that
callers mount directly. This module exists for the case a colleague argued for
under one specific reading of "different interaction shape": retrieval quality
is genuinely hard enough (query rewriting, multi-hop reasoning across several
pages, reranking) that the wiki team wants to own it behind a single
`wiki_ask` call instead of leaving every caller's prompt to get it right on its
own.

That is a real reason to add an agent, and it composes cleanly: this is a
``DeepAgent`` built from the same ``make_wiki_toolset``, wrapped as one more
``AgentSpec`` a registry can hand out. It is not required. See
``assembly.build_mesh(with_wiki_agent=...)`` and
``docs/design/DESIGN.md`` §5.2 for why it defaults to off — direct tool
mounting already enforces authorization and returns citations; putting a model
in front adds a hop and a summarization step that can drop them.
"""

from __future__ import annotations

from dataclasses import dataclass

from deep_research_agent.protocol import AgentSpec
from deep_research_agent.runtime import DeepAgent
from deep_research_agent.wiki.authz import WikiAuthorizer
from deep_research_agent.wiki.backend import WikiBackend
from deep_research_agent.wiki.tools import make_wiki_toolset

__all__ = ["WikiCoordinator"]

ASK_DESCRIPTION = (
    "Ask a question about the internal wiki and get a synthesized answer "
    "grounded in wiki pages, with citations back to the raw weekly reports "
    "behind them. Prefer wiki_search + wiki_read_page for a direct lookup; "
    "reach for this when the question needs pulling together several pages "
    "or reasoning across them. Results are scoped to what the current user "
    "may read."
)

SYSTEM_PROMPT = """\
You answer questions about the internal wiki. Search, read what you need in
full — search previews are truncated — and synthesize a grounded answer.
Every claim needs a source; if you cannot source something, drop it. An empty
result means no record visible to this run, not that nothing happened. You
have no write access: if the user needs a page created or updated, say so
rather than attempting it.
"""


@dataclass
class WikiCoordinator:
    """Wraps the wiki toolset in a synthesis agent, for whoever opts in."""

    backend: WikiBackend
    authz: WikiAuthorizer
    model: str | None = None
    max_turns: int = 15

    def _agent(self) -> DeepAgent:
        return DeepAgent(
            name="wiki_ask",
            description=ASK_DESCRIPTION,
            system_prompt=SYSTEM_PROMPT,
            toolsets=[make_wiki_toolset(self.backend, self.authz, writable=False)],
            model=self.model,
            max_turns=self.max_turns,
            input_schema={
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "The question, in full sentences."}
                },
                "required": ["task"],
            },
        )

    def specs(self) -> list[AgentSpec]:
        return [self._agent().as_spec()]
