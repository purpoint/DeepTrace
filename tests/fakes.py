"""Test doubles shared across the suite.

:class:`FakeProvider` is a full implementation of the LLM provider interface
written without importing any vendor SDK. That makes it useful twice: it lets
the suite exercise retry, repair, routing, and cost logic without network access
or spend, and it is standing evidence that the provider interface is genuinely
implementable by more than the one adapter that exists today.
"""

from __future__ import annotations

from collections.abc import Iterable

from core.llm.base import (
    CompletionRequest,
    CompletionResult,
    EmbeddingResult,
    TokenUsage,
)


class FakeProvider:
    """Returns queued responses in order, raising any that are exceptions.

    The final response repeats once the queue is exhausted, so a test asserting
    that retries are bounded does not have to supply one response per attempt.
    """

    def __init__(
        self,
        responses: Iterable[object],
        *,
        name: str = "fake",
        input_tokens: int = 900,
        output_tokens: int = 120,
        latency_ms: float = 42.0,
        structured_output: bool = True,
    ) -> None:
        self._responses = list(responses) or [""]
        self._name = name
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self._latency_ms = latency_ms
        self._structured_output = structured_output
        self.calls = 0
        self.requests: list[CompletionRequest] = []

    @property
    def name(self) -> str:
        return self._name

    def supports_structured_output(self, model: str) -> bool:
        return self._structured_output

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.requests.append(request)
        response = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1

        if isinstance(response, BaseException):
            raise response

        return CompletionResult(
            text=str(response),
            model=request.model,
            provider=self._name,
            usage=TokenUsage(input_tokens=self._input_tokens, output_tokens=self._output_tokens),
            latency_ms=self._latency_ms,
        )

    async def embed(
        self,
        texts: tuple[str, ...],
        *,
        model: str,
        timeout_seconds: float = 60.0,
    ) -> EmbeddingResult:
        return EmbeddingResult(
            vectors=tuple((0.1, 0.2, 0.3) for _ in texts),
            model=model,
            provider=self._name,
            usage=TokenUsage(input_tokens=len(texts) * 10),
            latency_ms=self._latency_ms,
        )


class SchemaRoutedProvider(FakeProvider):
    """Answers according to what was asked for, not the order it was asked.

    :class:`FakeProvider` returns queued responses in sequence, which is exact
    and readable while one caller works at a time. Once research tasks run
    concurrently the order stops being deterministic: three researchers
    interleave, and a queue hands the second task's answer to the third. The
    test then fails on the double rather than on the code.

    Routing on ``schema_name`` -- which the client sets to the name of the prompt
    it is running -- makes the double indifferent to interleaving, so a
    concurrency test measures concurrency.
    """

    def __init__(
        self,
        by_schema: dict[str, object],
        *,
        default: object = "",
        delay_seconds: float = 0.0,
        **kwargs: object,
    ) -> None:
        super().__init__([default], **kwargs)  # type: ignore[arg-type]
        self.by_schema = by_schema
        self.delay_seconds = delay_seconds

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.requests.append(request)
        self.calls += 1

        if self.delay_seconds:
            import asyncio

            await asyncio.sleep(self.delay_seconds)

        response = self.by_schema.get(request.schema_name, self._responses[0])
        if isinstance(response, BaseException):
            raise response

        return CompletionResult(
            text=str(response),
            model=request.model,
            provider=self.name,
            usage=TokenUsage(input_tokens=self._input_tokens, output_tokens=self._output_tokens),
            latency_ms=self._latency_ms,
        )
