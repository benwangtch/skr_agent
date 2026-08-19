"""The config layer: env-var overrides, provider defaults, and the chat model
each provider resolves to.

Each test clears LLM_/DB_/MINIO_* env vars first so the process's real
environment (which may have provider keys set for other purposes) can't leak
into an assertion. Constructing a chat model performs no network I/O, so these
stay credential-free.
"""

from __future__ import annotations

import os

import pytest

from deep_research_agent.config import LLM, get_llm, reset_settings_cache

ENV_PREFIXES = ("LLM_", "DB_", "MINIO_")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.upper().startswith(ENV_PREFIXES):
            monkeypatch.delenv(key, raising=False)
    reset_settings_cache()
    yield
    reset_settings_cache()


class TestDefaults:
    def test_default_provider_is_openrouter(self):
        assert LLM().provider == "openrouter"

    def test_default_model_is_a_qwen35_slug(self):
        assert LLM().resolved_model().startswith("qwen/qwen3.5")

    def test_default_base_url_is_openrouters_openai_compatible_path(self):
        """/api/v1 — the OpenAI-compatible endpoint. Running on LangChain
        means the model client picks the protocol, so the OpenAI shape is the
        one nearly every internal gateway also speaks."""
        assert LLM().resolved_base_url() == "https://openrouter.ai/api/v1"


class TestChatModelConstruction:
    def test_openrouter_builds_an_openai_client_pointed_at_openrouter(self):
        chat = LLM(provider="openrouter", api_key="sk-or-abc").build_chat_model()
        assert type(chat).__name__ == "ChatOpenAI"
        assert str(chat.openai_api_base) == "https://openrouter.ai/api/v1"
        assert chat.openai_api_key.get_secret_value() == "sk-or-abc"

    def test_the_configured_model_reaches_the_client(self):
        chat = LLM(provider="openrouter", api_key="k", model="qwen/qwen3.5-flash-02-23")
        assert chat.build_chat_model().model_name == "qwen/qwen3.5-flash-02-23"

    def test_per_agent_model_overrides_the_config_default(self):
        """An agent may pin its own model; config only supplies the fallback."""
        chat = LLM(provider="openrouter", api_key="k").build_chat_model(model="some/other-model")
        assert chat.model_name == "some/other-model"

    def test_anthropic_builds_an_anthropic_client(self):
        chat = LLM(provider="anthropic", api_key="sk-ant-real").build_chat_model()
        assert type(chat).__name__ == "ChatAnthropic"
        assert chat.anthropic_api_key.get_secret_value() == "sk-ant-real"

    def test_base_url_override_wins_over_provider_default(self):
        llm = LLM(provider="openrouter", base_url="https://internal.example/v1", api_key="k")
        assert llm.resolved_base_url() == "https://internal.example/v1"
        assert str(llm.build_chat_model().openai_api_base) == "https://internal.example/v1"

    def test_a_missing_credential_fails_at_build_not_mid_run(self):
        """The provider client refuses to construct without a key. That is an
        improvement on the previous setup, where an unset key silently fell
        back to whatever credentials the machine happened to have."""
        with pytest.raises(Exception, match="(?i)credential|api_key"):
            LLM(provider="openrouter").build_chat_model()

    def test_anthropic_default_model_is_a_claude_model(self):
        assert LLM(provider="anthropic").resolved_model().startswith("claude-")


class TestCustomProvider:
    def test_custom_provider_has_no_default_base_url(self):
        """provider="custom" is for a real internal host — there's no sane
        guess for its URL, so resolving it without an override yields None
        rather than silently pointing somewhere wrong."""
        assert LLM(provider="custom").resolved_base_url() is None

    def test_custom_provider_speaks_the_openai_api_at_the_given_host(self):
        llm = LLM(
            provider="custom",
            base_url="https://llm.internal.corp/v1",
            api_key="internal-token",
            model="internal/qwen3.5",
        )
        chat = llm.build_chat_model()
        assert type(chat).__name__ == "ChatOpenAI"
        assert str(chat.openai_api_base) == "https://llm.internal.corp/v1"
        assert chat.openai_api_key.get_secret_value() == "internal-token"

    def test_custom_provider_without_a_model_is_a_loud_error(self):
        """There is no sensible default model for an unknown host; failing at
        construction beats a confusing 404 from the gateway mid-run."""
        with pytest.raises(ValueError, match="no default model"):
            LLM(provider="custom", base_url="https://x/v1").build_chat_model()


class TestEnvVarOverrides:
    def test_llm_provider_env_var(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        assert LLM().provider == "anthropic"

    def test_llm_model_env_var_overrides_provider_default(self, monkeypatch):
        monkeypatch.setenv("LLM_MODEL", "qwen/qwen3.5-flash-02-23")
        assert LLM().resolved_model() == "qwen/qwen3.5-flash-02-23"

    def test_llm_api_key_env_var(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-or-from-env")
        assert LLM().api_key.get_secret_value() == "sk-or-from-env"

    def test_env_vars_are_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("llm_provider", "anthropic")
        assert LLM().provider == "anthropic"

    def test_get_llm_is_cached_across_calls(self, monkeypatch):
        monkeypatch.setenv("LLM_MODEL", "qwen/qwen3.5-plus-02-15")
        first = get_llm()
        monkeypatch.setenv("LLM_MODEL", "qwen/qwen3.5-flash-02-23")
        second = get_llm()  # cache not invalidated -- same object
        assert first is second
        assert second.resolved_model() == "qwen/qwen3.5-plus-02-15"

    def test_reset_settings_cache_picks_up_the_new_value(self, monkeypatch):
        monkeypatch.setenv("LLM_MODEL", "qwen/qwen3.5-plus-02-15")
        get_llm()
        monkeypatch.setenv("LLM_MODEL", "qwen/qwen3.5-flash-02-23")
        reset_settings_cache()
        assert get_llm().resolved_model() == "qwen/qwen3.5-flash-02-23"


class TestOtherServicesFollowTheSamePattern:
    def test_minio_has_its_own_prefix_and_defaults(self):
        from deep_research_agent.config import Minio

        m = Minio()
        assert m.bucket_name == "cpoml-object-storage"

    def test_minio_env_prefix_is_isolated_from_llm(self, monkeypatch):
        monkeypatch.setenv("MINIO_BUCKET_NAME", "some-other-bucket")
        from deep_research_agent.config import Minio

        assert Minio().bucket_name == "some-other-bucket"
        assert LLM().provider == "openrouter"  # untouched by the minio_ var

    def test_db_dsn_defaults_empty(self):
        from deep_research_agent.config import DB

        assert DB().dsn.get_secret_value() == ""
