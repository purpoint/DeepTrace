"""The tool interface.

Tools are the only path from DeepTrace to the outside world. The rule that keeps
the system explainable is that **tools do not reason and agents do not fetch**: a
tool takes structured input, performs one external action, and returns structured
output plus a trace event. It never decides what to do next, never calls a model,
and never interprets what it retrieved.

That separation is what makes a research run reproducible. Every external effect
is a recorded tool call with known inputs, so a run can be explained by reading
its calls rather than inferring what an agent might have done.

Everything a tool returns is untrusted. Search results and page content are
attacker-influenced, so they are data to be analysed, never instructions to
follow -- see :func:`core.prompts.registry.wrap_untrusted`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Generic, TypeVar

from core.logging import get_logger
from core.observability.recorder import RunRecorder, ToolCall

log = get_logger(__name__)

T = TypeVar("T")


class ToolError(Exception):
    """Base class for tool failures.

    A failed tool call is part of the research trace, not an absence of one. A
    source that could not be fetched explains a gap in the evidence; silently
    dropping it leaves the gap unaccounted for.
    """

    retryable: bool = False

    transient: bool = False
    """Whether the provider could not be reached, rather than answering badly.

    The same distinction the LLM errors draw: read by the workflow to tell a
    step that is still owed from one that failed. A 404 is a fact about that
    URL; a search provider being down is a fact about right now."""

    def __init__(self, message: str, *, tool: str | None = None, retryable: bool | None = None):
        super().__init__(message)
        self.message = message
        self.tool = tool
        if retryable is not None:
            self.retryable = retryable


class ToolTimeoutError(ToolError):
    retryable = True
    transient = True


class ToolRateLimitError(ToolError):
    retryable = True
    transient = True


class ToolUnavailableError(ToolError):
    """The provider is down or unreachable."""

    retryable = True
    transient = True


class ToolConfigurationError(ToolError):
    """A required credential or setting is missing. Not retryable."""

    retryable = False


class SourceFetchError(ToolError):
    """A specific URL could not be retrieved.

    Not retryable by default: most causes are permanent for that URL -- it was
    blocked by validation, returned 404, or served something unparseable.
    Transient network failures are raised as ToolUnavailableError instead.
    """

    retryable = False

    def __init__(self, url: str, reason: str, *, status_code: int | None = None) -> None:
        super().__init__(f"Could not fetch {url}: {reason}", tool="fetch_url")
        self.url = url
        self.reason = reason
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ToolOutcome(Generic[T]):
    """The result of one tool call, with the metadata the trace needs."""

    value: T
    tool: str
    latency_ms: float
    cache_hit: bool = False
    result_count: int | None = None
    result_bytes: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ToolRun:
    """Times a tool call and records it, whether it succeeds or fails.

    Used as a context manager so the recording cannot be forgotten on the error
    path, which is exactly the path worth recording:

        with ToolRun("web_search", recorder, research_id=rid) as run:
            results = await provider.search(query)
            run.result_count = len(results)
    """

    def __init__(
        self,
        tool: str,
        recorder: RunRecorder | None = None,
        *,
        research_id: str | None = None,
        task_id: str | None = None,
        agent: str | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> None:
        self.tool = tool
        self.recorder = recorder
        self.research_id = research_id
        self.task_id = task_id
        self.agent = agent
        self.arguments = arguments or {}

        self.result_count: int | None = None
        self.result_bytes: int | None = None
        self.cache_hit = False
        self.retry_count = 0
        self._started = 0.0
        self._started_at = datetime.now(UTC)

    def __enter__(self) -> ToolRun:
        self._started = time.perf_counter()
        self._started_at = datetime.now(UTC)
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: object
    ) -> None:
        latency_ms = (time.perf_counter() - self._started) * 1000

        if self.recorder is not None:
            self.recorder.record_tool_call(
                ToolCall(
                    tool=self.tool,
                    research_id=self.research_id,
                    task_id=self.task_id,
                    agent=self.agent,
                    arguments=self.arguments,
                    latency_ms=latency_ms,
                    status="success" if exc is None else "error",
                    error_type=type(exc).__name__ if exc else None,
                    error_message=str(exc) if exc else None,
                    retry_count=self.retry_count,
                    result_count=self.result_count,
                    result_bytes=self.result_bytes,
                    cache_hit=self.cache_hit,
                    started_at=self._started_at,
                    completed_at=datetime.now(UTC),
                )
            )

        if exc is None:
            log.info(
                f"tool.{self.tool}",
                research_id=self.research_id,
                task_id=self.task_id,
                latency_ms=round(latency_ms, 1),
                result_count=self.result_count,
                cache_hit=self.cache_hit,
            )
        else:
            log.warning(
                f"tool.{self.tool}.failed",
                research_id=self.research_id,
                task_id=self.task_id,
                latency_ms=round(latency_ms, 1),
                error_type=type(exc).__name__,
            )

    @property
    def latency_ms(self) -> float:
        return (time.perf_counter() - self._started) * 1000
