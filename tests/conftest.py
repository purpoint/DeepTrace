"""Shared pytest fixtures.

Two pieces of global state can leak between tests and produce failures that
depend on execution order: the cached settings singleton and structlog's
context variables. Both are reset automatically for every test.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterator
from typing import Any

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
        # And the file-backed form of the same setting. A developer with
        # JWT_SECRET_FILE exported would otherwise carry a real signing key
        # into tests that assert a default -- the leak this fixture exists to
        # prevent, arriving through the door added after it was written.
        monkeypatch.delenv(f"{name}_FILE", raising=False)
    return monkeypatch


@pytest.fixture
def settings(isolated_env: pytest.MonkeyPatch) -> Settings:
    """Settings built from defaults, ignoring any local ``.env`` file."""
    return Settings(_env_file=None)


@pytest.fixture
def stub_dns(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Make hostname resolution hermetic.

    The URL guard resolves every hostname before allowing a fetch, so any test
    exercising code above the guard would otherwise depend on real DNS. That
    makes the suite slow, dependent on a network, and dependent on domains
    staying registered. It also silently changes what a test measures: an
    invented hostname fails to resolve, the guard blocks it, and the test passes
    or fails for a reason that has nothing to do with what it was written for.

    IP literals pass through unchanged, so loopback and metadata addresses stay
    blocked -- the stub replaces the name lookup, never the safety decision.

    Usage::

        stub_dns()                                  # every name is public
        stub_dns({"evil.test": "127.0.0.1"})        # this one is loopback
        stub_dns({"gone.test": None})               # this one does not resolve
    """

    def install(
        mapping: dict[str, str | None] | None = None,
        *,
        default: str = "93.184.216.34",
    ) -> None:
        resolved = mapping or {}

        def fake_getaddrinfo(host: str, port: object, *args: object, **kwargs: object) -> list[Any]:
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                # An IP literal is not a name lookup. Returning it unchanged
                # keeps every address-based check under test.
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port or 0))]

            if host in resolved and resolved[host] is None:
                raise socket.gaierror(8, "nodename nor servname provided, or not known")

            address = resolved.get(host) or default
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 0))]

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    return install
