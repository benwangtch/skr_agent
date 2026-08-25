"""Langfuse tracing — where a run's step-by-step trace gets sent.

Off unless both keys are set. With nothing configured the agent behaves
exactly as before and never contacts anything, which matters because tracing
is the kind of dependency that must never be able to break a research run.

``base_url`` defaults to the internal instance, so a deployment normally only
supplies the two keys.

Note the class name shadows the SDK's own ``Langfuse`` client class. The SDK
is imported function-locally (see ``observability.py``) rather than at module
scope, both to keep that unambiguous and so importing config does not drag in
the tracing stack for a process that will never use it.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict

from deep_research_agent.config.base import BaseConfig

log = logging.getLogger(__name__)

__all__ = ["Langfuse", "get_langfuse"]


class Langfuse(BaseConfig):
    model_config = SettingsConfigDict(env_prefix = 'langfuse_')

    secret_key          : SecretStr         = SecretStr('')
    """``sk-lf-...``. Server-side credential — never sent to a browser."""

    public_key          : str               = ''
    """``pk-lf-...``."""

    base_url            : str               = 'http://langfuse-ai4bi.cpoap-dev.dev.tsmc.com'
    """The Langfuse host. Defaults to the internal instance."""

    environment         : str               = ''
    """Optional label (``dev`` / ``prod`` / a branch name) so traces from
    different deployments stay separable in one Langfuse project. Empty means
    Langfuse's own default."""

    def configured(self) -> bool:
        """Both keys present. One alone is a misconfiguration, not a partial
        setup, so it is reported rather than half-enabled."""
        return bool(self.secret_key.get_secret_value() and self.public_key)

    def problems(self) -> list[str]:
        """Why tracing is off, when it looks like someone meant it to be on."""
        secret, public = self.secret_key.get_secret_value(), self.public_key
        if secret and not public:
            return ["LANGFUSE_SECRET_KEY is set but LANGFUSE_PUBLIC_KEY is not"]
        if public and not secret:
            return ["LANGFUSE_PUBLIC_KEY is set but LANGFUSE_SECRET_KEY is not"]
        return []


@lru_cache(maxsize=1)
def get_langfuse() -> Langfuse:
    return Langfuse()
