"""Tests for the Gemini adapter.

No network. A stub stands in for the SDK client, because what needs testing is
the translation in both directions: DeepTrace request to Gemini call, and Gemini
response or exception back to DeepTrace types.

Error translation gets the most attention here. The typed error the adapter
chooses decides whether the retry policy will try again, so a misclassification
either wastes attempts on a failure that cannot recover or gives up on one that
would have.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from core.llm.base import CompletionRequest, Message
from core.llm.errors import (
    LLMAuthenticationError,
    LLMBadRequestError,
    LLMConnectionError,
    LLMContentFilterError,
    LLMError,
    LLMRateLimitError,
    LLMServerError,
    LLMTimeoutError,
)
from core.llm.gemini import GeminiProvider, _split_system, _translate_error, _usage_from

pytestmark = pytest.mark.unit


class StubModels:
    """Stands in for ``client.aio.models``."""

    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response

    async def embed_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


def stub_client(response: Any = None, error: Exception | None = None) -> Any:
    models = StubModels(response, error)
    return SimpleNamespace(aio=SimpleNamespace(models=models))


def gemini_response(
    text: str = "hello",
    *,
    finish_reason: str = "STOP",
    prompt_tokens: int = 900,
    output_tokens: int = 120,
    cached_tokens: int = 0,
) -> Any:
    return SimpleNamespace(
        text=text,
        candidates=[
            SimpleNamespace(
                finish_reason=SimpleNamespace(name=finish_reason),
                content=SimpleNamespace(parts=[SimpleNamespace(text=text)]),
            )
        ],
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt_tokens,
            candidates_token_count=output_tokens,
            cached_content_token_count=cached_tokens,
            thoughts_token_count=0,
        ),
        response_id="resp_123",
    )


def request(**kwargs: Any) -> CompletionRequest:
    defaults: dict[str, Any] = {
        "messages": (Message.system("You plan research."), Message.user("Plan for X")),
        "model": "gemini-3.5-flash-lite",
    }
    defaults.update(kwargs)
    return CompletionRequest(**defaults)


class ApiError(Exception):
    """Mimics the SDK's APIError shape without importing its constructor."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@pytest.fixture(autouse=True)
def _patch_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the adapter's isinstance check at the local stand-in."""
    import core.llm.gemini as gemini_module

    monkeypatch.setattr(gemini_module.genai_errors, "APIError", ApiError)


class TestRequestTranslation:
    def test_system_message_becomes_system_instruction(self) -> None:
        """Gemini takes the system prompt as a separate field rather than as a
        message with a system role. That quirk stops at this boundary."""
        system, contents = _split_system(
            (Message.system("You plan research."), Message.user("Plan for X"))
        )

        assert system == "You plan research."
        assert len(contents) == 1

    def test_assistant_role_is_renamed_to_model(self) -> None:
        _, contents = _split_system((Message.assistant("previous answer"),))
        assert contents[0].role == "model"

    def test_multiple_system_messages_are_joined(self) -> None:
        system, _ = _split_system((Message.system("first"), Message.system("second")))
        assert system == "first\n\nsecond"

    def test_no_system_message_yields_none(self) -> None:
        system, contents = _split_system((Message.user("just a question"),))
        assert system is None
        assert len(contents) == 1

    async def test_schema_is_sent_when_the_model_supports_it(self) -> None:
        client = stub_client(gemini_response('{"ok": true}'))
        provider = GeminiProvider(api_key="test", client=client)

        await provider.complete(request(response_schema={"type": "object"}))

        config = client.aio.models.calls[0]["config"]
        assert config.response_mime_type == "application/json"
        assert config.response_schema == {"type": "object"}

    async def test_timeout_is_converted_to_milliseconds(self) -> None:
        """The SDK expects milliseconds; passing seconds would make a 30 second
        timeout fire after 30 milliseconds."""
        client = stub_client(gemini_response())
        provider = GeminiProvider(api_key="test", client=client)

        await provider.complete(request(timeout_seconds=30.0))

        assert client.aio.models.calls[0]["config"].http_options.timeout == 30_000


class TestResponseTranslation:
    async def test_usage_and_latency_are_populated(self) -> None:
        """Both are required: without them the run recorder has nothing to
        record and cost cannot be computed."""
        provider = GeminiProvider(api_key="test", client=stub_client(gemini_response()))
        result = await provider.complete(request())

        assert result.usage.input_tokens == 900
        assert result.usage.output_tokens == 120
        assert result.latency_ms > 0
        assert result.provider == "google"

    async def test_cached_tokens_are_carried_through(self) -> None:
        provider = GeminiProvider(
            api_key="test", client=stub_client(gemini_response(cached_tokens=400))
        )
        result = await provider.complete(request())

        assert result.usage.cached_tokens == 400
        assert result.usage.billable_input_tokens == 500

    async def test_truncation_is_reported(self) -> None:
        provider = GeminiProvider(
            api_key="test", client=stub_client(gemini_response(finish_reason="MAX_TOKENS"))
        )
        result = await provider.complete(request())

        assert result.was_truncated is True

    def test_missing_usage_metadata_yields_zeros(self) -> None:
        """A missing usage field should not fail a completion that otherwise
        succeeded, though it shows up as a suspiciously free call."""
        assert _usage_from(SimpleNamespace()).total_tokens == 0

    async def test_text_is_recovered_when_the_accessor_raises(self) -> None:
        """A blocked or empty candidate can make the SDK's convenience accessor
        throw; the parts are still readable."""

        class Exploding:
            @property
            def text(self) -> str:
                raise ValueError("no candidates")

            candidates: ClassVar[list[Any]] = [
                SimpleNamespace(
                    finish_reason=SimpleNamespace(name="STOP"),
                    content=SimpleNamespace(parts=[SimpleNamespace(text="recovered")]),
                )
            ]
            usage_metadata = None

        provider = GeminiProvider(api_key="test", client=stub_client(Exploding()))
        result = await provider.complete(request())

        assert result.text == "recovered"


class TestErrorTranslation:
    """The chosen error type decides whether the retry policy tries again."""

    @pytest.mark.parametrize(
        ("code", "expected", "retryable"),
        [
            (429, LLMRateLimitError, True),
            (500, LLMServerError, True),
            (503, LLMServerError, True),
            (401, LLMAuthenticationError, False),
            (403, LLMAuthenticationError, False),
            (400, LLMBadRequestError, False),
        ],
    )
    def test_status_codes_map_to_typed_errors(
        self, code: int, expected: type[LLMError], retryable: bool
    ) -> None:
        error = _translate_error(ApiError(code, "boom"), "gemini-3.5-flash-lite")

        assert isinstance(error, expected)
        assert error.retryable is retryable
        assert error.provider == "google"

    def test_timeout_maps_to_a_retryable_error(self) -> None:
        error = _translate_error(TimeoutError("slow"), "gemini-3.5-flash-lite")
        assert isinstance(error, LLMTimeoutError)
        assert error.retryable is True

    def test_connection_failure_is_retryable(self) -> None:
        error = _translate_error(ConnectionError("dns"), "gemini-3.5-flash-lite")
        assert isinstance(error, LLMConnectionError)
        assert error.retryable is True

    def test_unknown_failure_is_not_retried(self) -> None:
        """Retrying an unclassified failure risks repeating a request that
        already had an effect or already cost money."""
        error = _translate_error(ValueError("something odd"), "gemini-3.5-flash-lite")

        assert type(error) is LLMError
        assert error.retryable is False

    async def test_sdk_exceptions_do_not_leak_upward(self) -> None:
        provider = GeminiProvider(
            api_key="test", client=stub_client(error=ApiError(429, "slow down"))
        )

        with pytest.raises(LLMRateLimitError):
            await provider.complete(request())

    @pytest.mark.parametrize("reason", ["SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "RECITATION"])
    async def test_refusal_is_not_retryable(self, reason: str) -> None:
        """Retrying a refusal until something slips through is the wrong
        behaviour; it should be recorded and degraded from."""
        provider = GeminiProvider(
            api_key="test",
            client=stub_client(gemini_response("", finish_reason=reason)),
        )

        with pytest.raises(LLMContentFilterError) as exc:
            await provider.complete(request())
        assert exc.value.retryable is False


class TestCapabilityReporting:
    def test_a_future_model_generation_is_still_supported(self) -> None:
        """Regression test. Capability was once an allowlist of version
        prefixes, so a new provider generation silently reported
        "unsupported" and the client degraded to prompt-level JSON with repair
        loops -- slower, more expensive, and easy to miss."""
        provider = GeminiProvider(api_key="test", client=stub_client())

        assert provider.supports_structured_output("gemini-9.9-flash") is True

    @pytest.mark.parametrize(
        ("model", "supported"),
        [
            ("gemini-3.5-flash-lite", True),
            ("gemini-3.7-flash", True),
            ("gemini-1.5-pro", True),
            ("models/gemini-3.7-flash", True),
            ("gemini-embedding-001", False),
            ("veo-3.1-generate-preview", False),
            ("some-other-model", False),
        ],
    )
    def test_structured_output_support(self, model: str, supported: bool) -> None:
        """When False, the client falls back to prompt-level instruction plus
        validation, which is less reliable and worth knowing about."""
        provider = GeminiProvider(api_key="test", client=stub_client())
        assert provider.supports_structured_output(model) is supported
