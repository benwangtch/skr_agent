"""Where skills are found, and which ones the agent loads.

A skill is a rubric the agent must follow on every run (report format, severity
scale, house style). The built-in one lives in this repo, but a skill you
*maintain* usually does not — it lives in its own repo, or a shared directory
several agents read from. Copying it in works right up until your copy and the
original disagree, and then nobody notices for a month.

So skills resolve across a search path, newest-wins, in this order:

1. every directory in ``SKILLS_PATH`` (colon-separated, leftmost first)
2. ``<project_root>/skills`` — skills that ship with this repo
3. ``<project_root>/.claude/skills`` — legacy location, still honoured

and ``SKILLS_ENABLED`` adds names to what the agent loads without editing
``DEFAULT_SKILLS``. Both default to empty, so with nothing set the behavior is
exactly what it was: one built-in skill, loaded from this repo.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from pydantic_settings import SettingsConfigDict

from deep_research_agent.config.base import BaseConfig

log = logging.getLogger(__name__)

__all__ = ["Skills", "get_skills"]


class Skills(BaseConfig):
    model_config = SettingsConfigDict(env_prefix = 'skills_')

    path                : str               = ''
    """Extra directories to search for skills, colon-separated like ``PATH``.

    Each entry is a directory that *contains* skill directories, so
    ``/opt/skills`` holds ``/opt/skills/house-style/SKILL.md``. Searched
    before the repo's own, so you can override a built-in skill without
    touching it."""

    enabled             : str               = ''
    """Extra skill names to load, comma-separated. Added to the agent's
    defaults rather than replacing them."""

    def search_paths(self) -> list[Path]:
        return [Path(p).expanduser() for p in self.path.split(":") if p.strip()]

    def enabled_names(self) -> list[str]:
        return [n.strip() for n in self.enabled.split(",") if n.strip()]


@lru_cache(maxsize=1)
def get_skills() -> Skills:
    return Skills()
