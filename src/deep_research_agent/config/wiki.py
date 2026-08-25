"""Where the wiki lives, for building links a reader can follow.

Only the base URL for now, because only ``wiki/routes.py`` needs it. The
backend itself is still injected (``assembly.py``) rather than configured —
this is about rendering a reference, not about reaching the service.

The default is deliberately an obvious placeholder rather than something that
looks real: a link to a host nobody serves should be recognisable as unset at
a glance, not discovered by a reader clicking it.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import SettingsConfigDict

from deep_research_agent.config.base import BaseConfig

__all__ = ["Wiki", "get_wiki"]


class Wiki(BaseConfig):
    model_config = SettingsConfigDict(env_prefix = 'wiki_')

    base_url            : str               = 'https://wiki.internal.example/wiki'
    """Root for page links. A trailing slash is stripped so callers can join
    with ``/`` without producing ``//``."""

    def resolved_base_url(self) -> str:
        return self.base_url.rstrip("/")


@lru_cache(maxsize=1)
def get_wiki() -> Wiki:
    return Wiki()
