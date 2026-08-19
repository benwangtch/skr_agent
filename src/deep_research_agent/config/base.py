"""Shared settings base. Every IO-service config in this folder inherits it.

One file per service (``llm.py``, ``db.py``, ...), each defining a class named
after the service with its own ``env_prefix``. That's the whole convention —
copy an existing file's shape when a new IO service shows up, don't invent a
new pattern.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["BaseConfig"]


class BaseConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file            = '.env',
        env_file_encoding   = 'utf-8',
        extra               = 'ignore',
    )
