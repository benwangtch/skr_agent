"""Which model backend the Claude Agent SDK's CLI subprocess talks to.

The Agent SDK speaks the Anthropic Messages wire protocol — it is not a
generic "any LLM" client. Pointing it at a non-Anthropic model therefore
requires an endpoint that speaks that protocol back. OpenRouter serves
exactly that at ``/api`` (distinct from its OpenAI-compatible ``/api/v1``)
and translates it to whatever model you name — that's what makes
``provider="openrouter"`` a legitimate stand-in for "our internal host," not
just a coincidence of API shape. A real internal host that speaks the same
protocol slots in as ``provider="custom"`` with no code change; one that only
speaks OpenAI's format needs a translating proxy in front of it (see
``docs/design/01-config-and-serving.md``) — this config can't paper over a
different wire protocol, only point at where one is being spoken.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import SettingsConfigDict

from .base import BaseConfig

log = logging.getLogger(__name__)

__all__ = ["LLM", "get_llm"]

Provider = Literal["anthropic", "openrouter", "custom"]

# Sensible defaults per provider, so setting only llm_provider still works.
# llm_base_url / llm_model always override these when set.
_BASE_URLS   : dict[str, str | None] = {
    "anthropic"  : None,  # None = let the SDK use its own default (api.anthropic.com)
    "openrouter" : "https://openrouter.ai/api",
    "custom"     : None,  # must be supplied via llm_base_url
}
_DEFAULT_MODELS : dict[str, str] = {
    "anthropic"  : "claude-opus-5",
    "openrouter" : "qwen/qwen3.5-plus-02-15",
    "custom"     : "",
}


class LLM(BaseConfig):
    model_config = SettingsConfigDict(env_prefix = 'llm_')

    provider            : Provider          = 'openrouter'
    """Which service to talk to. Defaults to openrouter to simulate an
    internal host without needing real Anthropic credentials."""

    base_url            : str | None        = None
    """Anthropic-Messages-compatible endpoint. None picks the provider
    default (_BASE_URLS); ignored for provider="anthropic" unless set
    explicitly, since that provider's own default is usually correct."""

    api_key             : SecretStr         = SecretStr('')
    """Credential for the endpoint. For provider="openrouter" or "custom"
    this is sent as ANTHROPIC_AUTH_TOKEN, never ANTHROPIC_API_KEY — see
    to_cli_env() for why that distinction is load-bearing, not stylistic."""

    model                : str | None       = None
    """None picks the provider default (_DEFAULT_MODELS). Per-agent code can
    still pass its own model explicitly; this is only the fallback."""

    effort               : Literal['low', 'medium', 'high', 'xhigh', 'max'] = 'high'

    timeout              : int              = 600  # secs, agentic turns run long
    retry_number         : int              = 3

    @model_validator(mode='after')
    def _warn_missing_credential(self) -> 'LLM':
        # Not raising: a dev running a free OpenRouter model, or an
        # environment relying on an ambient ANTHROPIC_API_KEY for
        # provider="anthropic", both legitimately have nothing to set here.
        if self.provider != 'anthropic' and not self.api_key.get_secret_value():
            log.warning(
                "llm_provider=%s but llm_api_key is empty; requests to %s will "
                "likely be rejected",
                self.provider, self.resolved_base_url(),
            )
        return self

    def resolved_base_url(self) -> str | None:
        return self.base_url or _BASE_URLS.get(self.provider)

    def resolved_model(self) -> str:
        return self.model or _DEFAULT_MODELS.get(self.provider, '')

    def to_cli_env(self) -> dict[str, str]:
        """The env dict to hand to ``ClaudeAgentOptions(env=...)``.

        Only for ``provider="anthropic"`` does the real ``ANTHROPIC_API_KEY``
        make sense — that's the credential the Anthropic API itself checks.
        For any Anthropic-Messages-*compatible* endpoint (OpenRouter, a
        company-internal proxy speaking the same protocol), the credential
        goes on ``ANTHROPIC_AUTH_TOKEN`` and ``ANTHROPIC_API_KEY`` is set to
        the empty string explicitly — not omitted. An *unset*
        ``ANTHROPIC_API_KEY`` still falls back to a locally logged-in
        ``claude`` session's own credentials, silently sending the request to
        the real Anthropic API instead of the endpoint you configured. An
        empty string forecloses that fallback.
        """
        key      = self.api_key.get_secret_value()
        base_url = self.resolved_base_url()

        if self.provider == 'anthropic':
            env: dict[str, str] = {}
            if base_url:
                env['ANTHROPIC_BASE_URL'] = base_url
            if key:
                env['ANTHROPIC_API_KEY'] = key
            return env

        # openrouter / custom: Anthropic-compatible endpoint, non-Anthropic auth.
        env = {'ANTHROPIC_API_KEY': ''}
        if base_url:
            env['ANTHROPIC_BASE_URL'] = base_url
        if key:
            env['ANTHROPIC_AUTH_TOKEN'] = key
        return env


@lru_cache(maxsize=1)
def get_llm() -> LLM:
    return LLM()
