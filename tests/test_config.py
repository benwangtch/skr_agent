"""The config layer: env-var overrides, provider defaults, the CLI-env shape
that makes the OpenRouter/Qwen "simulate an internal host" trick work.

Each test clears LLM_/DB_/MINIO_* env vars first so the process's real
environment (which may have ANTHROPIC_API_KEY set for other purposes) can't
leak into an assertion.
"""

from __future__ import annotations

import os

import pytest

from skr_agent.config import LLM, get_llm, reset_settings_cache

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

    def test_default_base_url_is_the_anthropic_compatible_openrouter_path(self):
        """Not /api/v1 — that's OpenRouter's OpenAI-compatible endpoint, which
        speaks a different wire protocol than the Claude Agent SDK expects."""
        assert LLM().resolved_base_url() == "https://openrouter.ai/api"

    def test_no_key_set_still_produces_a_usable_env_shape(self):
        env = LLM().to_cli_env()
        assert env["ANTHROPIC_API_KEY"] == ""
        assert "ANTHROPIC_BASE_URL" in env
        assert "ANTHROPIC_AUTH_TOKEN" not in env  # nothing to send


class TestOpenRouterEnvShape:
    """The specific env shape OpenRouter's Anthropic-compatible endpoint needs."""

    def test_key_goes_on_auth_token_not_api_key(self):
        env = LLM(provider="openrouter", api_key="sk-or-abc").to_cli_env()
        assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-or-abc"

    def test_api_key_is_explicitly_blanked_not_omitted(self):
        """An omitted ANTHROPIC_API_KEY falls back to a locally logged-in
        `claude` session, silently sending the request to the real Anthropic
        API instead of the configured endpoint. Blanking it forecloses that."""
        env = LLM(provider="openrouter", api_key="sk-or-abc").to_cli_env()
        assert env["ANTHROPIC_API_KEY"] == ""

    def test_base_url_override_wins_over_provider_default(self):
        llm = LLM(provider="openrouter", base_url="https://internal.example/anthropic")
        assert llm.resolved_base_url() == "https://internal.example/anthropic"


class TestAnthropicProvider:
    def test_anthropic_provider_uses_the_real_api_key_field(self):
        env = LLM(provider="anthropic", api_key="sk-ant-real").to_cli_env()
        assert env["ANTHROPIC_API_KEY"] == "sk-ant-real"
        assert "ANTHROPIC_AUTH_TOKEN" not in env

    def test_anthropic_provider_with_no_override_sets_no_base_url(self):
        """Absence, not an explicit empty string — the SDK's own default
        (api.anthropic.com) should apply, not an override to nothing."""
        env = LLM(provider="anthropic").to_cli_env()
        assert "ANTHROPIC_BASE_URL" not in env

    def test_anthropic_default_model_is_a_claude_model(self):
        assert LLM(provider="anthropic").resolved_model().startswith("claude-")


class TestCustomProvider:
    def test_custom_provider_has_no_default_base_url(self):
        """provider="custom" is for a real internal host — there's no sane
        guess for its URL, so resolving it without an override yields None
        rather than silently pointing somewhere wrong."""
        assert LLM(provider="custom").resolved_base_url() is None

    def test_custom_provider_uses_the_same_auth_token_shape_as_openrouter(self):
        llm = LLM(
            provider="custom",
            base_url="https://llm.internal.corp/anthropic",
            api_key="internal-token",
        )
        env = llm.to_cli_env()
        assert env["ANTHROPIC_AUTH_TOKEN"] == "internal-token"
        assert env["ANTHROPIC_API_KEY"] == ""
        assert env["ANTHROPIC_BASE_URL"] == "https://llm.internal.corp/anthropic"


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
        from skr_agent.config import Minio

        m = Minio()
        assert m.bucket_name == "cpoml-object-storage"

    def test_minio_env_prefix_is_isolated_from_llm(self, monkeypatch):
        monkeypatch.setenv("MINIO_BUCKET_NAME", "some-other-bucket")
        from skr_agent.config import Minio

        assert Minio().bucket_name == "some-other-bucket"
        assert LLM().provider == "openrouter"  # untouched by the minio_ var

    def test_db_dsn_defaults_empty(self):
        from skr_agent.config import DB

        assert DB().dsn.get_secret_value() == ""
