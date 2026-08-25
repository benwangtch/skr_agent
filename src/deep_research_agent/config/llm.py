"""Which chat model the agent runs on.

The agent runs on LangChain/LangGraph (``deepagents``), so the model is an
ordinary LangChain ``BaseChatModel`` and the only thing that has to match is
the *provider SDK*, not a specific wire protocol. That is a real
simplification over the previous Claude-Agent-SDK setup, which spoke the
Anthropic Messages protocol exclusively and therefore needed the endpoint to
speak it back.

Practically: almost every internal LLM gateway exposes an OpenAI-compatible
``/v1/chat/completions``, and that is what ``provider="custom"`` points at.
OpenRouter is the same shape (``https://openrouter.ai/api/v1``), which is why
it stands in for an internal host here without any protocol translation.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import TYPE_CHECKING, Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import SettingsConfigDict

from deep_research_agent.config.base import BaseConfig

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

log = logging.getLogger(__name__)

__all__ = ["LLM", "get_llm"]

Provider = Literal["openrouter", "openai", "anthropic", "custom"]

# Defaults per provider, so setting only llm_provider still works.
# llm_base_url / llm_model always override these when set.
_BASE_URLS      : dict[str, str | None] = {
    "openrouter" : "https://openrouter.ai/api/v1",
    "openai"     : None,  # None = let the SDK use its own default
    "anthropic"  : None,
    "custom"     : None,  # must be supplied via llm_base_url
}
_DEFAULT_MODELS : dict[str, str] = {
    "openrouter" : "qwen/qwen3.5-plus-02-15",
    "openai"     : "gpt-5.5",
    "anthropic"  : "claude-opus-5",
    "custom"     : "",
}


class LLM(BaseConfig):
    model_config = SettingsConfigDict(env_prefix = 'llm_')

    provider            : Provider          = 'openrouter'
    """Which service to talk to. Defaults to openrouter to simulate an
    internal host without needing real vendor credentials."""

    base_url            : str | None        = None
    """Endpoint override. None picks the provider default (_BASE_URLS)."""

    api_key             : SecretStr         = SecretStr('')

    model               : str | None        = None
    """None picks the provider default (_DEFAULT_MODELS). Per-agent code can
    still pass its own model explicitly; this is only the fallback."""

    temperature         : float             = 0.0
    """Research work wants reproducibility over variety."""

    timeout             : int               = 600  # secs, agentic turns run long
    retry_number        : int               = 3

    @model_validator(mode='after')
    def _warn_missing_credential(self) -> 'LLM':
        # Not raising: a local vLLM/Ollama gateway may legitimately need no key.
        if not self.api_key.get_secret_value():
            log.warning(
                "llm_provider=%s but llm_api_key is empty; requests to %s will "
                "likely be rejected",
                self.provider, self.resolved_base_url() or '(provider default)',
            )
        return self

    def problems(self) -> list[str]:
        """Why a run would fail, in terms of *this repo's* variable names.

        Without this the first sign of a missing key is a provider traceback
        ending in "set the OPENAI_API_KEY environment variable" — advice that
        is actively wrong here, since the key is ``LLM_API_KEY`` and may be
        going to a completely different host.

        ``custom`` is exempt from the key check on purpose: a local vLLM or
        Ollama gateway legitimately needs no credential, and refusing to start
        against one would be the same mistake in the other direction.
        """
        found: list[str] = []
        if self.provider != 'custom' and not self.api_key.get_secret_value():
            found.append(
                f"LLM_API_KEY is empty and LLM_PROVIDER={self.provider} requires a key"
            )
        if self.provider == 'custom':
            if not self.resolved_model():
                found.append("LLM_PROVIDER=custom needs LLM_MODEL (no default for an unknown host)")
            if not self.resolved_base_url():
                found.append(
                    "LLM_PROVIDER=custom needs LLM_BASE_URL -- without it requests "
                    "go to the OpenAI SDK's own default host, not your gateway"
                )
        return found

    def resolved_base_url(self) -> str | None:
        return self.base_url or _BASE_URLS.get(self.provider)

    def resolved_model(self) -> str:
        return self.model or _DEFAULT_MODELS.get(self.provider, '')

    def build_chat_model(self, *, model: str | None = None) -> 'BaseChatModel':
        """The ``BaseChatModel`` handed to ``create_deep_agent(model=...)``.

        Returns a constructed instance rather than a ``"provider:model"``
        string because a string cannot carry ``base_url`` — and pointing at a
        different endpoint is the entire reason this config exists. Imports
        are function-local so that importing the config package does not drag
        in every provider SDK.
        """
        name = model or self.resolved_model()
        if not name:
            raise ValueError(
                f"llm_provider={self.provider!r} has no default model; set llm_model"
            )
        key      = self.api_key.get_secret_value()
        base_url = self.resolved_base_url()

        if self.provider == 'anthropic':
            from langchain_anthropic import ChatAnthropic

            return ChatAnthropic(
                model_name      = name,
                api_key         = SecretStr(key) if key else None,
                base_url        = base_url,
                temperature     = self.temperature,
                timeout         = float(self.timeout),
                max_retries     = self.retry_number,
                stop            = None,
            )

        # openrouter / openai / custom all speak the OpenAI chat-completions API.
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model           = name,
            api_key         = SecretStr(key) if key else None,
            base_url        = base_url,
            temperature     = self.temperature,
            timeout         = float(self.timeout),
            max_retries     = self.retry_number,
        )


@lru_cache(maxsize=1)
def get_llm() -> LLM:
    return LLM()
