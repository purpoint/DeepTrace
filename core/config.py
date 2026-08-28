"""Typed application configuration, loaded from the environment.

Configuration is validated once at startup rather than read ad hoc with
``os.getenv`` throughout the codebase. A missing or malformed setting fails
immediately with a clear message instead of surfacing as a confusing error deep
inside a research run.

Secrets are never given defaults. A default API key would let the application
start in a broken state and fail later at the first LLM call, which is a far
worse failure mode than refusing to start.

Secrets may also arrive from *files* rather than the environment -- see
:class:`FileSecretsSource`, which is what a deployed instance uses.
"""

from __future__ import annotations

import logging
import os
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

FILE_SUFFIX = "_FILE"
"""Environment-variable suffix naming a file to read a setting from.

``JWT_SECRET_FILE=/run/secrets/jwt_secret`` rather than ``JWT_SECRET=...``.
"""


PRODUCTION_REQUIRED = {
    "jwt_secret": "no signing key: every sign-in and every stream ticket fails",
    "database_url": "no database: nothing a run produces is kept",
    "google_api_key": "no model provider: every research run fails at the first agent",
    "tavily_api_key": "no search provider: every research run fails at the first query",
}
"""What a production deployment cannot run without, and why each one is on the
list rather than left to fail at the point of use."""


class SecretFileError(RuntimeError):
    """Raised when a ``*_FILE`` variable cannot be honoured.

    Never carries the file's contents -- only its path. An exception message is
    the least controlled string in a program: it reaches logs, crash reporters
    and error pages, which is exactly the journey a secret must not make.
    """


class FileSecretsSource(PydanticBaseSettingsSource):
    """Read settings from files named by ``<FIELD>_FILE`` environment variables.

    **Why a file beats an environment variable for a secret.** An environment
    variable is readable by anything that can see the process: ``docker
    inspect`` prints it, ``/proc/<pid>/environ`` exposes it to any process
    running as the same user, it is inherited by every child process including
    ones spawned to run something unrelated, and it is dumped verbatim by a
    surprising number of crash handlers. None of that is a vulnerability in this
    application; all of it is a way this application's keys leave it. A file is
    read once, by the one process that opens it, and by nothing else -- it is
    not inherited, not enumerable, and not printed by anything that inspects the
    container.

    This is the convention the ``postgres`` image in this stack already uses
    (``POSTGRES_PASSWORD_FILE``), so the compose file speaks one idiom rather
    than two.

    Three behaviours are deliberate, and each one is a failure that would
    otherwise be silent:

    *A trailing newline is stripped.* Secret files are written with ``echo`` and
    by every secret manager in existence, so almost all of them end in ``\\n``.
    A signing key with a stray newline is simply a different key, and the
    symptom is that tokens minted before a redeploy stop verifying -- which
    reads as a session bug, not a configuration one.

    *Naming both forms is an error.* An operator moving a deployment from
    environment variables to files will, at some point, have both set. Quietly
    preferring one means they believe they have rotated a credential that is
    still the old one.

    *An unreadable file is an error.* Falling back to "unset" would let the
    application start without the secret it was explicitly told where to find,
    and `require()` would then blame a variable the operator did set.
    """

    def __init__(self, settings_cls: type[BaseSettings], environ: dict[str, str] | None = None):
        super().__init__(settings_cls)
        self._environ = os.environ if environ is None else environ

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        # Everything is resolved in __call__, which needs to see all the
        # variables at once to detect a field named both ways.
        raise NotImplementedError

    def __call__(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for field_name in self.settings_cls.model_fields:
            variable = f"{field_name.upper()}{FILE_SUFFIX}"
            path = self._environ.get(variable)
            if not path:
                continue

            if self._environ.get(field_name.upper()):
                raise SecretFileError(
                    f"Both {field_name.upper()} and {variable} are set. "
                    f"Remove one -- with both present it is not possible to tell "
                    f"which value the application is actually using."
                )

            try:
                content = Path(path).read_text(encoding="utf-8")
            except OSError as exc:
                raise SecretFileError(
                    f"{variable} points at {path!r}, which could not be read: "
                    f"{exc.strerror or exc}. A secret file that is named must exist; "
                    f"starting without it would look like the variable was never set."
                ) from None

            # rstrip rather than strip: leading whitespace in a secret is
            # unusual enough that removing it might change a legitimate value,
            # while a trailing newline is an artefact of how the file was
            # written in essentially every case.
            values[field_name] = content.rstrip("\r\n")
        return values

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}()"


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

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Insert file-backed secrets above the environment.

        Ordered above ``env_settings`` so that a deployment which has been moved
        onto files is not silently served by a stale variable left in a shell
        profile. In practice the two never both win, because naming a setting
        both ways is an error -- the ordering decides what happens with ``.env``,
        which is a local-development convenience and should lose to an explicit
        file every time.
        """
        return (
            init_settings,
            FileSecretsSource(settings_cls),
            env_settings,
            dotenv_settings,
            file_secret_settings,
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
    otel_exporter: str = Field(
        default="none",
        description=(
            "Where spans go: none, console, or otlp. Defaults to none, which "
            "leaves the OpenTelemetry API on its no-op implementation -- so an "
            "untraced run costs nothing rather than a little, and nothing has "
            "to be running for the application to start."
        ),
    )
    otel_endpoint: str | None = Field(
        default=None,
        description="Collector URL, required when otel_exporter is otlp.",
    )
    otel_service_name: str = "deeptrace"

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

    @model_validator(mode="after")
    def _production_has_its_credentials(self) -> Settings:
        """Refuse to start a production instance that is missing a credential.

        Deliberately here rather than in the compose file. `${VAR:?message}`
        guards exactly one way of starting the application, checks only that a
        string is non-empty, and is invisible to the test suite -- and it cannot
        coexist with secrets supplied as files, because compose interpolates each
        file before merging any overlay, so a base file demanding an environment
        variable demands it even from a deployment that has deliberately replaced
        it with a mounted file.

        Stated as a property of the application instead, it holds for compose,
        for Kubernetes, for a systemd unit and for a shell, and it is a unit
        test rather than a thing someone notices in YAML.

        Everything is reported at once. A deployment missing three credentials
        should learn that in one restart rather than three.
        """
        if self.app_env is not Environment.PRODUCTION:
            return self

        missing = [name for name in PRODUCTION_REQUIRED if not getattr(self, name, None)]
        if missing:
            detail = "\n".join(
                f"  - {name.upper()}: {PRODUCTION_REQUIRED[name]}" for name in missing
            )
            raise ValueError(
                f"APP_ENV is production but {len(missing)} required credential(s) are missing:\n"
                f"{detail}\n"
                f"Set each as an environment variable, or as a file with "
                f"<NAME>{FILE_SUFFIX} pointing at it."
            )
        return self

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
