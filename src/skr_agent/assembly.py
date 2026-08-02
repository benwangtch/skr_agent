"""Wiring. The only module that knows about every other module.

Keeping composition in one place is what lets the wiki coordinator be a mock
today and a real service tomorrow without any agent noticing: the swap happens
here, and nothing above changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .mesh import AgentRegistry
from .report import FixtureBom, FixtureNewsFeed, build_report_agent
from .runtime import DeepAgent
from .wiki import InMemoryWikiBackend, WikiAuthorizer, WikiCoordinator

__all__ = ["Mesh", "build_mesh"]


@dataclass
class Mesh:
    registry: AgentRegistry
    wiki: WikiCoordinator
    report_agent: DeepAgent


def build_mesh(
    *,
    fixtures: str | Path,
    project_root: str | Path,
    model: str | None = None,
    effort: str = "high",
) -> Mesh:
    """Build the whole mesh against fixture data.

    Swap ``InMemoryWikiBackend`` for the real wiki client and ``FixtureNewsFeed``
    for real search; nothing else here changes.
    """
    fixtures = Path(fixtures)

    wiki = WikiCoordinator(
        backend=InMemoryWikiBackend.from_fixtures(fixtures),
        authz=WikiAuthorizer(),
    )
    report_agent = build_report_agent(
        bom=FixtureBom.from_fixtures(fixtures),
        news=FixtureNewsFeed.from_fixtures(fixtures),
        wiki=wiki,
        project_root=project_root,
        model=model,
        effort=effort,
    )

    registry = AgentRegistry()
    for spec in wiki.specs():
        registry.register(spec)
    registry.register(report_agent.as_spec())

    return Mesh(registry=registry, wiki=wiki, report_agent=report_agent)
