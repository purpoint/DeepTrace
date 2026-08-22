"""Client-side request rate limiting.

Measured problem this solves. A research run over 43 sources issued 64 rate-limit
errors against a free-tier provider and lost 19 sources' worth of evidence --
sources that had already been searched for, fetched, scored, and stored. The cost
was paid and the value was thrown away.

Per-agent concurrency bounds did not prevent it. Each agent limited its own
in-flight calls, but nothing limited the *total* request rate across agents, and
the provider's limit applies to the whole account.

A token bucket shapes traffic instead of failing it: a call waits its turn rather
than being rejected and retried. Waiting is cheaper than a failed round trip, and
far cheaper than the work that gets discarded when the retry budget runs out.

Two properties matter beyond the basic algorithm.

*It is shared.* One limiter per provider, held by the client, so ten concurrent
agents draw from one budget. A limiter per agent would multiply the limit by the
number of agents, which is the bug it exists to prevent.

*It reacts to the provider.* A 429 is information about the shared budget, not
just about the request that received it. When one arrives, the whole bucket
pauses, so every other in-flight caller backs off too. Retrying only the failed
call leaves the rest of the fleet pushing at the same rate that caused it.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from core.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class RateLimitStats:
    """What the limiter did, for the run's cost and latency accounting."""

    acquired: int = 0
    waited: int = 0
    total_wait_seconds: float = 0.0
    penalties: int = 0

    @property
    def mean_wait_seconds(self) -> float:
        return round(self.total_wait_seconds / self.waited, 3) if self.waited else 0.0


class RateLimiter:
    """An async token bucket, shared across every caller of one provider.

    Tokens refill continuously at ``requests_per_minute / 60`` per second. A call
    that finds the bucket empty sleeps for exactly as long as the next token
    needs, rather than polling.
    """

    def __init__(
        self,
        requests_per_minute: float,
        *,
        burst: int | None = None,
        name: str = "llm",
    ) -> None:
        """Args:
        requests_per_minute: Sustained rate. Zero or negative disables limiting.
        burst: How many requests may go out back to back before the sustained
            rate applies. Defaults to a quarter of a minute's allowance, which
            lets a short burst of parallel work start immediately without
            spending the whole minute's budget at once.
        name: Included in log events, so two providers are distinguishable.
        """
        self.requests_per_minute = requests_per_minute
        self.enabled = requests_per_minute > 0
        self.capacity = float(burst if burst is not None else max(1, int(requests_per_minute / 4)))
        self.name = name

        self._tokens = self.capacity
        self._refill_per_second = requests_per_minute / 60.0
        self._updated = time.monotonic()
        self._paused_until = 0.0
        self._lock = asyncio.Lock()
        self.stats = RateLimitStats()

    def _refill(self, now: float) -> None:
        """Add tokens for elapsed time.

        ``elapsed`` is clamped at zero because a penalty sets ``_updated`` to the
        end of the pause, making it negative until then. That is what stops
        tokens accruing while the bucket is paused.
        """
        elapsed = max(0.0, now - self._updated)
        if elapsed:
            self._updated = now
            self._tokens = min(self.capacity, self._tokens + elapsed * self._refill_per_second)

    async def acquire(self) -> float:
        """Wait until a request may be sent. Returns how long it waited.

        The lock is held only while computing the wait, not while sleeping.
        Holding it across the sleep would serialise every caller into a single
        queue and destroy the burst capacity the bucket exists to provide.
        """
        if not self.enabled:
            return 0.0

        waited = 0.0
        while True:
            async with self._lock:
                now = time.monotonic()
                self._refill(now)

                if now < self._paused_until:
                    delay = self._paused_until - now
                elif self._tokens >= 1.0:
                    self._tokens -= 1.0
                    self.stats.acquired += 1
                    if waited > 0:
                        self.stats.waited += 1
                        self.stats.total_wait_seconds += waited
                    return waited
                else:
                    delay = (1.0 - self._tokens) / self._refill_per_second

            waited += delay
            await asyncio.sleep(delay)

    def penalise(self, seconds: float) -> None:
        """Pause the bucket after the provider reported a rate limit.

        Applies to every caller, not just the one that was rejected. A 429 says
        the shared budget is exhausted, so backing off only the failed request
        leaves the rest of the fleet pushing at the rate that caused it.

        The bucket is emptied and refilling is deferred to the end of the pause.
        Without the second part, a long penalty would end with a full bucket and
        the recovery would be a burst straight back into the limit -- which is
        the behaviour that caused the penalty in the first place.
        """
        if not self.enabled:
            return

        now = time.monotonic()
        self._paused_until = max(self._paused_until, now + max(0.0, seconds))
        self._tokens = 0.0
        self._updated = self._paused_until
        self.stats.penalties += 1

        log.warning(
            "rate_limit.penalised",
            limiter=self.name,
            pause_seconds=round(seconds, 2),
            total_penalties=self.stats.penalties,
        )

    @property
    def available_tokens(self) -> float:
        """Approximate tokens available now. For tests and diagnostics."""
        self._refill(time.monotonic())
        return self._tokens


class NullRateLimiter:
    """Never waits. Used where limiting is disabled, so callers need no branch."""

    enabled = False

    def __init__(self) -> None:
        self.stats = RateLimitStats()

    async def acquire(self) -> float:
        return 0.0

    def penalise(self, seconds: float) -> None:  # noqa: ARG002
        return None
