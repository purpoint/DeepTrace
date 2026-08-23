"""Live progress over a WebSocket.

A research run takes minutes and produces nothing until it finishes. Polling can
report that, and does -- but a client polling every two seconds is asking a
question whose answer is "not yet" nineteen times out of twenty, and the
interesting moments arrive up to two seconds late.

The hard part is not the socket. It is that a dropped connection must cost
nothing, and pub/sub alone cannot promise that: a subscriber receives what is
published while it is connected, so a client whose network blinked is missing
whatever happened during the blink and has no way to know it. For a stream that
ends in a result, silently missing an event means waiting forever for one that
already came and went.

So a client says which event it last saw, and the order of operations here is
what makes the answer complete:

1. Subscribe first, and buffer whatever arrives.
2. Then read the history after that sequence number and send it.
3. Then send the buffered live events, skipping any the history already
   covered.

Subscribing after reading the history would leave a window -- events published
between the read and the subscribe reach neither path, and the client never
learns they existed. Sending the buffer without de-duplicating would deliver
some events twice, which for a progress stream means a claim count that goes up
and then up again.

**Authenticating it is its own problem.** A browser opening a WebSocket cannot
set an ``Authorization`` header -- the API has no parameter for one -- so the
credential must travel in the URL, where access logs and browser history will
keep it. The answer here is a ticket: minted by an authenticated HTTP call,
valid for thirty seconds, destroyed by the first use. What ends up in a log file
is a string that expired before anyone read it.

Redeeming the ticket says who is connecting. It does not say whether they may
watch *this* run, so the run is loaded through the same scoped repository the
REST endpoints use, and a stream for someone else's research is refused. A
progress stream leaks more than it looks like: the question text, the sources
being read, and the claims as they are made.
"""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from core.logging import get_logger
from core.observability.progress import EventKind, ProgressEvent
from infrastructure.db.repositories.research import ResearchRepository
from infrastructure.db.repositories.scope import Viewer

log = get_logger(__name__)

router = APIRouter(tags=["research"])

IDLE_TIMEOUT_SECONDS = 300
"""How long a stream waits with nothing happening before giving up.

A run's quietest stretch is a slow model call, which is seconds. Five minutes of
complete silence means the run is not running: its worker died and its job is
waiting to be reclaimed, and holding the socket open would tell the client
nothing while consuming a connection.
"""


@router.websocket("/research/{research_id}/events")
async def events(
    websocket: WebSocket,
    research_id: str,
    ticket: str = Query(min_length=8, max_length=256),
    after: int = Query(default=0, ge=0),
) -> None:
    """Stream a run's progress, starting after a given event.

    ``after=0`` replays everything still held, which is what a fresh client
    wants. A reconnecting client sends the last sequence number it saw, and
    receives exactly what followed -- which is the difference between a
    reconnect costing nothing and a reconnect losing the result.

    A reconnect therefore needs a fresh ticket, because the previous one was
    consumed. That is the client's job and it is a cheap call, and the
    alternative -- a ticket good for several connections -- is a credential in a
    URL with a reason to keep it alive.
    """
    stream = getattr(websocket.app.state, "events", None)
    if stream is None:
        # Accept and close rather than refusing the handshake: a client can read
        # a close code, while a rejected upgrade gives it an HTTP error it has
        # to interpret from a WebSocket API that was not built to show one.
        await websocket.accept()
        await websocket.close(code=1013, reason="Progress streaming is unavailable.")
        return

    await websocket.accept()

    viewer = await _redeem(websocket, ticket)
    if viewer is None:
        await websocket.close(code=1008, reason="Not signed in.")
        return

    if not await _may_watch(websocket, research_id, viewer):
        # The same answer a stranger gets from every REST endpoint: as though
        # the run does not exist. A distinct "not yours" close code would
        # confirm the id to anyone guessing.
        log.info("events.refused", research_id=research_id, user_id=viewer.user_id)
        await websocket.close(code=1008, reason="No such research.")
        return

    delivered = after

    try:
        async with stream.subscribe(research_id) as subscription:
            buffered = await _drain(subscription)

            for event in await stream.history(research_id, after=after):
                await websocket.send_text(event.model_dump_json())
                delivered = max(delivered, event.sequence)
                if event.kind.is_terminal:
                    await websocket.close(code=1000, reason="Research finished.")
                    return

            for event in buffered:
                if event.sequence > delivered:
                    await websocket.send_text(event.model_dump_json())
                    delivered = event.sequence

            while True:
                message = await asyncio.wait_for(
                    subscription.get_message(ignore_subscribe_messages=True, timeout=5),
                    timeout=IDLE_TIMEOUT_SECONDS,
                )
                if message is None:
                    # A poll that found nothing. Sending a ping here keeps
                    # intermediaries from closing an idle connection, and tells
                    # the client the stream is alive rather than stalled.
                    await websocket.send_json({"kind": "heartbeat", "sequence": delivered})
                    continue

                event = ProgressEvent.model_validate_json(message["data"])
                if event.sequence <= delivered:
                    continue

                await websocket.send_text(event.model_dump_json())
                delivered = event.sequence

                if event.kind.is_terminal:
                    await websocket.close(code=1000, reason="Research finished.")
                    return

    except WebSocketDisconnect:
        log.info("events.client_left", research_id=research_id, delivered=delivered)
    except TimeoutError:
        log.info("events.idle_timeout", research_id=research_id, delivered=delivered)
        with contextlib.suppress(RuntimeError):
            await websocket.close(code=1000, reason="No activity.")
    except Exception as exc:
        log.error(
            "events.stream_failed",
            research_id=research_id,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        with contextlib.suppress(RuntimeError):
            await websocket.close(code=1011, reason="The stream failed.")


async def _redeem(websocket: WebSocket, ticket: str) -> Viewer | None:
    """Trade a ticket for the viewer it was issued to, exactly once."""
    sessions = getattr(websocket.app.state, "sessions", None)
    if sessions is None:
        return None

    user_id = await sessions.redeem_ticket(ticket)
    return Viewer.user(user_id) if user_id else None


async def _may_watch(websocket: WebSocket, research_id: str, viewer: Viewer) -> bool:
    """Whether this viewer owns the run -- asked of the queue first, then the archive.

    The order is not an optimisation, it is the only order that works. A run's
    database row is written by the worker when the run *finishes*, so for the
    entire time a progress stream is interesting there is no row to check
    ownership against. The job in Redis exists from the moment the question was
    submitted and carries the id of whoever submitted it, which makes it the
    only record of ownership that exists while a run is in flight.

    The archive is the fallback, for a finished run whose job has aged out of
    Redis and is being read back. Between them the two cover a run's whole life,
    and neither covers it alone.
    """
    queue = getattr(websocket.app.state, "queue", None)
    if queue is not None:
        job = await queue.get_by_research(research_id)
        if job is not None:
            return job.user_id is not None and job.user_id == viewer.user_id

    factory = getattr(websocket.app.state, "session_factory", None)
    if factory is None:
        return False

    async with factory() as session:
        repository = ResearchRepository(session, viewer)
        return await repository.get_session(research_id) is not None


async def _drain(subscription: object) -> list[ProgressEvent]:
    """Take whatever the subscription has already buffered, without waiting.

    Called between subscribing and reading the history, so events published in
    that window are held rather than lost. They are sent after the history and
    filtered by sequence number, which is what keeps the two paths from
    delivering the same event twice.
    """
    events: list[ProgressEvent] = []
    while True:
        message = await subscription.get_message(  # type: ignore[attr-defined]
            ignore_subscribe_messages=True, timeout=0.01
        )
        if message is None:
            return events
        with contextlib.suppress(ValueError):
            events.append(ProgressEvent.model_validate_json(message["data"]))


__all__ = ["EventKind", "router"]
