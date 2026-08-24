"""What a queued research request is.

A job is not a research concept. The engine takes a question and produces a run;
it has no opinion about who asked, when, or how many times the request has been
attempted. That is why this lives in infrastructure rather than in ``core``: it
describes how a deployment schedules work, and the research engine runs
identically without one.

The job and the run are deliberately separate records with a shared id. The job
is operational and short-lived -- queued, running, retried, done -- and lives in
Redis. The run is the research itself, lives in PostgreSQL, and outlives every
worker that touched it. Merging them would put attempt counters in the archive
and evidence in the queue.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from core.config import ResearchDepth
from core.observability.recorder import new_run_id


class JobStatus(StrEnum):
    """Where a job is, operationally.

    Distinct from the run's status, which describes the research. A job can be
    ``running`` while the research inside it is analysing, and a job can fail
    for reasons the research never saw -- a worker dying, a queue timing out.
    """

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEAD = "dead"
    """Retried as many times as allowed and still failing.

    Separate from ``failed`` because they call for different responses: a failed
    job may simply be waiting for its next attempt, while a dead one will not be
    attempted again and is waiting for a person."""

    @property
    def is_terminal(self) -> bool:
        return self in (
            JobStatus.COMPLETED,
            JobStatus.CANCELLED,
            JobStatus.DEAD,
        )


class Job(BaseModel):
    """One research request, queued for a worker.

    Carries the research id rather than generating one at execution time. That
    is what makes a retry a *resumption*: the same id is the same checkpoint
    thread, so a job picked up after a crash continues the research instead of
    starting a second one under a new name and paying for it twice.
    """

    model_config = {"extra": "forbid"}

    id: str = Field(default_factory=lambda: new_run_id("job"))
    research_id: str = Field(default_factory=lambda: new_run_id("res"))
    question: str = Field(min_length=3, max_length=2000)
    depth: ResearchDepth = ResearchDepth.STANDARD
    max_tasks: int | None = None

    status: JobStatus = JobStatus.QUEUED
    attempts: int = 0
    """How many times a worker has taken this job.

    Incremented on reservation rather than on failure, because the failure this
    counter exists to bound is a worker dying before it can report anything."""

    worker: str | None = None
    error: str | None = None

    user_id: str | None = None

    trace_carrier: dict[str, str] = Field(default_factory=dict)
    """The submitting request's trace context, in W3C traceparent form.

    A question is submitted by the API and executed minutes later by a worker
    that may be on another machine. Traced naively that is two unrelated
    traces, and the only question anyone actually asks -- why did this run take
    nine minutes -- spans both.

    Carried on the job rather than looked up, because by the time the worker
    starts there is nothing left to look it up from: the HTTP request is long
    over. The format is a header format, but nothing about it requires HTTP;
    the two ends here are a queue producer and a queue consumer.

    Empty for a job queued by the CLI, or by a version of the API that predates
    this. That is not an error -- the worker simply starts a new trace.
    """

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def to_redis(self) -> dict[str, str]:
        """Flatten for a Redis hash, which stores strings and nothing else.

        Everything is JSON-encoded rather than str()'d, so ``None`` survives as
        null instead of arriving back as the string "None" -- which reads as a
        value and compares as truthy.
        """
        return {key: json.dumps(value, default=str) for key, value in self.model_dump().items()}

    @classmethod
    def from_redis(cls, raw: dict[str, str]) -> Job:
        decoded: dict[str, Any] = {}
        for key, value in raw.items():
            try:
                decoded[key] = json.loads(value)
            except json.JSONDecodeError:
                decoded[key] = value
        return cls.model_validate(decoded)

    def summary(self) -> str:
        return (
            f"{self.id} [{self.status.value}] attempt {self.attempts} "
            f"{self.depth.value}: {self.question[:60]}"
        )
