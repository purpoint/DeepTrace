"""Typed application configuration, loaded from the environment.

Configuration is validated once at startup rather than read ad hoc with
``os.getenv`` throughout the codebase. A missing or malformed setting fails
immediately with a clear message instead of surfacing as a confusing error deep
inside a research run.

Secrets are never given defaults. A default API key would let the application
start in a broken state and fail later at the first LLM call, which is a far
worse failure mode than refusing to start.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Environment(StrEnum):
    """Deployment environment.

    Behaviour that must differ between environments keys off this value rather
    than on scattered debug flags.
    """

    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class ResearchDepth(StrEnum):
    """How much work a research run is permitted to do.

    Depth is the primary cost control in DeepTrace. Each value maps to a hard
    ceiling on tasks, sources, verification loops, and tokens; see
    :class:`DepthBudget`.
    """

    QUICK = "quick"
    STANDARD = "standard"
    DEEP = "deep"


class DepthBudget:
    """Hard ceilings per research depth.

    These are limits, not targets. A run that satisfies its completion criteria
    early stops early. They exist so that an agent loop cannot spend unbounded
    money, which is a real failure mode of autonomous research systems.
    """

    __slots__ = ("max_sources", "max_tasks", "max_tokens", "max_verification_loops")

    def __init__(
        self,
        max_tasks: int,
        max_sources: int,
        max_verification_loops: int,
        max_tokens: int,
    ) -> None:
        self.max_tasks = max_tasks
        self.max_sources = max_sources
        self.max_verification_loops = max_verification_loops
        self.max_tokens = max_tokens


DEPTH_BUDGETS: dict[ResearchDepth, DepthBudget] = {
    ResearchDepth.QUICK: DepthBudget(
        max_tasks=3, max_sources=8, max_verification_loops=0, max_tokens=40_000
    ),
    ResearchDepth.STANDARD: DepthBudget(
        max_tasks=6, max_sources=20, max_verification_loops=1, max_tokens=150_000
    ),
    ResearchDepth.DEEP: DepthBudget(
        max_tasks=12, max_sources=50, max_verification_loops=3, max_tokens=500_000
    ),
}


class Settings(BaseSettings):
    """Application settings, populated from environment variables and ``.env``.

    Fields are grouped by concern. Anything not yet needed by an implemented
    milestone is optional, so the application starts without credentials for
    subsystems that do not exist yet. The layer that needs a credential is
    responsible for demanding it -- see :meth:`require`.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -- Application -------------------------------------------------------
    app_env: Environment = Environment.LOCAL
    log_level: str = "INFO"
    json_logs: bool = Field(
        default=False,
        description="Emit JSON logs. Enabled in deployed environments for log aggregation.",
    )

    # -- LLM providers -----------------------------------------------------
    # No agent reads these directly. The provider layer resolves them, which is
    # what keeps agents unaware of which vendor is serving a request.
    llm_provider: str = Field(
        default="openai",
        description="Default provider id. Individual agents may override it.",
    )
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    google_api_key: str | None = None

    # Model routing. Cheap models handle classification and extraction; strong
    # models handle analysis, verification, and synthesis. Names are configured
    # rather than hardcoded so routing can be retuned without code changes.
    llm_model_cheap: str = "gpt-4o-mini"
    llm_model_strong: str = "gpt-4o"
    llm_model_embed: str = "text-embedding-3-small"

    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 3

    # -- Search ------------------------------------------------------------
    tavily_api_key: str | None = None
    search_timeout_seconds: float = 30.0

    # -- Data stores -------------------------------------------------------
    database_url: str | None = None
    redis_url: str = "redis://localhost:6379/0"

    # -- Research limits ---------------------------------------------------
    default_depth: ResearchDepth = ResearchDepth.STANDARD
    max_concurrent_tasks: int = Field(
        default=5,
        description="Bounded parallelism. Protects search quotas, LLM spend, and the database.",
    )
    max_graph_iterations: int = Field(
        default=25,
        description="Absolute ceiling on workflow steps. Makes infinite agent loops impossible.",
    )

    # -- Observability -----------------------------------------------------
    langsmith_api_key: str | None = None
    langsmith_project: str = "deeptrace"
    run_log_path: Path = PROJECT_ROOT / "data" / "runs"

    # -- Security ----------------------------------------------------------
    jwt_secret: str | None = None

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        """Reject an unknown log level at startup rather than at first log call."""
        level = value.upper()
        if not isinstance(logging.getLevelName(level), int):
            valid = "DEBUG, INFO, WARNING, ERROR, CRITICAL"
            raise ValueError(f"log_level must be one of: {valid}. Got: {value!r}")
        return level

    @property
    def is_production(self) -> bool:
        return self.app_env is Environment.PRODUCTION

    @property
    def depth_budget(self) -> DepthBudget:
        """Budget ceilings for the configured default depth."""
        return DEPTH_BUDGETS[self.default_depth]

    def require(self, field: str) -> str:
        """Return a required setting, or fail with an actionable message.

        Used by the layer that actually needs a credential, at the moment it
        needs it. This keeps startup permissive while making the eventual
        failure specific about which variable is missing and where to set it.
        """
        value = getattr(self, field, None)
        if not value:
            raise MissingConfigurationError(field)
        return str(value)


class MissingConfigurationError(RuntimeError):
    """Raised when a required setting is absent.

    Carries the variable name so the message tells the operator exactly what to
    add to ``.env`` instead of producing a generic failure.
    """

    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(
            f"Required configuration '{field}' is not set. "
            f"Add {field.upper()} to your .env file (see .env.example)."
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so the ``.env`` file is parsed once and every caller observes the
    same configuration. Tests clear the cache via ``get_settings.cache_clear()``
    after changing environment variables.
    """
    return Settings()
