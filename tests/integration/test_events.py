"""Integration tests for progress events, against a real Redis.

The acceptance criterion is that a client which disconnects and reconnects
loses no events. That is a claim about the seam between two mechanisms -- a
capped history and a pub/sub channel -- and the failure it guards against is
silent: a client waits forever for a result that was published while it was
away.

Against a real Redis because the guarantee rests on pub/sub delivering only to
current subscribers, which is precisely the behaviour a fake would be written
to paper over.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis

from apps.api.main import create_app
from core.config import Settings
from core.observability.progress import EventKind, ProgressEvent
from infrastructure.queue.events import RedisProgressStream

pytestmark = [pytest.mark.integration]

TEST_REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://localhost:6379/15")


@pytest.fixture
async def stream() -> AsyncIterator[RedisProgressStream]:
    client: Redis = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    await client.flushdb()
    subject = RedisProgressStream(client, history_limit=10)
    try:
        yield subject
    finally:
        await client.flushdb()
        await client.aclose()


def an_event(research_id: str = "res_1", kind: EventKind = EventKind.STAGE) -> ProgressEvent:
    return ProgressEvent(research_id=research_id, kind=kind, message="Planning the research")


class TestSequencing:
    async def test_events_are_numbered_in_order(self, stream: RedisProgressStream) -> None:
        for _ in range(3):
            await stream.emit(an_event())

        assert [e.sequence for e in await stream.history("res_1")] == [1, 2, 3]

    async def test_numbering_survives_the_history_being_trimmed(
        self, stream: RedisProgressStream
    ) -> None:
        """The number comes from a counter, not the list's length. Once old
        events are dropped the list is shorter than the stream, and a
        length-derived number would start repeating itself."""
        for _ in range(15):  # the fixture caps history at ten
            await stream.emit(an_event())

        history = await stream.history("res_1")

        assert len(history) == 10
        assert [e.sequence for e in history] == list(range(6, 16))

    async def test_runs_do_not_share_a_stream(self, stream: RedisProgressStream) -> None:
        await stream.emit(an_event("res_a"))
        await stream.emit(an_event("res_b"))

        assert len(await stream.history("res_a")) == 1
        assert len(await stream.history("res_b")) == 1

    async def test_history_can_start_after_a_given_event(self, stream: RedisProgressStream) -> None:
        for _ in range(5):
            await stream.emit(an_event())

        assert [e.sequence for e in await stream.history("res_1", after=3)] == [4, 5]

    async def test_emitting_never_raises(self) -> None:
        """Progress is narration. A run that fails because the thing describing
        it failed would be the tail wagging the dog."""
        broken: Redis = Redis.from_url("redis://127.0.0.1:6399/0", socket_connect_timeout=1)
        stream = RedisProgressStream(broken)

        await stream.emit(an_event())  # must not raise

        await broken.aclose()


class TestLiveDelivery:
    async def test_a_subscriber_receives_events_as_they_happen(
        self, stream: RedisProgressStream
    ) -> None:
        async with stream.subscribe("res_1") as subscription:
            await stream.emit(an_event())

            message = None
            for _ in range(50):
                message = await subscription.get_message(
                    ignore_subscribe_messages=True, timeout=0.1
                )
                if message:
                    break

            assert message is not None
            received = ProgressEvent.model_validate_json(message["data"])
            assert received.kind is EventKind.STAGE

    async def test_a_subscriber_misses_what_happened_before_it_arrived(
        self, stream: RedisProgressStream
    ) -> None:
        """The reason a history exists. Pub/sub delivers to current subscribers
        and nothing else, so this is the gap the replay closes."""
        await stream.emit(an_event())

        async with stream.subscribe("res_1") as subscription:
            message = await subscription.get_message(ignore_subscribe_messages=True, timeout=0.2)

        assert message is None
        assert len(await stream.history("res_1")) == 1


class TestTheWebSocket:
    """Driven through Starlette's test client, which speaks the protocol.

    The application is built from test settings and left to wire itself: the
    lifespan opens its own connections on startup, so anything assigned to
    ``app.state`` beforehand is replaced. Seeding therefore happens through a
    separate stream against the same Redis, which is also how the worker and the
    API relate in production -- two processes, one Redis.
    """

    @staticmethod
    def _app(migrated_database: str, *, redis_url: str = TEST_REDIS_URL) -> object:
        return create_app(
            Settings(
                _env_file=None,  # type: ignore[call-arg]
                database_url=migrated_database,
                redis_url=redis_url,
            )
        )

    @staticmethod
    def _seed(events: list[ProgressEvent]) -> None:
        async def run() -> None:
            client: Redis = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
            await client.flushdb()
            stream = RedisProgressStream(client)
            for event in events:
                await stream.emit(event)
            await client.aclose()

        asyncio.run(run())

    def test_a_client_receives_the_history_it_missed(self, migrated_database: str) -> None:
        from starlette.testclient import TestClient

        self._seed(
            [
                an_event("res_ws", EventKind.STARTED),
                an_event("res_ws", EventKind.STAGE),
                ProgressEvent(
                    research_id="res_ws", kind=EventKind.COMPLETED, message="Research complete."
                ),
            ]
        )

        with (
            TestClient(self._app(migrated_database)) as http,  # type: ignore[arg-type]
            http.websocket_connect("/research/res_ws/events") as socket,
        ):
            received = [socket.receive_json() for _ in range(3)]

        assert [event["sequence"] for event in received] == [1, 2, 3]
        assert received[-1]["kind"] == "completed"

    def test_a_reconnecting_client_receives_only_what_it_missed(
        self, migrated_database: str
    ) -> None:
        """The acceptance criterion. A client says what it last saw and gets
        exactly what followed -- no gap, and no duplicate that would make a
        counter climb twice."""
        from starlette.testclient import TestClient

        self._seed(
            [
                *(an_event("res_gap") for _ in range(4)),
                ProgressEvent(
                    research_id="res_gap", kind=EventKind.COMPLETED, message="Research complete."
                ),
            ]
        )

        with (
            TestClient(self._app(migrated_database)) as http,  # type: ignore[arg-type]
            http.websocket_connect("/research/res_gap/events?after=3") as socket,
        ):
            fourth = socket.receive_json()
            fifth = socket.receive_json()

        assert fourth["sequence"] == 4
        assert fifth["sequence"] == 5
        assert fifth["kind"] == "completed"

    def test_an_event_published_while_the_client_is_connected_arrives(
        self, migrated_database: str
    ) -> None:
        """Live delivery, not replay: the event is published after the socket is
        already open."""
        from starlette.testclient import TestClient

        self._seed([an_event("res_live", EventKind.STARTED)])

        def publish_terminal() -> None:
            async def run() -> None:
                client: Redis = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
                await RedisProgressStream(client).emit(
                    ProgressEvent(
                        research_id="res_live",
                        kind=EventKind.COMPLETED,
                        message="Research complete.",
                    )
                )
                await client.aclose()

            asyncio.run(run())

        with (
            TestClient(self._app(migrated_database)) as http,  # type: ignore[arg-type]
            http.websocket_connect("/research/res_live/events") as socket,
        ):
            assert socket.receive_json()["kind"] == "started"
            publish_terminal()
            assert socket.receive_json()["kind"] == "completed"

    def test_the_socket_closes_when_the_run_ends(self, migrated_database: str) -> None:
        """A client holding a socket open after the run finished holds a
        connection for nothing, and a server that never closes one leaks them."""
        from starlette.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect

        self._seed(
            [
                ProgressEvent(
                    research_id="res_end", kind=EventKind.FAILED, message="Research failed."
                )
            ]
        )

        with (
            TestClient(self._app(migrated_database)) as http,  # type: ignore[arg-type]
            http.websocket_connect("/research/res_end/events") as socket,
        ):
            assert socket.receive_json()["kind"] == "failed"
            with pytest.raises(WebSocketDisconnect):
                socket.receive_json()

    def test_streaming_being_unavailable_is_reported_not_hidden(
        self, migrated_database: str
    ) -> None:
        """Accepted and closed with a code rather than refused: a client can
        read a close code, while a rejected upgrade gives it an HTTP error its
        WebSocket API was not built to surface."""
        from starlette.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect

        # A Redis that is not there, so the lifespan records the failure and
        # leaves the stream unset -- which is what a real outage looks like.
        app = self._app(migrated_database, redis_url="redis://127.0.0.1:6399/0")

        with (
            TestClient(app) as http,  # type: ignore[arg-type]
            pytest.raises(WebSocketDisconnect) as closed,
            http.websocket_connect("/research/res_x/events") as socket,
        ):
            socket.receive_json()

        assert closed.value.code == 1013
