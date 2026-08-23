"""Refusing too many requests, counted across every process.

Not to be confused with :mod:`core.llm.rate_limit`, which is the mirror image
of this. That one shapes *outbound* traffic to stay under a provider's quota,
and it makes callers wait. This one rejects *inbound* traffic that is over ours,
and it makes callers stop. Waiting is right when the work is already paid for
and the limit is someone else's; rejecting is right when the request has cost
nothing yet and the limit is ours to enforce.

**A sliding window, not a fixed one.** The cheap implementation is a counter per
clock-aligned window, and its flaw is at the boundary: a limit of ten per
fifteen minutes permits twenty requests in one second, ten at 14:59 and ten at
15:00. A log of timestamps in a sorted set has no boundary, because the window
is measured from now rather than from the top of the hour.

**Rejected requests are not counted.** A limiter that records the attempts it
turned away pushes its own window forward every time a client retries, so a
client that keeps trying can never return -- and every ``Retry-After`` it was
sent was a lie. Not counting them makes the limit exactly what it says: ten
served requests per fifteen minutes, and a client that waits the advertised time
gets in.
"""

from __future__ import annotations

import math
import secrets
import time
from dataclasses import dataclass

from redis.asyncio import Redis


@dataclass(frozen=True, slots=True)
class Decision:
    """Whether one request may proceed, and what to tell the client if not."""

    allowed: bool
    limit: int
    remaining: int
    retry_after: int
    """Seconds until the oldest recorded request leaves the window.

    Zero when the request was allowed. Sent as the ``Retry-After`` header, which
    is the difference between a client backing off correctly and a client
    guessing.
    """


class RateLimiter:
    """A shared sliding-window limiter.

    In Redis rather than in memory, because the API runs as more than one
    process and an in-memory limiter gives each of them its own allowance: a
    limit of ten becomes ten times however many workers happen to be running,
    which is a number nobody chose.
    """

    def __init__(self, client: Redis, *, prefix: str = "deeptrace:ratelimit") -> None:
        self.client = client
        self.prefix = prefix

    async def check(self, bucket: str, identity: str, *, limit: int, window: int) -> Decision:
        """Record one request against a bucket, or refuse it.

        ``bucket`` is the policy -- ``auth``, ``submit`` -- and ``identity`` is
        who is being counted. Keeping them separate is what stops one person's
        login attempts from consuming their own research allowance.
        """
        key = f"{self.prefix}:{bucket}:{identity}"
        now = time.time()
        cutoff = now - window

        pipeline = self.client.pipeline()
        pipeline.zremrangebyscore(key, 0, cutoff)
        # A unique member per request. Using the timestamp itself would make two
        # requests in the same millisecond a single set member, and the second
        # would be free.
        pipeline.zadd(key, {secrets.token_urlsafe(8): now})
        pipeline.zcard(key)
        pipeline.expire(key, window)
        _, _, count, _ = await pipeline.execute()

        if count <= limit:
            return Decision(
                allowed=True, limit=limit, remaining=max(0, limit - count), retry_after=0
            )

        # Over the limit: take this attempt back out of the log, so the window
        # drains on schedule and the Retry-After below stays true.
        await self.client.zremrangebyrank(key, -1, -1)

        oldest = await self.client.zrange(key, 0, 0, withscores=True)
        wait = math.ceil(float(oldest[0][1]) + window - now) if oldest else window
        return Decision(allowed=False, limit=limit, remaining=0, retry_after=max(1, wait))

    async def reset(self, bucket: str, identity: str) -> None:
        """Forget a client's history. Used by tests, and after a successful
        password change, where continuing to count failed guesses at the old
        password would punish the person who just proved they are the owner."""
        await self.client.delete(f"{self.prefix}:{bucket}:{identity}")


__all__ = ["Decision", "RateLimiter"]
