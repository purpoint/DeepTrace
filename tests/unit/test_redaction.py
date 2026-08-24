"""Tests for secret redaction, in logs and in the persisted trace.

Redaction has a failure mode in each direction, and both have already happened
here once.

Redacting too little is the obvious one: a credential reaches a log sink or a
database column and stays there.

Redacting too much is the one that bit this project. The key pattern matched
`input_tokens` because the name contains "token", which silently destroyed the
cost tracking sitting next to it -- a security control breaking an unrelated
feature, with nothing in the output to say so. So the allowlist is tested as
carefully as the denylist.
"""

from __future__ import annotations

import pytest

from core.observability.recorder import AgentRun, ToolCall
from core.redaction import REDACTED, redact_mapping, redact_text, redact_value


def an_agent_run(**overrides: object) -> AgentRun:
    defaults: dict[str, object] = {
        "agent": "planner",
        "provider": "google",
        "model": "gemini-3.7-flash",
        "prompt_name": "planner",
        "prompt_version": "v1",
    }
    return AgentRun(**{**defaults, **overrides})  # type: ignore[arg-type]


class TestSecretShapes:
    """Values that are credentials whatever field they arrive in.

    This layer is the one that matters. A caller who names a field `api_key`
    already knew it was sensitive; the leak that actually happens is a provider
    error message quoting the request that failed.
    """

    @pytest.mark.parametrize(
        "secret",
        [
            "sk-abcdefghijklmnopqrstuvwx",
            "sk-ant-abcdefghijklmnopqrstuvwx",
            "tvly-abcdefghijklmnopqrst",
            "ghp_abcdefghijklmnopqrstuvwxyz",
            "AIzaSyD1234567890abcdefghijklmnop",
        ],
    )
    def test_a_provider_key_is_removed_from_free_text(self, secret: str) -> None:
        redacted = redact_value(f"request failed with key {secret} attached")

        assert secret not in redacted
        assert REDACTED in redacted

    def test_url_credentials_are_removed(self) -> None:
        redacted = redact_value("could not connect to postgresql://admin:hunter2@db:5432/x")

        assert "hunter2" not in redacted

    def test_a_bearer_token_is_removed(self) -> None:
        """Arrived with authentication. A rejected token reaches an exception
        message as readily as a rejected password does."""
        redacted = redact_value("Authorization: Bearer abcdefghijklmnopqrstuvwxyz123")

        assert "abcdefghijklmnopqrstuvwxyz123" not in redacted

    def test_a_jwt_is_recognised_by_shape(self) -> None:
        """Three base64url segments after an `eyJ` header. Recognising it on
        sight is what catches the one that was logged by accident."""
        token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c3JfMSJ9.c2lnbmF0dXJl"

        assert token not in str(redact_value(f"token {token} was rejected"))

    def test_nested_structures_are_reached(self) -> None:
        redacted = redact_mapping({"outer": {"inner": ["tvly-abcdefghijklmnopqrst"]}})

        assert "tvly-" not in str(redacted)

    def test_ordinary_text_is_untouched(self) -> None:
        message = "the request timed out after 30 seconds"

        assert redact_value(message) == message


class TestTheAllowlist:
    """The direction that broke something the first time."""

    @pytest.mark.parametrize(
        "field",
        ["input_tokens", "output_tokens", "cached_tokens", "total_tokens", "max_tokens"],
    )
    def test_token_counts_are_not_redacted(self, field: str) -> None:
        """They contain "token" and are usage metrics. Redacting them destroys
        the cost tracking that observability depends on."""
        assert redact_mapping({field: 1234})[field] == 1234

    def test_an_actual_token_field_still_is(self) -> None:
        """The allowlist must not be so broad that it lets a real one through."""
        assert redact_mapping({"access_token": "abc"})["access_token"] == REDACTED

    @pytest.mark.parametrize(
        "field", ["api_key", "OPENAI_API_KEY", "X-Api-Key", "password", "secret", "cookie"]
    )
    def test_sensitive_field_names_are_redacted(self, field: str) -> None:
        assert redact_mapping({field: "value"})[field] == REDACTED


class TestTheTrace:
    """Redaction where the trace record is built, not where it is written.

    There are five recorders. Redacting in each is the "every call site
    remembers" pattern the logging processor exists to avoid, so it happens once
    in the record itself and a sixth recorder inherits it.
    """

    def test_a_model_error_is_redacted_before_it_can_be_stored(self) -> None:
        run = an_agent_run(
            error_message="401 from https://api.example.com?key=AIzaSyD1234567890abcdefghijklmnop"
        )

        assert "AIzaSy" not in (run.error_message or "")

    def test_tool_arguments_are_redacted(self) -> None:
        """`arguments` holds a URL or a search query. Both can carry a
        credential -- a signed URL, or a key someone pasted into a question."""
        call = ToolCall(
            tool="web_search",
            arguments={"query": "why is tvly-abcdefghijklmnopqrst rejected"},
        )

        assert "tvly-" not in str(call.arguments)

    def test_a_tool_error_is_redacted(self) -> None:
        call = ToolCall(tool="fetch_url", error_message="Bearer abcdefghijklmnopqrstuvwxyz123")

        assert "abcdefghijklmnopqrstuvwxyz123" not in (call.error_message or "")

    def test_metadata_is_redacted_too(self) -> None:
        call = ToolCall(tool="fetch_url", metadata={"api_key": "abc"})

        assert call.metadata["api_key"] == REDACTED

    def test_no_error_stays_none(self) -> None:
        """`None` and `[REDACTED]` mean different things -- "there was no error"
        against "there was one we will not show you" -- and collapsing them
        makes a successful call look censored."""
        assert ToolCall(tool="fetch_url").error_message is None
        assert redact_text(None) is None

    def test_the_useful_part_of_an_error_survives(self) -> None:
        """Redaction that removes the whole message leaves an operator with a
        failure and no cause, which is its own kind of outage."""
        call = ToolCall(
            tool="fetch_url",
            error_message="connection refused to postgresql://u:p@db:5432/x after 3 retries",
        )

        assert "connection refused" in (call.error_message or "")
        assert "after 3 retries" in (call.error_message or "")

    def test_token_counts_survive_a_trace_record(self) -> None:
        """The end-to-end version of the allowlist test: the bug was found in a
        record like this one, not in the pattern."""
        run = an_agent_run(input_tokens=1200, output_tokens=340)

        assert run.input_tokens == 1200
        assert run.total_tokens == 1540

    def test_agent_run_metadata_is_redacted_too(self) -> None:
        """Both records carry a free-form metadata dict, so both must clean it.
        Guarding one and not the other is the kind of gap that survives review
        precisely because the guarded one looks like proof."""
        run = an_agent_run(metadata={"api_key": "abc", "attempt": 2})

        assert run.metadata["api_key"] == REDACTED
        assert run.metadata["attempt"] == 2
