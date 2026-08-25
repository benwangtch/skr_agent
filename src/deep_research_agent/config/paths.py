"""Where the agent's on-disk data lives.

The entry points used to sit in ``examples/`` and resolve everything with
``Path(__file__).parent.parent`` — fine while they were scripts in the repo,
wrong the moment they moved into the package, because an installed wheel in
``site-packages`` has no repo above it.

So the roots become configuration like everything else:

``project_root``
    Where skills are looked up (``<root>/skills/``). Left empty, it is found
    by walking up from this file for a checkout marker, and falls back to the
    working directory — so a source checkout needs no configuration and a
    container can point at wherever it mounted its skills.

``fixtures``
    The demo data behind the supply-chain domain. A real deployment swaps the
    domain's sources for real services and never reads this.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import SettingsConfigDict

from deep_research_agent.config.base import BaseConfig

__all__ = ["Paths", "get_paths"]


class Paths(BaseConfig):
    model_config = SettingsConfigDict(env_prefix = 'paths_')

    project_root        : str               = ''
    """Root for skill discovery. Empty means "work it out"."""

    fixtures            : str               = ''
    """Demo data directory. Empty means ``<project_root>/fixtures``."""

    def resolved_project_root(self) -> Path:
        if self.project_root:
            return Path(self.project_root).expanduser().resolve()
        return _checkout_root() or Path.cwd()

    def resolved_fixtures(self) -> Path:
        if self.fixtures:
            return Path(self.fixtures).expanduser().resolve()
        return self.resolved_project_root() / "fixtures"


def _checkout_root() -> Path | None:
    """The repo root, when running from a source checkout.

    Both markers are required: ``pyproject.toml`` alone would happily match a
    parent project that merely vendored this one, and ``src/`` alone is far
    too common a directory name.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "src").is_dir():
            return parent
    return None


@lru_cache(maxsize=1)
def get_paths() -> Paths:
    return Paths()
