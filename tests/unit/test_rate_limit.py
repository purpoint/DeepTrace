"""Tests for client-side rate limiting.

The limiter exists because of a measured failure: a run over 43 sources issued
64 rate-limit errors and lost 19 sources' worth of evidence that had already
been searched for, fetched, and stored. These tests pin the properties that
prevent that, and they use small rates so the suite stays fast.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from core.llm.base import CompletionRequest, CompletionResult, Message, ModelTier, TokenUsage
from core.llm.client import LLMClient, ModelRouter
from core.llm.errors import LLMRateLimitError
from core.llm.rate_limit import NullRateLimiter, RateLimiter
from core.llm.retry import RetryPolicy
from core.observability.recorder import InMemoryRunRecorder
from core.prompts.registry import Prompt
from tests.fakes import FakeProvider

pytestmark = pytest.mark.unit


PROMPT = Prompt(
    name="probe",
    version="v1",
    system="s",
    user_template="$question",
    variables=frozenset({"question"}),
    tier=ModelTier.CHEAP,
)
ROUTER = ModelRouter("fake", "cheap-model", "strong-model", "embed-model")


class TestTokenBucket:
    async def test_burst_capacity_is_available_immediately(self) -> None:
        """Parallel work must be able to start without waiting a full period."""
        limiter = RateLimiter(600, burst=5)
        started = time.monotonic()

        for _ in range(5):
            await limiter.acquire()

        assert time.monotonic() - started < 0.05

    async def test_exceeding_the_burst_waits(self) -> None:
        """The point: a call queues instead of being rejected."""
        limiter = RateLimiter(600, burst=2)  # 10 per second sustained
        await limiter.acquire()
        await limiter.acquire()

        waited = await limiter.acquire()

        assert waited > 0

    async def test_waiting_is_proportional_to_the_configured_rate(self) -> None:
        limiter = RateLimiter(60, burst=1)  # one per second
        await limiter.acquire()

        started = time.monotonic()
        await limiter.acquire()
        elapsed = time.monotonic() - started

        assert 0.5 < elapsed < 2.0

    async def test_tokens_refill_over_time(self) -> None:
        limiter = RateLimiter(6000, burst=2)
        await limiter.acquire()
        await limiter.acquire()
        assert limiter.available_tokens < 1

        await asyncio.sleep(0.05)
        assert limiter.available_tokens >= 1

    async def test_concurrent_callers_share_one_budget(self) -> None:
        """A limiter per agent would multiply the provider's limit by the number
        of agents, which is the bug this exists to prevent."""
        limiter = RateLimiter(600, burst=3)
        started = time.monotonic()

        await asyncio.gather(*(limiter.acquire() for _ in range(6)))
        elapsed = time.monotonic() - started

        # Three went immediately; the other three waited for refill.
        assert elapsed > 0.15
        assert limiter.stats.acquired == 6

    async def test_the_lock_is_not_held_while_sleeping(self) -> None:
        """Holding it across the sleep would serialise every caller into one
        queue and destroy the burst capacity the bucket provides."""
        limiter = RateLimiter(600, burst=4)
        started = time.monotonic()

        await asyncio.gather(*(limiter.acquire() for _ in range(4)))

        assert time.monotonic() - started < 0.05


class TestProviderFeedback:
    def test_a_rate_limit_pauses_the_whole_bucket(self) -> None:
        """A 429 is information about the shared account budget, not just about
        the request that received it."""
        limiter = RateLimiter(600, burst=5)
        limiter.penalise(0.2)

        assert limiter.available_tokens == 0
        assert limiter.stats.penalties == 1

    async def test_callers_wait_out_the_penalty(self) -> None:
        limiter = RateLimiter(6000, burst=10)
        limiter.penalise(0.2)

        started = time.monotonic()
        await limiter.acquire()

        assert time.monotonic() - started >= 0.15

    def test_penalties_do_not_shorten_each_other(self) -> None:
        """A second 429 during a pause must not reduce the wait."""
        limiter = RateLimiter(600)
        limiter.penalise(10.0)
        first = limiter._paused_until
        limiter.penalise(0.1)

        assert limiter._paused_until == first

    def test_the_bucket_is_emptied_by_a_penalty(self) -> None:
        """Otherwise the pause is followed immediately by a burst of the tokens
        that accumulated while waiting -- straight back into the limit."""
        limiter = RateLimiter(600, burst=10)
        limiter.penalise(0.01)

        assert limiter.available_tokens == 0


class TestDisabling:
    async def test_a_zero_rate_disables_limiting(self) -> None:
        limiter = RateLimiter(0)
        assert limiter.enabled is False
        assert await limiter.acquire() == 0.0

    async def test_the_null_limiter_never_waits(self) -> None:
        """Lets the client call the limiter unconditionally instead of guarding
        every call site."""
        limiter = NullRateLimiter()
        assert await limiter.acquire() == 0.0
        limiter.penalise(100.0)  # must not raise


class TestClientIntegration:
    def test_a_directly_constructed_client_does_not_wait(self) -> None:
        """Tests build clients directly, and silently introducing waiting would
        make the suite slow for no reason."""
        client = LLMClient(FakeProvider(["{}"]), router=ROUTER)
        assert client.rate_limiter.enabled is False

    async def test_every_provider_call_passes_through_the_limiter(self) -> None:
        limiter = RateLimiter(6000, burst=10)
        client = LLMClient(FakeProvider(["ok", "ok"]), router=ROUTER, rate_limiter=limiter)

        await client.complete(PROMPT, {"question": "a"})
        await client.complete(PROMPT, {"question": "b"})

        assert limiter.stats.acquired == 2

    async def test_a_rate_limit_error_penalises_the_shared_bucket(self) -> None:
        """Retrying only the failed call would leave every other in-flight
        caller pushing at the rate that caused the limit."""
        limiter = RateLimiter(6000, burst=10)
        client = LLMClient(
            FakeProvider([LLMRateLimitError("429", retry_after=0.05), "ok"]),
            router=ROUTER,
            rate_limiter=limiter,
            retry_policy=RetryPolicy(max_attempts=2, initial_delay_seconds=0.001, jitter=0.0),
        )

        await client.complete(PROMPT, {"question": "a"})

        assert limiter.stats.penalties == 1

    async def test_the_repair_path_is_also_limited(self) -> None:
        """Repair traffic spikes exactly when a run is already under pressure,
        so a path that skipped the limiter would defeat it."""
        from pydantic import BaseModel

        class Shape(BaseModel):
            value: str

        limiter = RateLimiter(6000, burst=10)
        recorder = InMemoryRunRecorder()
        client = LLMClient(
            FakeProvider(['{"wrong": 1}', '{"value": "ok"}']),
            router=ROUTER,
            recorder=recorder,
            rate_limiter=limiter,
        )

        await client.complete_structured(PROMPT, Shape, {"question": "a"})

        assert limiter.stats.acquired == 2  # original call plus the repair


class TestStats:
    async def test_waiting_is_measured(self) -> None:
        """Time spent waiting is part of a run's latency and belongs in the
        record, not hidden inside the client."""
        limiter = RateLimiter(600, burst=1)
        await limiter.acquire()
        await limiter.acquire()

        assert limiter.stats.waited == 1
        assert limiter.stats.total_wait_seconds > 0
        assert limiter.stats.mean_wait_seconds > 0

    def test_mean_wait_is_zero_when_nothing_waited(self) -> None:
        assert RateLimiter(600).stats.mean_wait_seconds == 0.0


def _request() -> CompletionRequest:
    return CompletionRequest(messages=(Message.user("hi"),), model="cheap-model")


def _result() -> CompletionResult:
    return CompletionResult(
        text="ok", model="m", provider="fake", usage=TokenUsage(), latency_ms=1.0
    )
