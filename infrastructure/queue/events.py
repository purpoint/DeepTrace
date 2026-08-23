"""Progress events over Redis: published live, and kept long enough to replay.

Pub/sub alone cannot satisfy the requirement. A subscriber receives what is
published while it is connected and nothing else, so a client whose network
dropped for two seconds is missing whatever happened in those two seconds and
has no way to discover that it is. For a progress stream that ends in a result,
a silently truncated stream is worse than no stream: the client waits forever
for an event that already came and went.

So every event is written twice, and the two writes do different jobs.

*A list, capped, per run.* This is the history. It is what a reconnecting client
reads to catch up, and it is why a sequence number exists -- the client says
which event it last saw, and receives exactly what followed.

*A channel, published to.* This is the live delivery, and it is the only reason
a browser sees an event within milliseconds rather than on its next poll.

The order of the two matters. The history is written before the event is
published, because publishing first lets a live subscriber receive event 7 at a
moment when a reconnecting client cannot yet find it in the history -- and then
"everything after 6" means two different things depending on which client
asked.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from redis.asyncio import Redis

from core.config import Settings, get_settings
from core.logging import get_logger
from core.observability.progress import ProgressEvent

log = get_logger(__name__)

HISTORY_LIMIT = 500
"""Events kept per run.

A run emits on the order of dozens, so this holds several runs' worth of
headroom for one that loops. Capped rather than unbounded because a stuck run
must not be able to fill Redis, and trimmed from the front because when
something has to be lost it should be the oldest -- a client that is that far
behind has already reconnected and caught up, or has gone away.
"""

HISTORY_TTL_SECONDS = 60 * 60 * 24
"""How long a finished run's events remain replayable.

A day, because the audience for a progress stream is someone watching a run or
returning to a tab. The research itself is in PostgreSQL and permanent; this is
the narration, and narration of a run finished last week is of no use to
anyone.
"""


def _stream_key(research_id: str) -> str:
    return f"deeptrace:events:{research_id}"


def _sequence_key(research_id: str) -> str:
    return f"deeptrace:events:{research_id}:seq"


def _channel(research_id: str) -> str:
    return f"deeptrace:events:channel:{research_id}"


class RedisProgressStream:
    """Publishes progress events and serves the history of them."""

    def __init__(self, client: Redis, *, history_limit: int = HISTORY_LIMIT) -> None:
        self.client = client
        self.history_limit = history_limit

    @classmethod
    def from_settings(cls, settings: Settings | None = None, **kwargs: Any) -> RedisProgressStream:
        settings = settings or get_settings()
        return cls(Redis.from_url(settings.redis_url, decode_responses=True), **kwargs)

    async def emit(self, event: ProgressEvent) -> None:
        """Record an event and deliver it.

        Satisfies the ``ProgressEmitter`` protocol, which is what lets the
        research engine emit without knowing Redis exists.

        Failures are logged and swallowed. Progress reporting is narration: a
        run that completes without telling anyone is a worse outcome than a
        silent run, but it is far better than a run that fails because the thing
        narrating it did.
        """
        try:
            key = _stream_key(event.research_id)
            # The number comes from a counter rather than from the list's
            # length, so the list only ever holds complete events. Writing a
            # placeholder to claim a position and filling it in afterwards
            # leaves a window in which a reader sees something unparseable.
            #
            # It also survives trimming: once old events are dropped, the list
            # is shorter than the stream, and a length-derived number would
            # start repeating itself.
            event.sequence = await self.client.incr(_sequence_key(event.research_id))
            payload = event.model_dump_json()

            await self.client.rpush(key, payload)
            await self.client.ltrim(key, -self.history_limit, -1)
            await self.client.expire(key, HISTORY_TTL_SECONDS)
            await self.client.expire(_sequence_key(event.research_id), HISTORY_TTL_SECONDS)
            await self.client.publish(_channel(event.research_id), payload)
        except Exception as exc:
            log.warning(
                "events.emit_failed",
                research_id=event.research_id,
                kind=event.kind.value,
                error_type=type(exc).__name__,
            )

    async def history(self, research_id: str, *, after: int = 0) -> list[ProgressEvent]:
        """Events already recorded for a run, after a given sequence number.

        ``after=0`` is everything still held. A client reconnecting sends the
        last number it saw, which is what makes a dropped connection cost
        nothing.
        """
        raw = await self.client.lrange(_stream_key(research_id), 0, -1)
        events = []
        for entry in raw:
            event = ProgressEvent.model_validate_json(entry)
            if event.sequence > after:
                events.append(event)
        return events

    @asynccontextmanager
    async def subscribe(self, research_id: str) -> AsyncIterator[Any]:
        """A live subscription to one run's events.

        A context manager because an unclosed pub/sub connection holds a socket
        on the server for as long as the process lives, and a WebSocket endpoint
        is exactly where that accumulates: one leak per client that closed its
        tab.
        """
        pubsub = self.client.pubsub()
        await pubsub.subscribe(_channel(research_id))
        try:
            yield pubsub
        finally:
            await pubsub.unsubscribe(_channel(research_id))
            await pubsub.aclose()

    async def close(self) -> None:
        await self.client.aclose()
