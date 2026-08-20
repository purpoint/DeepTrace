"""Shared pytest fixtures.

Two pieces of global state can leak between tests and produce failures that
depend on execution order: the cached settings singleton and structlog's
context variables. Both are reset automatically for every test.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import structlog

from core.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    """Clear the settings singleton around each test.

    ``get_settings`` is ``lru_cache``d, so a test that changes environment
    variables would otherwise poison every test that runs after it.
    """
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_log_context() -> Iterator[None]:
    """Clear bound log context around each test.

    Mirrors what the worker does at the end of a research run, and prevents a
    ``research_id`` bound by one test from appearing in another's records.
    """
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Remove real credentials from the environment.

    Settings reads a developer's actual ``.env``, so tests that assert on
    defaults must run against a clean environment to be reproducible on any
    machine, including CI.
    """
    for name in (
        "APP_ENV",
        "LOG_LEVEL",
        "JSON_LOGS",
        "LLM_PROVIDER",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "TAVILY_API_KEY",
        "DATABASE_URL",
        "JWT_SECRET",
        "LANGSMITH_API_KEY",
        "DEFAULT_DEPTH",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def settings(isolated_env: pytest.MonkeyPatch) -> Settings:
    """Settings built from defaults, ignoring any local ``.env`` file."""
    return Settings(_env_file=None)
