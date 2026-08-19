"""Placeholder — nothing in this codebase talks to a database yet.

Kept here so the pattern (one config class per IO service, one env prefix, a
cached singleton getter) is established before it's needed, rather than
invented ad hoc under deadline pressure the day a database shows up. Delete
this docstring's caveat once something actually constructs a connection from
``get_db()``.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict

from deep_research_agent.config.base import BaseConfig

__all__ = ["DB", "get_db"]


class DB(BaseConfig):
    model_config = SettingsConfigDict(env_prefix = 'db_')

    dsn                  : SecretStr = SecretStr('')
    pool_min             : int       = 1
    pool_max             : int       = 10
    timeout              : int       = 30  # secs


@lru_cache(maxsize=1)
def get_db() -> DB:
    return DB()
