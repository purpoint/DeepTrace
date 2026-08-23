"""Progress events: what a run is doing, while it is doing it.

A research run takes minutes and produces nothing until it finishes. Without
progress, a user's experience of the system is a spinner and a hope, and the
most common response to that is a second submission -- so the absence of
progress reporting costs real money, not just patience.

The emitter is a Protocol for the same reason the run recorder is. The research
engine must not know that Redis exists, and it must run identically with no
emitter at all: a test, a CLI run, or a worker deployed without a fan-out layer
all execute the same code, and the only difference is where the events go.

Events carry a sequence number assigned by whatever stores them, not by the
emitter. That number is what makes reconnection lossless -- a client says which
event it last saw and receives everything after it -- and a counter held by the
producer would restart at zero every time the producer did.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

EVENT_SCHEMA_VERSION = 1
"""Bumped when the shape of an event changes.

Carried on every event so a client can refuse a version it does not understand
rather than silently mis-reading fields. A browser tab open across a deploy is
the normal case, not the exotic one.
"""


class EventKind(StrEnum):
    """What happened.

    Coarse on purpose. An event per internal step would make the stream a
    mirror of the implementation, so every refactor would change what clients
    see -- and a progress bar does not need to know the graph's node names.
    """

    QUEUED = "queued"
    STARTED = "started"
    STAGE = "stage"
    """The run moved to a new phase: planning, researching, extracting, and so
    on. The one a progress bar is built from."""

    TASK_COMPLETED = "task_completed"
    SOURCES_FOUND = "sources_found"
    EVIDENCE_EXTRACTED = "evidence_extracted"
    CLAIMS_VERIFIED = "claims_verified"
    REPORT_READY = "report_ready"

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """Whether a stream can close after this.

        A client that keeps a socket open after the run has ended holds a
        connection for nothing, and a server that never closes one leaks them.
        """
        return self in (EventKind.COMPLETED, EventKind.FAILED, EventKind.CANCELLED)


class ProgressEvent(BaseModel):
    """One thing that happened during a run.

    ``message`` is written for a person and ``data`` for a program. Both are
    present because a client that has to compose its own sentences from a
    payload ends up with worse ones, and a client that has to parse a sentence
    to find a number ends up broken.
    """

    model_config = {"extra": "forbid"}

    version: int = EVENT_SCHEMA_VERSION
    sequence: int = Field(
        default=0,
        description="Position in this run's stream. Assigned by the store, not the emitter.",
    )
    research_id: str
    kind: EventKind
    message: str = Field(max_length=500)
    data: dict[str, Any] = Field(default_factory=dict)
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def summary(self) -> str:
        return f"#{self.sequence} {self.kind.value}: {self.message}"


@runtime_checkable
class ProgressEmitter(Protocol):
    """Where progress events go.

    One method, deliberately. An emitter that could also read, replay, or
    subscribe would be a client of the transport rather than a sink, and the
    research engine has no business holding one of those.
    """

    async def emit(self, event: ProgressEvent) -> None: ...


class NullProgressEmitter:
    """Discards events. The default, so nothing has to check for None.

    A run with no emitter is the normal case in a test and in the CLI, and
    making the absence a no-op rather than a null check keeps the emit calls in
    the nodes from being wrapped in conditionals that could get one wrong.
    """

    async def emit(self, event: ProgressEvent) -> None:  # noqa: ARG002 - the protocol's shape
        return None


class InMemoryProgressEmitter:
    """Collects events. For tests that assert what a run reported."""

    def __init__(self) -> None:
        self.events: list[ProgressEvent] = []

    async def emit(self, event: ProgressEvent) -> None:
        event.sequence = len(self.events) + 1
        self.events.append(event)

    def kinds(self) -> list[EventKind]:
        return [event.kind for event in self.events]
