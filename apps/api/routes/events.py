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
"""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from core.logging import get_logger
from core.observability.progress import EventKind, ProgressEvent

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
    after: int = Query(default=0, ge=0),
) -> None:
    """Stream a run's progress, starting after a given event.

    ``after=0`` replays everything still held, which is what a fresh client
    wants. A reconnecting client sends the last sequence number it saw, and
    receives exactly what followed -- which is the difference between a
    reconnect costing nothing and a reconnect losing the result.
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
