"""A job queue on Redis, with the delivery guarantee written down.

Built rather than imported, for the same reason the rate limiter was: the
property that matters here is narrow, and a library that provides it also
provides a worker model, a serialisation format, and a scheduling story this
project would then be shaped by. What is needed is one guarantee.

**A job taken by a worker that dies is taken again by another worker.**

That is at-least-once delivery, and it is the whole design:

*Reserving is atomic.* ``BLMOVE`` pops from the pending list and pushes onto a
processing list in one operation. A pop followed by a push has a window between
them, and a worker that dies inside that window loses the job with no record
that it ever existed.

*A reservation expires.* The worker holds a heartbeat key with a short TTL and
refreshes it while it works. The key is what says the worker is alive -- a job
sitting in the processing list with no heartbeat behind it belongs to a worker
that is gone, and reclaiming moves it back to pending.

*Retries are bounded.* Each reservation increments the attempt count, and a job
that exhausts its attempts goes to a dead-letter list rather than back to
pending. A job that crashes its worker would otherwise crash every worker in the
fleet, one after another, forever.

At-least-once means a job can run twice. That is safe here, and not by luck:
research is checkpointed under the job's research id, so a second attempt
resumes the first attempt's work instead of repeating it. The queue's weakest
guarantee is survivable precisely because the layer below it is resumable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

from core.config import Settings, get_settings
from core.logging import get_logger
from infrastructure.queue.job import Job, JobStatus

log = get_logger(__name__)

PENDING = "deeptrace:jobs:pending"
PROCESSING = "deeptrace:jobs:processing"
DEAD_LETTER = "deeptrace:jobs:dead"

HEARTBEAT_TTL_SECONDS = 60
"""How long a reservation survives without a heartbeat.

Long enough that a worker busy inside one slow model call is not declared dead,
short enough that a genuinely crashed worker's job is not stranded for minutes.
A run makes calls of a few seconds each, so a minute of silence means the
process is gone rather than working.
"""

MAX_ATTEMPTS = 3
"""Reservations before a job is set aside.

Three because the failures worth retrying -- a worker killed mid-run, a provider
outage, a deploy -- are transient and rarely persist across three attempts,
while a job that fails deterministically will fail identically the third time
and every time after.
"""

JOB_TTL_SECONDS = 60 * 60 * 24 * 7
"""How long a finished job's record stays in Redis.

The research itself is in PostgreSQL and outlives this entirely. What expires
here is the operational record -- attempts, worker, timings -- which is worth a
week for debugging and worth nothing after.
"""


def _as_text(value: Any) -> str:
    """Narrow a Redis value to text.

    The client is constructed with ``decode_responses=True``, so values arrive
    as strings -- but the stubs describe the general client, which may return
    bytes. Decoding when it is bytes rather than asserting it never is keeps
    this correct for a client configured either way, which matters because the
    configuration lives in a different module from the code that reads it.
    """
    return value if isinstance(value, str) else str(value.decode())


def _as_hash(raw: Any) -> dict[str, str]:
    return {_as_text(key): _as_text(value) for key, value in raw.items()}


def _key(job_id: str) -> str:
    return f"deeptrace:job:{job_id}"


def _alive_key(job_id: str) -> str:
    return f"deeptrace:job:{job_id}:alive"


def _cancel_key(job_id: str) -> str:
    return f"deeptrace:job:{job_id}:cancel"


class RedisJobQueue:
    """The queue. One instance per process, holding one connection pool."""

    def __init__(
        self,
        client: Redis,
        *,
        max_attempts: int = MAX_ATTEMPTS,
        heartbeat_ttl: int = HEARTBEAT_TTL_SECONDS,
    ) -> None:
        self.client = client
        self.max_attempts = max_attempts
        self.heartbeat_ttl = heartbeat_ttl

    @classmethod
    def from_settings(cls, settings: Settings | None = None, **kwargs: Any) -> RedisJobQueue:
        settings = settings or get_settings()
        # decode_responses=True so values come back as text. The client's type
        # stubs cannot express that, which is what _as_text and _as_hash are
        # for -- they narrow at the boundary and stay correct either way.
        return cls(Redis.from_url(settings.redis_url, decode_responses=True), **kwargs)

    # -- producing ---------------------------------------------------------

    async def enqueue(self, job: Job) -> Job:
        """Add a job to the back of the queue.

        The record is written before the id is pushed. A job id in the list with
        no hash behind it is a job a worker will reserve and then fail to read,
        and the reverse ordering makes that impossible rather than unlikely.
        """
        await self._write(job)
        await self.client.lpush(PENDING, job.id)
        log.info("queue.enqueued", job_id=job.id, research_id=job.research_id)
        return job

    # -- consuming ---------------------------------------------------------

    async def reserve(self, worker: str, *, timeout: int = 5) -> Job | None:
        """Take the next job, atomically, or return None if the queue is empty.

        Blocks for ``timeout`` seconds rather than spinning: a polling loop
        against Redis costs a request per interval per worker and adds latency
        equal to half the interval, and blocking has neither cost.
        """
        reserved = await self.client.blmove(PENDING, PROCESSING, timeout, "RIGHT", "LEFT")
        if reserved is None:
            return None

        job_id = _as_text(reserved)
        raw = await self.client.hgetall(_key(job_id))
        if not raw:
            # The record expired or was deleted while the id sat in the queue.
            # Dropping the id rather than failing keeps one lost record from
            # stopping the worker.
            await self.client.lrem(PROCESSING, 1, job_id)
            log.warning("queue.orphaned_id", job_id=job_id)
            return None

        job = Job.from_redis(_as_hash(raw))
        job.status = JobStatus.RUNNING
        job.attempts += 1
        job.worker = worker
        job.started_at = datetime.now(UTC)

        await self._write(job)
        await self.heartbeat(job.id)
        log.info("queue.reserved", job_id=job.id, worker=worker, attempt=job.attempts)
        return job

    async def _write(self, job: Job) -> None:
        """Store the job record.

        One place, so the ignore below is stated once rather than four times.
        ``hset`` is typed to accept a mapping whose keys are any Redis-writable
        type, and mypy treats a mapping's key type as invariant -- so a
        ``dict[str, str]`` is rejected despite every key being valid. A
        limitation of the stubs' variance, not of the call.
        """
        await self.client.hset(_key(job.id), mapping=job.to_redis())  # type: ignore[arg-type]

    async def heartbeat(self, job_id: str) -> None:
        """Say the worker is still alive. Called while the job runs."""
        await self.client.set(_alive_key(job_id), "1", ex=self.heartbeat_ttl)

    async def complete(self, job: Job) -> None:
        await self._finish(job, JobStatus.COMPLETED)

    async def cancelled(self, job: Job) -> None:
        await self._finish(job, JobStatus.CANCELLED)

    async def fail(self, job: Job, error: str) -> JobStatus:
        """Record a failure, and retry unless the attempts are spent.

        Returns the status the job ended in, so a caller can tell "will be
        tried again" from "will not be", which are different things to log and
        different things to tell a user.
        """
        job.error = error
        if job.attempts >= self.max_attempts:
            await self._finish(job, JobStatus.DEAD)
            await self.client.lpush(DEAD_LETTER, job.id)
            log.warning(
                "queue.dead_lettered",
                job_id=job.id,
                attempts=job.attempts,
                error=error[:200],
            )
            return JobStatus.DEAD

        job.status = JobStatus.QUEUED
        job.worker = None
        await self._write(job)
        await self.client.lrem(PROCESSING, 1, job.id)
        await self.client.delete(_alive_key(job.id))
        await self.client.lpush(PENDING, job.id)
        log.info("queue.retrying", job_id=job.id, attempt=job.attempts, error=error[:200])
        return JobStatus.QUEUED

    async def _finish(self, job: Job, status: JobStatus) -> None:
        job.status = status
        job.finished_at = datetime.now(UTC)
        await self._write(job)
        await self.client.expire(_key(job.id), JOB_TTL_SECONDS)
        await self.client.lrem(PROCESSING, 1, job.id)
        await self.client.delete(_alive_key(job.id))
        await self.client.delete(_cancel_key(job.id))
        log.info("queue.finished", job_id=job.id, status=status.value)

    # -- recovery ----------------------------------------------------------

    async def reclaim_stalled(self) -> list[str]:
        """Return jobs whose worker stopped heartbeating to the pending list.

        This is the guarantee the whole design exists for. A worker killed
        mid-run leaves its job in the processing list with nothing to advance
        it; without reclaiming, the job is not failed, not queued, and not
        running -- it is simply gone, and the only sign is a user waiting.

        Reclaimed rather than failed: the job has not gone wrong, it has been
        interrupted, and its research resumes from the last checkpoint.
        """
        reclaimed: list[str] = []
        for entry in await self.client.lrange(PROCESSING, 0, -1):
            job_id = _as_text(entry)
            if await self.client.exists(_alive_key(job_id)):
                continue

            raw = await self.client.hgetall(_key(job_id))
            await self.client.lrem(PROCESSING, 1, job_id)
            if not raw:
                continue

            job = Job.from_redis(_as_hash(raw))
            if job.attempts >= self.max_attempts:
                await self._finish(job, JobStatus.DEAD)
                await self.client.lpush(DEAD_LETTER, job.id)
                log.warning("queue.stalled_dead_lettered", job_id=job.id)
                continue

            job.status = JobStatus.QUEUED
            job.worker = None
            job.error = "worker stopped responding"
            await self._write(job)
            await self.client.lpush(PENDING, job.id)
            reclaimed.append(job.id)
            log.warning("queue.reclaimed", job_id=job.id, attempts=job.attempts)

        return reclaimed

    # -- cancellation ------------------------------------------------------

    async def request_cancel(self, job_id: str) -> bool:
        """Ask for a job to stop.

        A flag rather than a signal, because the process to interrupt may be on
        another machine. The worker reads it while running and stops -- which is
        what makes cancellation actually stop spending, rather than marking a
        job cancelled while its model calls continue.

        A queued job is cancelled outright: it has no worker to notice a flag.
        """
        raw = await self.client.hgetall(_key(job_id))
        if not raw:
            return False

        job = Job.from_redis(_as_hash(raw))
        if job.status.is_terminal:
            return False

        if job.status is JobStatus.QUEUED:
            await self.client.lrem(PENDING, 1, job_id)
            await self._finish(job, JobStatus.CANCELLED)
            return True

        await self.client.set(_cancel_key(job_id), "1", ex=self.heartbeat_ttl * 10)
        log.info("queue.cancel_requested", job_id=job_id)
        return True

    async def is_cancelled(self, job_id: str) -> bool:
        return bool(await self.client.exists(_cancel_key(job_id)))

    # -- reading -----------------------------------------------------------

    async def get(self, job_id: str) -> Job | None:
        raw = await self.client.hgetall(_key(job_id))
        return Job.from_redis(_as_hash(raw)) if raw else None

    async def depth(self) -> dict[str, int]:
        """How much work is waiting, running, and set aside."""
        return {
            "pending": await self.client.llen(PENDING),
            "processing": await self.client.llen(PROCESSING),
            "dead": await self.client.llen(DEAD_LETTER),
        }

    async def close(self) -> None:
        await self.client.aclose()
