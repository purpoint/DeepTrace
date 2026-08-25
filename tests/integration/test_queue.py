"""Integration tests for the job queue, against a real Redis.

The queue's only promise is that a job taken by a worker that dies is taken
again by another. That promise is about what happens when a process stops
existing, so the tests are about absence: a heartbeat that stops arriving, a
reservation nobody completes, a job nobody acknowledges.

Against a real Redis rather than a fake, because the guarantee rests on the
atomicity of BLMOVE and the expiry of a key. A fake that implements both
correctly is a second implementation of the thing under test, and one that
implements them approximately proves nothing at all.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis
from redis.exceptions import TimeoutError as RedisTimeoutError

from core.config import ResearchDepth, Settings
from infrastructure.queue.job import Job, JobStatus
from infrastructure.queue.redis_queue import (
    DEAD_LETTER,
    PENDING,
    PROCESSING,
    RESERVE_BLOCK_SECONDS,
    SOCKET_TIMEOUT_SECONDS,
    RedisJobQueue,
)

pytestmark = [pytest.mark.integration]

TEST_REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://localhost:6379/15")
"""Database 15, not the application's 0.

A test that flushes the database it shares with a running worker deletes that
worker's jobs, and the failure appears as work quietly disappearing rather than
as a failing test.
"""


@pytest.fixture
async def queue() -> AsyncIterator[RedisJobQueue]:
    client: Redis = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    await client.flushdb()
    subject = RedisJobQueue(client, heartbeat_ttl=1)
    try:
        yield subject
    finally:
        await client.flushdb()
        await client.aclose()


def a_job(question: str = "How does Kafka order records?") -> Job:
    return Job(question=question, depth=ResearchDepth.QUICK)


class TestTakingWork:
    async def test_a_queued_job_is_reserved_by_a_worker(self, queue: RedisJobQueue) -> None:
        await queue.enqueue(a_job())

        taken = await queue.reserve("worker-1", timeout=1)

        assert taken is not None
        assert taken.status is JobStatus.RUNNING
        assert taken.worker == "worker-1"
        assert taken.attempts == 1

    async def test_an_empty_queue_returns_nothing_rather_than_blocking_forever(
        self, queue: RedisJobQueue
    ) -> None:
        assert await queue.reserve("worker-1", timeout=1) is None

    async def test_two_workers_never_take_the_same_job(self, queue: RedisJobQueue) -> None:
        """What BLMOVE buys. A pop followed by a push has a window between them,
        and two workers arriving inside it both leave holding the job."""
        await queue.enqueue(a_job())

        first = await queue.reserve("worker-1", timeout=1)
        second = await queue.reserve("worker-2", timeout=1)

        assert first is not None
        assert second is None

    async def test_jobs_are_taken_in_the_order_they_were_queued(self, queue: RedisJobQueue) -> None:
        await queue.enqueue(a_job("first question about ordering"))
        await queue.enqueue(a_job("second question about ordering"))

        first = await queue.reserve("worker-1", timeout=1)
        second = await queue.reserve("worker-1", timeout=1)

        assert first is not None and second is not None
        assert first.question.startswith("first")
        assert second.question.startswith("second")

    async def test_a_completed_job_leaves_the_processing_list(self, queue: RedisJobQueue) -> None:
        """A job left in processing is a job that will be reclaimed and run
        again, however successfully it finished."""
        await queue.enqueue(a_job())
        taken = await queue.reserve("worker-1", timeout=1)
        assert taken is not None

        await queue.complete(taken)

        assert await queue.client.llen(PROCESSING) == 0
        stored = await queue.get(taken.id)
        assert stored is not None
        assert stored.status is JobStatus.COMPLETED


class TestSurvivingAWorkerThatDies:
    """The guarantee the design exists for."""

    async def test_a_job_whose_worker_stopped_is_queued_again(self, queue: RedisJobQueue) -> None:
        import asyncio

        await queue.enqueue(a_job())
        taken = await queue.reserve("worker-1", timeout=1)
        assert taken is not None

        # The worker dies: no completion, no failure, and no more heartbeats.
        await asyncio.sleep(1.2)

        reclaimed = await queue.reclaim_stalled()

        assert reclaimed == [taken.id]
        assert await queue.client.llen(PENDING) == 1
        assert await queue.client.llen(PROCESSING) == 0

    async def test_a_reclaimed_job_keeps_its_research_id(self, queue: RedisJobQueue) -> None:
        """This is what makes a retry a resumption. A new id would be a second
        run under a different name, paying again for work already done."""
        import asyncio

        original = await queue.enqueue(a_job())
        taken = await queue.reserve("worker-1", timeout=1)
        assert taken is not None
        await asyncio.sleep(1.2)
        await queue.reclaim_stalled()

        retaken = await queue.reserve("worker-2", timeout=1)

        assert retaken is not None
        assert retaken.research_id == original.research_id
        assert retaken.attempts == 2

    async def test_a_live_worker_keeps_its_job(self, queue: RedisJobQueue) -> None:
        """The heartbeat is what separates a slow worker from a dead one, and
        reclaiming a job that is still running would run it twice."""
        import asyncio

        await queue.enqueue(a_job())
        taken = await queue.reserve("worker-1", timeout=1)
        assert taken is not None

        await asyncio.sleep(0.6)
        await queue.heartbeat(taken.id)
        await asyncio.sleep(0.6)

        assert await queue.reclaim_stalled() == []
        assert await queue.client.llen(PROCESSING) == 1

    async def test_a_job_that_keeps_killing_workers_is_set_aside(
        self, queue: RedisJobQueue
    ) -> None:
        """Without a limit, a job that crashes its worker crashes every worker
        in the fleet, one after another, forever."""
        import asyncio

        await queue.enqueue(a_job())

        for _ in range(3):
            taken = await queue.reserve("worker-1", timeout=1)
            assert taken is not None
            await asyncio.sleep(1.2)
            await queue.reclaim_stalled()

        assert await queue.client.llen(PENDING) == 0
        assert await queue.client.llen(DEAD_LETTER) == 1


class TestRetries:
    async def test_a_failed_job_is_queued_again(self, queue: RedisJobQueue) -> None:
        await queue.enqueue(a_job())
        taken = await queue.reserve("worker-1", timeout=1)
        assert taken is not None

        outcome = await queue.fail(taken, "the provider was unreachable")

        assert outcome is JobStatus.QUEUED
        assert await queue.client.llen(PENDING) == 1

    async def test_a_job_that_keeps_failing_is_dead_lettered(self, queue: RedisJobQueue) -> None:
        await queue.enqueue(a_job())

        for _ in range(3):
            taken = await queue.reserve("worker-1", timeout=1)
            assert taken is not None
            outcome = await queue.fail(taken, "the provider was unreachable")

        assert outcome is JobStatus.DEAD
        assert await queue.client.llen(DEAD_LETTER) == 1
        stored = await queue.get(taken.id)
        assert stored is not None
        assert stored.error == "the provider was unreachable"

    async def test_a_dead_job_is_distinguishable_from_one_awaiting_retry(
        self, queue: RedisJobQueue
    ) -> None:
        """They call for different responses: one is waiting for a worker, the
        other is waiting for a person."""
        assert JobStatus.DEAD.is_terminal
        assert not JobStatus.FAILED.is_terminal


class TestCancellation:
    async def test_a_queued_job_is_cancelled_outright(self, queue: RedisJobQueue) -> None:
        """It has no worker to notice a flag, so leaving one set would let it be
        picked up and run after the user asked for it to stop."""
        job = await queue.enqueue(a_job())

        assert await queue.request_cancel(job.id) is True

        assert await queue.client.llen(PENDING) == 0
        stored = await queue.get(job.id)
        assert stored is not None
        assert stored.status is JobStatus.CANCELLED

    async def test_a_running_job_is_flagged_for_its_worker(self, queue: RedisJobQueue) -> None:
        await queue.enqueue(a_job())
        taken = await queue.reserve("worker-1", timeout=1)
        assert taken is not None

        assert await queue.request_cancel(taken.id) is True

        assert await queue.is_cancelled(taken.id) is True

    async def test_a_finished_job_cannot_be_cancelled(self, queue: RedisJobQueue) -> None:
        await queue.enqueue(a_job())
        taken = await queue.reserve("worker-1", timeout=1)
        assert taken is not None
        await queue.complete(taken)

        assert await queue.request_cancel(taken.id) is False

    async def test_cancelling_a_job_that_does_not_exist_says_so(self, queue: RedisJobQueue) -> None:
        assert await queue.request_cancel("job_nothing") is False


class TestTheRecord:
    async def test_a_job_survives_a_round_trip_through_redis(self, queue: RedisJobQueue) -> None:
        """Redis stores strings. A field that comes back as the string "None"
        reads as a value and compares as truthy."""
        job = await queue.enqueue(
            Job(question="a question long enough to pass", depth=ResearchDepth.DEEP)
        )

        stored = await queue.get(job.id)

        assert stored is not None
        assert stored.max_tasks is None
        assert stored.worker is None
        assert stored.depth is ResearchDepth.DEEP
        assert stored.created_at == job.created_at

    async def test_queue_depth_counts_each_list(self, queue: RedisJobQueue) -> None:
        await queue.enqueue(a_job("first question about ordering"))
        await queue.enqueue(a_job("second question about ordering"))
        await queue.reserve("worker-1", timeout=1)

        depth = await queue.depth()

        assert depth == {"pending": 1, "processing": 1, "dead": 0}


class TestAnIdleWorker:
    """A worker waiting on an empty queue must simply keep waiting.

    This class exists because of a bug that every other test in this file
    walked past. `BLMOVE` holds the connection for the block duration and then
    returns nil, and if the socket's read timeout expires at that same moment
    the client raises instead -- so a worker's reserve call blew up on an empty
    queue and the process exited seconds after starting, having done nothing
    wrong and leaving the queue draining into nothing.

    The reason it survived is the reason it is worth writing down: every test
    above reserves with `timeout=1`, and production reserves with `timeout=5`.
    redis-py applies an effective five-second read timeout when none is given,
    so one second never raced it and five seconds always did. The tests were
    green because they were quicker than the bug.
    """

    async def test_reserving_from_an_empty_queue_returns_none(self, queue: RedisJobQueue) -> None:
        """At the block duration production actually uses.

        Deliberately not `timeout=1`. A test that blocks for less time than the
        real worker does is testing a case the real worker never encounters.
        """
        assert await queue.reserve("worker-1", timeout=RESERVE_BLOCK_SECONDS) is None

    async def test_the_client_can_outwait_its_own_block(self) -> None:
        """The invariant, asserted directly: the socket read timeout must be
        strictly greater than the longest blocking command. Equal is the bug."""
        assert SOCKET_TIMEOUT_SECONDS > RESERVE_BLOCK_SECONDS

    async def test_a_configured_client_blocks_the_full_duration_without_raising(
        self, queue: RedisJobQueue
    ) -> None:
        """End to end through a client built the way production builds one."""
        configured = RedisJobQueue.from_settings(
            Settings(_env_file=None, redis_url=TEST_REDIS_URL)  # type: ignore[call-arg]
        )
        started = time.perf_counter()
        try:
            result = await configured.reserve("worker-1", timeout=RESERVE_BLOCK_SECONDS)
        finally:
            await configured.close()
        elapsed = time.perf_counter() - started

        assert result is None
        # It really blocked rather than returning early, which is the behaviour
        # that makes a polling loop unnecessary.
        assert elapsed >= RESERVE_BLOCK_SECONDS - 0.5

    async def test_a_read_timeout_is_reported_as_no_job(
        self, queue: RedisJobQueue, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The second line of defence. Even if the timeouts were ever
        misconfigured again, an idle poll must not be able to end a worker."""

        async def raise_timeout(*args: object, **kwargs: object) -> None:
            raise RedisTimeoutError("Timeout reading from localhost:6379")

        monkeypatch.setattr(queue.client, "blmove", raise_timeout)

        assert await queue.reserve("worker-1", timeout=1) is None
