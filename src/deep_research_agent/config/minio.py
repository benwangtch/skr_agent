"""Object storage. Not wired into anything yet — included as the second
worked example of the pattern (alongside db.py), reproduced from the shape
already in use elsewhere in the company so this folder matches it exactly.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict

from deep_research_agent.config.base import BaseConfig

__all__ = ["Minio", "get_minio"]


class Minio(BaseConfig):
    endpoint             : str       = 'api-minio-c2.digwork-test.ftest.tsmc.com'
    access_key           : str       = 'cpoml-object-storage-admin'
    secret_key           : SecretStr = SecretStr('')
    bucket_name          : str       = 'cpoml-object-storage'

    retry_number         : int       = 7
    pool_max_size        : int       = 10
    timeout              : int       = 600  # secs

    model_config = SettingsConfigDict(env_prefix = 'minio_')


@lru_cache(maxsize=1)
def get_minio() -> Minio:
    return Minio()
