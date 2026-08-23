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
        default="google",
        description="Default provider id. Individual agents may override it.",
    )
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    google_api_key: str | None = None

    # Model routing. Cheap models handle classification and extraction; strong
    # models handle analysis, verification, and synthesis. Names are configured
    # rather than hardcoded so routing can be retuned without code changes.
    llm_model_cheap: str = "gemini-3.5-flash-lite"
    llm_model_strong: str = "gemini-3.7-flash"
    llm_model_embed: str = "gemini-embedding-001"

    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 3
    llm_requests_per_minute: float = Field(
        default=10.0,
        description=(
            "Sustained request rate the client shapes traffic to. Set to the "
            "provider's published limit; 0 disables limiting. A free tier is "
            "typically far below what a research run would otherwise issue."
        ),
    )

    # -- Search ------------------------------------------------------------
    tavily_api_key: str | None = None
    search_timeout_seconds: float = 30.0

    # -- API ---------------------------------------------------------------
    cors_origins: list[str] = Field(
        default_factory=list,
        description=(
            "Browser origins allowed to call the API. Empty disables CORS "
            "entirely, which is right for a service with no browser client. "
            "Listed rather than wildcarded: a wildcard is convenient until "
            "credentials are involved, and by then it has shipped."
        ),
    )

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
        default=60,
        description=(
            "Absolute ceiling on workflow steps. Makes infinite agent loops "
            "impossible. Must exceed what a legitimate run costs: research fans "
            "out to one step per task plus one per wave, so the worst case is "
            "2*max_tasks + 4 -- 28 at the deep budget."
        ),
    )

    # -- Observability -----------------------------------------------------
    langsmith_api_key: str | None = None
    langsmith_project: str = "deeptrace"
    run_log_path: Path = PROJECT_ROOT / "data" / "runs"

    # -- Security ----------------------------------------------------------
    # No default. A signing key with a default is a signing key every
    # deployment shares, and anyone who has read the source can mint a token
    # for any account. The layer that signs demands it via require().
    jwt_secret: str | None = None

    jwt_issuer: str = "deeptrace"
    """Who minted a token. Checked on verification.

    Cheap, and it stops a token issued by another service that happens to share
    the secret -- a staging environment, a sibling application -- from being
    accepted here."""

    access_token_ttl_seconds: int = Field(
        default=15 * 60,
        ge=60,
        description=(
            "How long an access token is usable. Short, because it is verified "
            "by signature alone and therefore cannot be revoked: the window in "
            "which a stolen one works is exactly this number."
        ),
    )
    refresh_token_ttl_seconds: int = Field(
        default=14 * 24 * 60 * 60,
        ge=300,
        description=(
            "How long a session survives without re-entering a password. Long, "
            "which is affordable because refresh tokens are recorded server "
            "side and can be revoked one at a time."
        ),
    )
    ws_ticket_ttl_seconds: int = Field(
        default=30,
        ge=5,
        le=300,
        description=(
            "How long a WebSocket ticket is valid. Seconds, because a ticket "
            "travels in a query string and query strings are written to access "
            "logs -- it is worthless by the time anyone reads one."
        ),
    )

    password_min_length: int = Field(default=12, ge=8)
    password_max_length: int = Field(
        default=1024,
        description=(
            "An upper bound is a denial-of-service control, not a password "
            "policy. Argon2 is deliberately expensive, so an unbounded password "
            "field is a way to make the server hash a megabyte on demand."
        ),
    )

    # -- Rate limits -------------------------------------------------------
    # Two different things are being protected, so they are two different
    # policies. Authentication is protected from guessing, and is counted per
    # client address because the attacker chooses the account. Research is
    # protected from spending, and is counted per user because that is who the
    # money belongs to.
    auth_rate_limit: int = Field(default=10, ge=1)
    auth_rate_window_seconds: int = Field(default=15 * 60, ge=1)
    submit_rate_limit: int = Field(default=20, ge=1)
    submit_rate_window_seconds: int = Field(default=60 * 60, ge=1)

    @field_validator("jwt_secret")
    @classmethod
    def _validate_jwt_secret(cls, value: str | None) -> str | None:
        """Refuse a signing key short enough to be searched.

        HS256 keys are brute-forceable offline: an attacker with one token can
        try candidate keys as fast as their hardware allows, with no server
        involved and nothing to rate limit. Thirty-two characters is the point
        at which that stops being a weekend project.

        Rejected at startup rather than at first login, because a key this weak
        is a deployment mistake and the moment to find it is before anyone has
        signed in with it.
        """
        if not value:
            # An unset variable and one present but empty are the same
            # intention. Treating "" as a two-character key would fail startup
            # with a message about length, for a deployment that simply has not
            # configured signing yet -- require() gives a better one.
            return None
        if len(value) < 32:
            raise ValueError(
                "jwt_secret must be at least 32 characters. "
                "Generate one with: python -c 'import secrets; print(secrets.token_urlsafe(48))'"
            )
        return value

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
