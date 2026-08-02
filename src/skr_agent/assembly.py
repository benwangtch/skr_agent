"""Wiring. The only module that knows about every other module.

Keeping composition in one place is what lets the wiki backend be fixtures
today and a real service tomorrow without any agent noticing: the swap happens
here, and nothing above changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .mesh import AgentRegistry
from .report import FixtureBom, FixtureNewsFeed, build_report_agent
from .runtime import DeepAgent
from .wiki import InMemoryWikiBackend, WikiAuthorizer, WikiBackend, WikiCoordinator

__all__ = ["Mesh", "build_mesh"]


@dataclass
class Mesh:
    registry: AgentRegistry
    backend: WikiBackend
    authz: WikiAuthorizer
    report_agent: DeepAgent
    coordinator: WikiCoordinator | None = field(default=None)
    """Only present when ``with_wiki_agent=True``. See ``build_mesh``."""


def build_mesh(
    *,
    fixtures: str | Path,
    project_root: str | Path,
    model: str | None = None,
    effort: str = "high",
    with_wiki_agent: bool = False,
) -> Mesh:
    """Build the mesh against fixture data.

    ``with_wiki_agent`` controls the open question in the design: by default the
    wiki is a set of authorized tools that callers mount directly, which is one
    fewer model hop and loses nothing. Set it to ``True`` to also register the
    LLM-backed ``wiki_ask`` — worth doing only if the wiki team takes ownership
    of retrieval quality (query rewriting, reranking, multi-hop) behind it.

    Swap ``InMemoryWikiBackend`` for the real wiki client and
    ``FixtureNewsFeed`` for real search; nothing else here changes.
    """
    fixtures = Path(fixtures)

    backend = InMemoryWikiBackend.from_fixtures(fixtures)
    authz = WikiAuthorizer()

    report_agent = build_report_agent(
        bom=FixtureBom.from_fixtures(fixtures),
        news=FixtureNewsFeed.from_fixtures(fixtures),
        wiki_backend=backend,
        wiki_authz=authz,
        project_root=project_root,
        model=model,
        effort=effort,
    )

    registry = AgentRegistry()
    registry.register(report_agent.as_spec())

    coordinator: WikiCoordinator | None = None
    if with_wiki_agent:
        coordinator = WikiCoordinator(backend=backend, authz=authz)
        for spec in coordinator.specs():
            registry.register(spec)

    return Mesh(
        registry=registry,
        backend=backend,
        authz=authz,
        report_agent=report_agent,
        coordinator=coordinator,
    )
