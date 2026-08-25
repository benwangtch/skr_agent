"""Wiring. The only module that knows about every other module.

Keeping composition in one place is what lets a data source be fixtures today
and a real service tomorrow without any agent noticing: the swap happens here,
and nothing above changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from deep_research_agent.core.agent import build_research_agent
from deep_research_agent.core.domain import ResearchDomain
from deep_research_agent.domains import supply_chain
from deep_research_agent.mesh import AgentRegistry
from deep_research_agent.runtime import DeepAgent, ToolsetFactory
from deep_research_agent.wiki import InMemoryWikiBackend, WikiAuthorizer, WikiBackend, WikiCoordinator

__all__ = ["Mesh", "build_mesh", "DomainFactory"]

DomainFactory = Callable[[Path], ResearchDomain]
"""Builds a domain against a data root. Taken rather than a built
``ResearchDomain`` so ``build_mesh`` keeps one place that knows where the data
lives — otherwise every caller would pass the fixtures path twice."""


@dataclass
class Mesh:
    registry: AgentRegistry
    backend: WikiBackend
    authz: WikiAuthorizer
    agent: DeepAgent
    """The deep research agent itself."""
    coordinator: WikiCoordinator | None = field(default=None)
    """Only present when ``with_wiki_agent=True``. See ``build_mesh``."""


def build_mesh(
    *,
    fixtures: str | Path,
    project_root: str | Path,
    model: str | None = None,
    domain: DomainFactory | None = supply_chain.from_fixtures,
    with_wiki_agent: bool = False,
    extra_toolsets: Sequence[ToolsetFactory] = (),
) -> Mesh:
    """Build the research agent and its data sources against fixture data.

    ``domain`` decides what the agent knows going in. It defaults to the
    supply-chain pack because that is this repo's scheduled job — a known task
    running every week, which is exactly the case a domain exists for.

    Pass ``domain=None`` for the **general agent**: no subject-matter
    briefing, no domain sources, no specialists — just the research loop, the
    wiki, and any MCP servers. That is the face-to-user configuration, where
    someone types a question nobody anticipated and there is no domain to
    select in advance. See ``cli/ask.py``.

    ``with_wiki_agent`` controls an open question in the design: by default the
    wiki is a set of authorized tools that callers mount directly, which is one
    fewer model hop and loses nothing. Set it to ``True`` to also register the
    LLM-backed ``wiki_ask`` — worth doing only if the wiki team takes ownership
    of retrieval quality (query rewriting, reranking, multi-hop) behind it.

    ``extra_toolsets`` mounts additional data sources on the agent — this is
    how MCP servers get in (see ``deep_research_agent.mcp``). It is a parameter
    rather than something this function loads itself because MCP discovery is
    async and this is not: the caller loads once at startup and passes the
    result down. ``cli/report.py`` and ``serving/service.py`` both do.

    Swap ``InMemoryWikiBackend`` for the real wiki client and the domain's
    fixture sources for real services; nothing else here changes.
    """
    fixtures = Path(fixtures)

    backend = InMemoryWikiBackend.from_fixtures(fixtures)
    authz = WikiAuthorizer()

    agent = build_research_agent(
        wiki_backend=backend,
        wiki_authz=authz,
        project_root=project_root,
        domain=domain(fixtures) if domain is not None else None,
        model=model,
        extra_toolsets=extra_toolsets,
    )

    registry = AgentRegistry()
    registry.register(agent.as_spec())

    coordinator: WikiCoordinator | None = None
    if with_wiki_agent:
        coordinator = WikiCoordinator(backend=backend, authz=authz)
        for spec in coordinator.specs():
            registry.register(spec)

    return Mesh(
        registry=registry,
        backend=backend,
        authz=authz,
        agent=agent,
        coordinator=coordinator,
    )
