"""Retry policy for LLM calls.

Written once, above the provider interface, so every vendor gets identical
behaviour. The policy asks the error whether it is retryable rather than
inspecting exception types, which is why adding a provider does not mean
revisiting this file.

Three properties matter:

*Bounded.* A fixed maximum number of attempts and a ceiling on total delay. An
unbounded retry loop against a paid API is a way to spend money producing
nothing.

*Jittered.* Concurrent research tasks fail together when a rate limit is hit.
Without jitter they would all retry at the same instant and re-trigger it --
the thundering herd. Randomising the delay spreads them out.

*Deferential.* When a provider says ``retry_after``, that value wins over the
computed backoff. The server knows its own limits better than the client does.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from core.llm.errors import LLMError, LLMRateLimitError
from core.logging import get_logger

log = get_logger(__name__)

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How many times to retry, and how long to wait between attempts."""

    max_attempts: int = 3
    """Total attempts including the first. 3 means one call and two retries."""

    initial_delay_seconds: float = 1.0
    backoff_multiplier: float = 2.0
    max_delay_seconds: float = 30.0
    """Ceiling on any single wait. Free-tier rate limits can suggest very long
    waits; blocking a research run for minutes is worse than failing it."""

    max_total_delay_seconds: float = 60.0
    """Ceiling on cumulative waiting across all retries for one call."""

    jitter: float = 0.25
    """Fraction of the delay randomised, in both directions."""

    def delay_for(self, attempt: int, *, retry_after: float | None = None) -> float:
        """Delay before the given attempt number, where the first retry is 1.

        A provider-supplied ``retry_after`` overrides the computed backoff but is
        still clamped, so a provider asking for a five-minute wait fails fast
        instead of stalling the run.
        """
        if retry_after is not None:
            return min(max(retry_after, 0.0), self.max_delay_seconds)

        raw = self.initial_delay_seconds * (self.backoff_multiplier ** max(attempt - 1, 0))
        capped = min(raw, self.max_delay_seconds)
        spread = capped * self.jitter
        return max(0.0, capped + random.uniform(-spread, spread))  # noqa: S311 - not cryptographic


DEFAULT_POLICY = RetryPolicy()


async def with_retries(
    operation: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy = DEFAULT_POLICY,
    operation_name: str = "llm.call",
    on_retry: Callable[[int, LLMError, float], None] | None = None,
) -> T:
    """Run an async operation, retrying only failures that could succeed.

    Args:
        operation: Zero-argument coroutine factory. It is called fresh on each
            attempt so the request is rebuilt rather than reused.
        policy: Attempt and delay limits.
        operation_name: Used in log events.
        on_retry: Called before each wait with attempt number, error, and delay.
            Used to record the retry in the run log.

    Raises:
        LLMError: The last error, once attempts or the delay budget run out.
            The original is re-raised rather than wrapped, so the caller sees
            the real failure instead of a generic "retries exhausted".
    """
    total_delay = 0.0
    last_error: LLMError | None = None

    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await operation()
        except LLMError as exc:
            last_error = exc

            if not exc.retryable:
                log.warning(
                    f"{operation_name}.failed",
                    error_type=type(exc).__name__,
                    attempt=attempt,
                    retryable=False,
                )
                raise

            if attempt == policy.max_attempts:
                log.warning(
                    f"{operation_name}.exhausted",
                    error_type=type(exc).__name__,
                    attempts=attempt,
                )
                raise

            retry_after = getattr(exc, "retry_after", None)
            delay = policy.delay_for(attempt, retry_after=retry_after)

            if total_delay + delay > policy.max_total_delay_seconds:
                log.warning(
                    f"{operation_name}.delay_budget_exceeded",
                    error_type=type(exc).__name__,
                    attempts=attempt,
                    total_delay_seconds=round(total_delay, 2),
                )
                raise

            total_delay += delay
            log.info(
                f"{operation_name}.retrying",
                error_type=type(exc).__name__,
                attempt=attempt,
                next_attempt=attempt + 1,
                delay_seconds=round(delay, 2),
                rate_limited=isinstance(exc, LLMRateLimitError),
            )
            if on_retry is not None:
                on_retry(attempt, exc, delay)

            await asyncio.sleep(delay)

    # Unreachable: the loop either returns or raises.
    raise last_error if last_error else LLMError(f"{operation_name} failed without an error")
