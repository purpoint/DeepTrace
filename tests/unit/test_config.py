"""Tests for typed configuration.

These lock in behaviour that is easy to erode: that secrets never acquire
defaults, that an invalid setting fails at startup, and that depth budgets stay
internally consistent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.config import (
    DEPTH_BUDGETS,
    PRODUCTION_REQUIRED,
    Environment,
    MissingConfigurationError,
    ResearchDepth,
    SecretFileError,
    Settings,
    get_settings,
)

pytestmark = pytest.mark.unit

PRODUCTION_CREDENTIALS = {
    "JWT_SECRET": "k" * 40,
    "DATABASE_URL": "postgresql+asyncpg://u:p@db:5432/deeptrace",
    "GOOGLE_API_KEY": "google-key",
    "TAVILY_API_KEY": "tvly-key",
}
"""The set a production instance refuses to start without."""


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
        # Production refuses to start without these; supplied so this test
        # stays about the override it is named for.
        for name, value in PRODUCTION_CREDENTIALS.items():
            isolated_env.setenv(name, value)

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


class TestSecretsFromFiles:
    """A deployed instance is given paths, not values.

    An environment variable is readable by `docker inspect`, by anything that
    can open /proc/<pid>/environ, and by every child process the container ever
    spawns. None of that is a flaw in this application; all of it is a way this
    application's keys leave it.
    """

    def test_a_secret_is_read_from_the_file_it_names(
        self, isolated_env: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        secret = tmp_path / "jwt_secret"
        secret.write_text("a" * 40)
        isolated_env.setenv("JWT_SECRET_FILE", str(secret))

        assert Settings(_env_file=None).jwt_secret == "a" * 40

    def test_a_trailing_newline_is_not_part_of_the_secret(
        self, isolated_env: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Every secret manager and every `echo` writes one. A signing key with
        a stray newline is a different key, and the symptom is that tokens
        minted before a redeploy stop verifying -- which reads as a session bug
        rather than a configuration one."""
        secret = tmp_path / "jwt_secret"
        secret.write_text("a" * 40 + "\n")
        isolated_env.setenv("JWT_SECRET_FILE", str(secret))

        assert Settings(_env_file=None).jwt_secret == "a" * 40

    def test_naming_a_secret_both_ways_is_an_error(
        self, isolated_env: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An operator migrating from variables to files will have both set at
        some point. Quietly preferring one means they believe they have rotated
        a credential that is still the old one."""
        secret = tmp_path / "jwt_secret"
        secret.write_text("a" * 40)
        isolated_env.setenv("JWT_SECRET_FILE", str(secret))
        isolated_env.setenv("JWT_SECRET", "b" * 40)

        with pytest.raises(SecretFileError, match="Both JWT_SECRET and JWT_SECRET_FILE"):
            Settings(_env_file=None)

    def test_a_missing_file_is_an_error_rather_than_an_unset_value(
        self, isolated_env: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Falling back to unset would start the application without the secret
        it was told where to find, and blame a variable the operator did set."""
        isolated_env.setenv("JWT_SECRET_FILE", str(tmp_path / "absent"))

        with pytest.raises(SecretFileError, match="could not be read"):
            Settings(_env_file=None)

    def test_the_error_never_contains_the_secret(
        self, isolated_env: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An exception message reaches logs, crash reporters and error pages,
        which is exactly the journey a secret must not make."""
        secret = tmp_path / "jwt_secret"
        secret.write_text("super-secret-value-nobody-should-ever-see")
        isolated_env.setenv("JWT_SECRET_FILE", str(secret))
        isolated_env.setenv("JWT_SECRET", "b" * 40)

        with pytest.raises(SecretFileError) as caught:
            Settings(_env_file=None)

        assert "super-secret-value" not in str(caught.value)

    def test_a_file_beats_a_dotenv_entry(self, tmp_path: Path) -> None:
        """`.env` is a local-development convenience. An explicit file is a
        deliberate act, and should win."""
        secret = tmp_path / "jwt_secret"
        secret.write_text("f" * 40)
        dotenv = tmp_path / ".env"
        dotenv.write_text("JWT_SECRET=" + "d" * 40 + "\n")

        import os

        os.environ["JWT_SECRET_FILE"] = str(secret)
        try:
            assert Settings(_env_file=dotenv).jwt_secret == "f" * 40
        finally:
            del os.environ["JWT_SECRET_FILE"]


class TestProductionRefusesToStartWithoutCredentials:
    """The guarantee that used to live in `${VAR:?}` in the compose file.

    It could only ever guard one way of starting the application, and compose
    interpolates each file before merging an overlay -- so it also demanded an
    environment variable from a deployment that had deliberately replaced it
    with a mounted file. As a property of Settings it holds for compose, for
    Kubernetes, for a systemd unit and for a shell, and it is testable.
    """

    def test_a_local_instance_starts_without_them(self, settings: Settings) -> None:
        """Nothing here changes for development. The whole point of `require()`
        is that a subsystem demands its own credential when it is used."""
        assert settings.jwt_secret is None

    def test_production_refuses(self, isolated_env: pytest.MonkeyPatch) -> None:
        isolated_env.setenv("APP_ENV", "production")

        with pytest.raises(ValueError, match="required credential"):
            Settings(_env_file=None)

    def test_it_names_every_missing_credential_at_once(
        self, isolated_env: pytest.MonkeyPatch
    ) -> None:
        """A deployment missing three should learn that in one restart rather
        than three."""
        isolated_env.setenv("APP_ENV", "production")
        isolated_env.setenv("JWT_SECRET", "k" * 40)

        with pytest.raises(ValueError) as caught:
            Settings(_env_file=None)

        message = str(caught.value)
        assert "DATABASE_URL" in message
        assert "GOOGLE_API_KEY" in message
        assert "TAVILY_API_KEY" in message
        assert "JWT_SECRET:" not in message

    def test_production_starts_once_they_are_present(
        self, isolated_env: pytest.MonkeyPatch
    ) -> None:
        isolated_env.setenv("APP_ENV", "production")
        for name, value in PRODUCTION_CREDENTIALS.items():
            isolated_env.setenv(name, value)

        assert Settings(_env_file=None).is_production is True

    def test_a_file_satisfies_the_requirement(
        self, isolated_env: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The case the compose guard could not express: production, with every
        credential mounted rather than exported."""
        isolated_env.setenv("APP_ENV", "production")
        for name, value in PRODUCTION_CREDENTIALS.items():
            path = tmp_path / name.lower()
            path.write_text(value + "\n")
            isolated_env.setenv(f"{name}_FILE", str(path))

        configured = Settings(_env_file=None)

        assert configured.is_production is True
        assert configured.jwt_secret == PRODUCTION_CREDENTIALS["JWT_SECRET"]

    def test_every_required_name_is_a_real_setting(self) -> None:
        """A typo here would demand a credential nothing reads, and never be
        satisfiable."""
        assert set(PRODUCTION_REQUIRED) <= set(Settings.model_fields)
