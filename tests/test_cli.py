"""The entry points, and the path resolution that moving them into the
package made necessary.

These are cheap structural checks, but they cover the failure mode of an
entry point: it is the code least likely to be exercised by anything else, so
a broken import or a renamed flag shows up in production rather than in CI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deep_research_agent.__main__ import COMMANDS, main
from deep_research_agent.config import reset_settings_cache
from deep_research_agent.config.paths import Paths, get_paths

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def clean_config():
    reset_settings_cache()
    yield
    reset_settings_cache()


class TestTheDispatcher:
    def test_no_arguments_prints_usage_and_succeeds(self, capsys):
        assert main([]) == 0
        assert "commands:" in capsys.readouterr().out

    def test_an_unknown_command_is_an_error_not_a_crash(self, capsys):
        assert main(["nope"]) == 2
        assert "unknown command" in capsys.readouterr().err

    @pytest.mark.parametrize("command", sorted(COMMANDS))
    def test_every_advertised_command_imports_and_parses(self, command):
        """A dispatcher that lists a command it cannot import is worse than
        one that lists nothing."""
        from importlib import import_module

        module = import_module(f"deep_research_agent.cli.{command}")
        assert callable(module.main)
        assert module.parser().prog.endswith(command)

    @pytest.mark.parametrize("command", sorted(COMMANDS))
    def test_help_works_for_every_command(self, command, capsys):
        with pytest.raises(SystemExit) as exit:
            main([command, "--help"])
        assert exit.value.code == 0
        assert command in capsys.readouterr().out


class TestPathResolution:
    """The entry points used to be scripts next to the repo root and resolved
    it with `__file__.parent.parent`. Inside the package that is wrong -- an
    installed wheel has no repo above it -- so the roots became config."""

    def test_a_source_checkout_needs_no_configuration(self):
        assert get_paths().resolved_project_root() == ROOT

    def test_the_checkout_is_found_regardless_of_working_directory(self, monkeypatch, tmp_path):
        """A cron entry runs from wherever the scheduler happened to be."""
        monkeypatch.chdir(tmp_path)
        assert get_paths().resolved_project_root() == ROOT

    def test_fixtures_default_under_the_project_root(self):
        assert get_paths().resolved_fixtures() == ROOT / "fixtures"

    def test_an_explicit_root_wins(self, tmp_path):
        paths = Paths(project_root=str(tmp_path))
        assert paths.resolved_project_root() == tmp_path.resolve()
        assert paths.resolved_fixtures() == tmp_path.resolve() / "fixtures"

    def test_fixtures_can_be_moved_independently(self, tmp_path):
        paths = Paths(fixtures=str(tmp_path / "data"))
        assert paths.resolved_fixtures() == (tmp_path / "data").resolve()
        assert paths.resolved_project_root() == ROOT

    def test_env_vars_are_read(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PATHS_PROJECT_ROOT", str(tmp_path))
        reset_settings_cache()
        assert get_paths().resolved_project_root() == tmp_path.resolve()

    def test_the_skill_search_path_follows_the_configured_root(self, tmp_path, monkeypatch):
        """The point of making the root configurable: a container mounts its
        skills somewhere that is not a checkout."""
        from deep_research_agent.runtime import skill_roots

        monkeypatch.setenv("PATHS_PROJECT_ROOT", str(tmp_path))
        reset_settings_cache()
        roots = [str(r) for r in skill_roots(get_paths().resolved_project_root())]
        assert str(tmp_path.resolve() / "skills") in roots


class TestStartupRefusesInsteadOfCrashing:
    """A missing key used to surface as a provider traceback ending in "set
    the OPENAI_API_KEY environment variable" -- wrong advice, since the
    variable here is LLM_API_KEY and the host may not be OpenAI at all."""

    def test_a_missing_key_is_reported_in_this_repos_terms(self, monkeypatch, capsys):
        from deep_research_agent.cli.preflight import require_llm

        monkeypatch.setenv("LLM_PROVIDER", "openrouter")
        monkeypatch.setenv("LLM_API_KEY", "")
        reset_settings_cache()

        with pytest.raises(SystemExit) as exit:
            require_llm()
        assert exit.value.code == 2

        err = capsys.readouterr().err
        assert "LLM_API_KEY" in err
        assert "OPENAI_API_KEY" not in err

    def test_misconfiguration_and_failure_have_different_exit_codes(self, monkeypatch):
        """2 is "you configured it wrong"; 1 is "it ran and did not succeed".
        A script driving these needs to tell them apart."""
        from deep_research_agent.cli.preflight import require_llm

        monkeypatch.setenv("LLM_API_KEY", "")
        reset_settings_cache()
        with pytest.raises(SystemExit) as exit:
            require_llm()
        assert exit.value.code == 2

    def test_a_configured_provider_passes(self, monkeypatch):
        from deep_research_agent.cli.preflight import require_llm

        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        reset_settings_cache()
        require_llm()  # must not raise

    def test_the_server_warns_but_does_not_refuse(self, monkeypatch, capsys):
        """It builds no model client until a task arrives, and a deployment
        may inject the key after the container starts."""
        from deep_research_agent.cli.preflight import warn_about_llm

        monkeypatch.setenv("LLM_API_KEY", "")
        reset_settings_cache()
        warn_about_llm()  # must not raise
        assert "LLM_API_KEY" in capsys.readouterr().err


class TestWhichConfigurationsCountAsBroken:
    def test_a_local_gateway_may_legitimately_need_no_key(self, monkeypatch):
        """vLLM and Ollama take no credential. Refusing to start against one
        would be the same mistake in the other direction."""
        from deep_research_agent.config.llm import LLM

        config = LLM(provider="custom", api_key="", base_url="http://vllm:8000/v1", model="q")
        assert config.problems() == []

    def test_custom_without_a_base_url_is_caught(self, monkeypatch):
        """Otherwise requests silently go to the OpenAI SDK's default host
        instead of the internal gateway -- a confusing way to fail."""
        from deep_research_agent.config.llm import LLM

        problems = LLM(provider="custom", model="q", base_url=None).problems()
        assert any("LLM_BASE_URL" in p for p in problems)

    def test_custom_without_a_model_is_caught(self):
        from deep_research_agent.config.llm import LLM

        problems = LLM(provider="custom", base_url="http://x/v1", model=None).problems()
        assert any("LLM_MODEL" in p for p in problems)

    def test_a_hosted_provider_with_a_key_is_fine(self):
        from deep_research_agent.config.llm import LLM

        assert LLM(provider="openrouter", api_key="sk-x").problems() == []
