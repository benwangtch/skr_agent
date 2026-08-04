"""Env-driven config for every IO service this codebase talks to.

One module per service, one ``env_prefix`` per module, all inheriting
``BaseConfig`` for the shared ``.env`` loading behavior. To add a new IO
service: copy ``db.py``, rename the class and prefix, add the fields, add a
cached getter, export it here.

Nothing outside this package should call ``os.environ`` for configuration —
that's what keeps "swap an IO service" down to "set env vars" when this code
moves to the real company environment.
"""

from __future__ import annotations

from .base import BaseConfig
from .db import DB, get_db
from .llm import LLM, get_llm
from .minio import Minio, get_minio

__all__ = [
    "BaseConfig",
    "LLM",
    "get_llm",
    "DB",
    "get_db",
    "Minio",
    "get_minio",
    "reset_settings_cache",
]


def reset_settings_cache() -> None:
    """For tests: forget cached settings so a changed env is picked up.

    Production code never calls this — each getter is a process-lifetime
    singleton, which is what lets call sites read it on every request without
    re-parsing the environment each time.
    """
    get_llm.cache_clear()
    get_db.cache_clear()
    get_minio.cache_clear()
