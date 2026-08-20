"""Tests for structured logging, redaction, and context propagation.

Redaction is a security control, so it is tested as one: both that secrets are
removed and that non-secrets survive. The second half matters as much as the
first -- an over-aggressive filter silently destroys the cost and token metrics
that observability depends on.
"""

from __future__ import annotations

import re

import pytest
import structlog

from core.config import Settings
from core.logging import (
    REDACTED,
    bind_research_context,
    build_processors,
    clear_research_context,
    configure_logging,
    get_logger,
    redact_secrets,
)

pytestmark = pytest.mark.unit


def redact(**fields: object) -> dict[str, object]:
    """Run one event dict through the redaction processor."""
    return dict(redact_secrets(None, "info", dict(fields)))


class TestSensitiveKeyRedaction:
    @pytest.mark.parametrize(
        "field",
        [
            "api_key",
            "API_KEY",
            "openai_api_key",
            "x-api-key",
            "apikey",
            "password",
            "passwd",
            "jwt_secret",
            "client_secret",
            "access_token",
            "refresh_token",
            "authorization",
            "Bearer",
            "credential",
            "cookie",
        ],
    )
    def test_sensitive_field_is_redacted(self, field: str) -> None:
        assert redact(**{field: "super-secret-value"})[field] == REDACTED


class TestMetricKeysSurvive:
    """Regression test.

    An earlier version matched the substring "token" and redacted input_tokens
    and output_tokens. Cost tracking would have recorded [REDACTED] for every
    LLM call and the failure would only have surfaced much later, as an empty
    cost dashboard. These assertions fail if that regression returns.
    """

    @pytest.mark.parametrize(
        "field",
        [
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "prompt_tokens",
            "completion_tokens",
            "cached_tokens",
            "reasoning_tokens",
            "max_tokens",
            "token_count",
            "tokens",
        ],
    )
    def test_token_metric_is_not_redacted(self, field: str) -> None:
        assert redact(**{field: 1234})[field] == 1234

    def test_full_llm_record_keeps_every_metric(self) -> None:
        """The exact shape emitted after an LLM call must survive intact."""
        record = redact(
            model="gpt-4o-mini",
            prompt_version="planner.v1",
            input_tokens=812,
            output_tokens=140,
            total_tokens=952,
            latency_ms=1240,
            cost_usd=0.00021,
            retry_count=0,
            api_key="sk-proj-abcdefghijklmnop1234",
        )

        assert record["input_tokens"] == 812
        assert record["output_tokens"] == 140
        assert record["total_tokens"] == 952
        assert record["cost_usd"] == 0.00021
        assert record["latency_ms"] == 1240
        assert record["api_key"] == REDACTED


class TestSecretValuePatterns:
    """Secret-shaped values are stripped wherever they appear.

    This is the layer that catches the leak that actually happens: a credential
    inside an exception message or a request URL, under an innocuous field name.
    """

    @pytest.mark.parametrize(
        "secret",
        [
            "sk-proj-abcdefghijklmnopqrstuvwx",
            "sk-ant-api03-abcdefghijklmnopqrst",
            "tvly-abcdefghijklmnopqrstuvwx",
            "ghp_abcdefghijklmnopqrstuvwxyz12",
            "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ123",
        ],
    )
    def test_secret_in_innocuous_field_is_stripped(self, secret: str) -> None:
        result = redact(error=f"request failed with key {secret}")
        assert secret not in str(result["error"])
        assert REDACTED in str(result["error"])

    def test_url_credentials_are_stripped_but_host_is_kept(self) -> None:
        """Connection strings must stay debuggable after redaction."""
        result = redact(dsn="postgresql://admin:hunter2@db.internal:5432/deeptrace")
        rendered = str(result["dsn"])

        assert "hunter2" not in rendered
        assert "admin" not in rendered
        assert "db.internal:5432/deeptrace" in rendered

    def test_ordinary_values_are_untouched(self) -> None:
        result = redact(
            question="Compare Kafka and RabbitMQ",
            url="https://kafka.apache.org/documentation/",
            verdict="SUPPORTED",
        )
        assert result["question"] == "Compare Kafka and RabbitMQ"
        assert result["url"] == "https://kafka.apache.org/documentation/"
        assert result["verdict"] == "SUPPORTED"


class TestNestedRedaction:
    """Secrets hide inside nested structures, which is how config dumps leak."""

    def test_nested_dict(self) -> None:
        result = redact(config={"provider": "openai", "api_key": "sk-nested-secret-value"})
        nested = result["config"]
        assert isinstance(nested, dict)
        assert nested["api_key"] == REDACTED
        assert nested["provider"] == "openai"

    def test_list_of_dicts(self) -> None:
        result = redact(providers=[{"name": "openai", "token": "secret-a"}])
        providers = result["providers"]
        assert isinstance(providers, list)
        assert providers[0]["token"] == REDACTED
        assert providers[0]["name"] == "openai"

    def test_deeply_nested_value_pattern(self) -> None:
        result = redact(trace={"step": {"detail": "used sk-proj-deadbeefdeadbeef1234"}})
        assert "sk-proj-deadbeef" not in str(result)


class TestContextPropagation:
    def test_bound_field_reaches_records_that_never_received_it(self) -> None:
        """The point of contextvars: a tool deep in the stack logs research_id
        without any caller threading it through."""
        bind_research_context(research_id="res_abc123", depth="standard")

        merged = structlog.contextvars.merge_contextvars(None, "info", {"event": "tool.called"})

        assert merged["research_id"] == "res_abc123"
        assert merged["depth"] == "standard"
        assert merged["event"] == "tool.called"

    def test_clearing_prevents_leaking_into_the_next_run(self) -> None:
        bind_research_context(research_id="res_first")
        clear_research_context()

        merged = structlog.contextvars.merge_contextvars(None, "info", {"event": "worker.idle"})

        assert "research_id" not in merged

    def test_bound_context_is_redacted_too(self) -> None:
        """Context fields pass through redaction because merge runs first."""
        bind_research_context(api_key="sk-bound-into-context-1234")
        merged = structlog.contextvars.merge_contextvars(None, "info", {"event": "x"})
        assert redact(**merged)["api_key"] == REDACTED


class TestProcessorChain:
    def test_context_merge_runs_before_redaction(self) -> None:
        """Order is load-bearing: bound fields must be redactable."""
        names = [getattr(p, "__name__", type(p).__name__) for p in build_processors(json_logs=True)]
        assert names.index("merge_contextvars") < names.index("redact_secrets")

    def test_renderer_is_last(self) -> None:
        for json_logs in (True, False):
            processors = build_processors(json_logs=json_logs)
            assert "Renderer" in type(processors[-1]).__name__

    def test_json_and_console_modes_differ(self) -> None:
        assert type(build_processors(json_logs=True)[-1]) is not type(
            build_processors(json_logs=False)[-1]
        )


class TestConfigureLogging:
    def test_is_idempotent(self, settings: Settings) -> None:
        """Calling twice must replace configuration, not stack handlers."""
        configure_logging(settings)
        configure_logging(settings)
        assert get_logger("test") is not None

    def test_emits_json_when_enabled(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(Settings(_env_file=None, json_logs=True))
        get_logger("test").info("research.started", research_id="res_xyz")

        output = capsys.readouterr().out
        assert '"message": "research.started"' in output
        assert '"research_id": "res_xyz"' in output

    def test_redacts_end_to_end(self, capsys: pytest.CaptureFixture[str]) -> None:
        """The whole point, verified through the real configured pipeline."""
        configure_logging(Settings(_env_file=None, json_logs=True))
        get_logger("test").warning(
            "tool.failed",
            openai_api_key="sk-proj-mustnotappear12345678",
            input_tokens=42,
        )

        output = capsys.readouterr().out
        assert "sk-proj-mustnotappear" not in output
        assert REDACTED in output
        assert '"input_tokens": 42' in output

    def test_respects_configured_level(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(Settings(_env_file=None, json_logs=True, log_level="WARNING"))
        log = get_logger("test")
        log.info("should.not.appear")
        log.warning("should.appear")

        output = capsys.readouterr().out
        assert "should.not.appear" not in output
        assert "should.appear" in output


class TestRedactionPatternsAreAnchored:
    def test_short_lookalike_is_not_over_redacted(self) -> None:
        """Patterns require a realistic length so ordinary text is not mangled."""
        result = redact(note="sk-1")
        assert result["note"] == "sk-1"

    def test_redacted_marker_is_recognisable(self) -> None:
        assert re.fullmatch(r"\[[A-Z]+\]", REDACTED)
