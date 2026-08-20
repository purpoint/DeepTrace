"""Tests for typed configuration.

These lock in behaviour that is easy to erode: that secrets never acquire
defaults, that an invalid setting fails at startup, and that depth budgets stay
internally consistent.
"""

from __future__ import annotations

import pytest

from core.config import (
    DEPTH_BUDGETS,
    Environment,
    MissingConfigurationError,
    ResearchDepth,
    Settings,
    get_settings,
)

pytestmark = pytest.mark.unit


class TestDefaults:
    def test_local_environment_by_default(self, settings: Settings) -> None:
        assert settings.app_env is Environment.LOCAL
        assert settings.is_production is False

    def test_standard_depth_by_default(self, settings: Settings) -> None:
        assert settings.default_depth is ResearchDepth.STANDARD

    def test_concurrency_and_iterations_are_bounded(self, settings: Settings) -> None:
        """Unbounded parallelism and unbounded loops are the two ways an agent
        system burns money without producing anything."""
        assert settings.max_concurrent_tasks > 0
        assert settings.max_graph_iterations > 0


class TestSecretsHaveNoDefaults:
    """A placeholder credential lets the app start broken and fail later.

    Refusing to start is the better failure mode, so every secret must default
    to None. This test fails if someone adds a convenient default.
    """

    @pytest.mark.parametrize(
        "field",
        [
            "openai_api_key",
            "anthropic_api_key",
            "google_api_key",
            "tavily_api_key",
            "database_url",
            "jwt_secret",
            "langsmith_api_key",
        ],
    )
    def test_secret_defaults_to_none(self, settings: Settings, field: str) -> None:
        assert getattr(settings, field) is None, f"{field} must not have a default value"


class TestLogLevelValidation:
    def test_rejects_unknown_level(self) -> None:
        """Fail at construction, not at the first log call."""
        with pytest.raises(ValueError, match="log_level"):
            Settings(_env_file=None, log_level="LOUD")

    def test_normalises_case(self) -> None:
        assert Settings(_env_file=None, log_level="debug").log_level == "DEBUG"

    @pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    def test_accepts_standard_levels(self, level: str) -> None:
        assert Settings(_env_file=None, log_level=level).log_level == level


class TestRequire:
    def test_missing_secret_names_the_variable(self, settings: Settings) -> None:
        """The error must tell an operator exactly what to add and where."""
        with pytest.raises(MissingConfigurationError) as exc:
            settings.require("openai_api_key")

        message = str(exc.value)
        assert "OPENAI_API_KEY" in message
        assert ".env.example" in message
        assert exc.value.field == "openai_api_key"

    def test_returns_value_when_present(self) -> None:
        configured = Settings(_env_file=None, openai_api_key="sk-test-value")
        assert configured.require("openai_api_key") == "sk-test-value"

    def test_empty_string_is_treated_as_missing(self) -> None:
        """An empty env var is a common misconfiguration and must not pass."""
        configured = Settings(_env_file=None, openai_api_key="")
        with pytest.raises(MissingConfigurationError):
            configured.require("openai_api_key")


class TestDepthBudgets:
    def test_every_depth_has_a_budget(self) -> None:
        """A depth without a budget would run unbounded."""
        assert set(DEPTH_BUDGETS) == set(ResearchDepth)

    def test_budgets_increase_with_depth(self) -> None:
        quick = DEPTH_BUDGETS[ResearchDepth.QUICK]
        standard = DEPTH_BUDGETS[ResearchDepth.STANDARD]
        deep = DEPTH_BUDGETS[ResearchDepth.DEEP]

        assert quick.max_tasks < standard.max_tasks < deep.max_tasks
        assert quick.max_sources < standard.max_sources < deep.max_sources
        assert quick.max_tokens < standard.max_tokens < deep.max_tokens
        assert (
            quick.max_verification_loops
            <= standard.max_verification_loops
            <= deep.max_verification_loops
        )

    def test_all_limits_are_positive(self) -> None:
        for depth, budget in DEPTH_BUDGETS.items():
            assert budget.max_tasks > 0, depth
            assert budget.max_sources > 0, depth
            assert budget.max_tokens > 0, depth
            assert budget.max_verification_loops >= 0, depth

    def test_settings_exposes_budget_for_configured_depth(self) -> None:
        configured = Settings(_env_file=None, default_depth=ResearchDepth.DEEP)
        assert configured.depth_budget is DEPTH_BUDGETS[ResearchDepth.DEEP]


class TestEnvironmentOverrides:
    def test_env_var_overrides_default(self, isolated_env: pytest.MonkeyPatch) -> None:
        isolated_env.setenv("APP_ENV", "production")
        isolated_env.setenv("OPENAI_API_KEY", "sk-from-environment")

        configured = Settings(_env_file=None)

        assert configured.app_env is Environment.PRODUCTION
        assert configured.is_production is True
        assert configured.require("openai_api_key") == "sk-from-environment"

    def test_env_var_names_are_case_insensitive(self, isolated_env: pytest.MonkeyPatch) -> None:
        isolated_env.setenv("openai_api_key", "sk-lowercase-name")
        assert Settings(_env_file=None).openai_api_key == "sk-lowercase-name"


class TestSettingsCache:
    def test_get_settings_returns_same_instance(self) -> None:
        """Cached so the .env file is parsed once and all callers agree."""
        assert get_settings() is get_settings()

    def test_cache_clear_rebuilds(self) -> None:
        first = get_settings()
        get_settings.cache_clear()
        assert get_settings() is not first
