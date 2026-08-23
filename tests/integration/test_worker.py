"""Integration tests for the worker.

The queue tests prove a job survives a worker that dies. These prove the other
half: that the worker does the right thing with a job it holds -- resumes rather
than restarts, stops when asked, and never marks a job complete when the
research inside it failed.

The research itself is stubbed. What is under test is the loop around it, and
running real research here would make every assertion depend on a model's mood.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis

from apps.worker.runner import Worker
from core.config import ResearchDepth, Settings
from core.models.run import ResearchRun
from infrastructure.queue.job import Job, JobStatus
from infrastructure.queue.redis_queue import PENDING, RedisJobQueue

pytestmark = [pytest.mark.integration]

TEST_REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://localhost:6379/15")


@pytest.fixture
async def queue() -> AsyncIterator[RedisJobQueue]:
    client: Redis = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    await client.flushdb()
    subject = RedisJobQueue(client, heartbeat_ttl=2)
    try:
        yield subject
    finally:
        await client.flushdb()
        await client.aclose()


def settings() -> Settings:
    return Settings(_env_file=None, google_api_key="k", tavily_api_key="k")  # type: ignore[call-arg]


def a_job() -> Job:
    return Job(question="How does Kafka order records?", depth=ResearchDepth.QUICK)


def a_run(job: Job, *, error: str | None = None) -> ResearchRun:
    return ResearchRun(
        research_id=job.research_id,
        question=job.question,
        depth=job.depth,
        error=error,
        elapsed_seconds=0.1,
    )


class StubWorker(Worker):
    """A worker whose research is replaced, and whose saves go nowhere.

    Persistence is stubbed out rather than pointed at a database because these
    tests are about the job's fate, and a worker that failed to save must still
    complete its job -- which is itself asserted below.
    """

    def __init__(self, *args: object, run: ResearchRun, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._run = run
        self.persisted: list[ResearchRun] = []
        self.research_calls = 0

    async def _research(self, job: Job) -> ResearchRun:
        self.research_calls += 1
        return self._run

    async def _persist(self, run: ResearchRun) -> None:
        self.persisted.append(run)


class TestFinishingAJob:
    async def test_a_successful_run_completes_the_job(self, queue: RedisJobQueue) -> None:
        job = await queue.enqueue(a_job())
        taken = await queue.reserve("worker-1", timeout=1)
        assert taken is not None
        worker = StubWorker(queue, settings=settings(), run=a_run(job))

        outcome = await worker.execute(taken)

        assert outcome is JobStatus.COMPLETED
        stored = await queue.get(job.id)
        assert stored is not None
        assert stored.status is JobStatus.COMPLETED
        assert worker.persisted, "a completed run was never saved"

    async def test_a_research_failure_is_retried_rather_than_completed(
        self, queue: RedisJobQueue
    ) -> None:
        """Research reports failure rather than raising, so a worker that only
        watched for exceptions would mark a failed run complete."""
        job = await queue.enqueue(a_job())
        taken = await queue.reserve("worker-1", timeout=1)
        assert taken is not None
        worker = StubWorker(queue, settings=settings(), run=a_run(job, error="LLMServerError: 503"))

        outcome = await worker.execute(taken)

        assert outcome is JobStatus.QUEUED
        assert await queue.client.llen(PENDING) == 1
        assert worker.persisted == [], "a failed run was archived as though it finished"

    async def test_a_run_that_keeps_failing_is_eventually_set_aside(
        self, queue: RedisJobQueue
    ) -> None:
        await queue.enqueue(a_job())

        for _ in range(3):
            taken = await queue.reserve("worker-1", timeout=1)
            assert taken is not None
            worker = StubWorker(
                queue, settings=settings(), run=a_run(taken, error="LLMServerError: 503")
            )
            outcome = await worker.execute(taken)

        assert outcome is JobStatus.DEAD


class TestCancellation:
    async def test_a_cancelled_job_stops_the_research(self, queue: RedisJobQueue) -> None:
        """The task is cancelled, not abandoned. An abandoned task keeps running
        and keeps spending behind a job that already says it stopped."""
        job = await queue.enqueue(a_job())
        taken = await queue.reserve("worker-1", timeout=1)
        assert taken is not None
        stopped = asyncio.Event()

        class SlowWorker(StubWorker):
            async def _research(self, job: Job) -> ResearchRun:
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    stopped.set()
                    raise
                return self._run  # pragma: no cover - the sleep is cancelled

        worker = SlowWorker(queue, settings=settings(), run=a_run(job))
        running = asyncio.create_task(worker.execute(taken))

        await asyncio.sleep(0.1)
        await queue.request_cancel(job.id)
        outcome = await running

        assert outcome is JobStatus.CANCELLED
        assert stopped.is_set(), "the research kept running after cancellation"
        stored = await queue.get(job.id)
        assert stored is not None
        assert stored.status is JobStatus.CANCELLED


class TestResumption:
    async def test_a_job_with_a_checkpoint_resumes_rather_than_restarts(
        self, queue: RedisJobQueue, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Where the queue's at-least-once delivery meets the workflow's
        resumability: a second attempt continues the first attempt's work rather
        than paying for it again.

        Which path is taken is decided by the checkpoint, not the attempt
        counter -- a first attempt that died before writing anything has nothing
        to resume, and only the checkpoint knows the difference.
        """
        import apps.worker.runner as runner

        job = await queue.enqueue(a_job())
        taken = await queue.reserve("worker-1", timeout=1)
        assert taken is not None

        resumed: list[str] = []
        started: list[str] = []

        async def fake_load(checkpointer: object, research_id: str) -> dict[str, str] | None:
            return {"status": "researching"}

        async def fake_resume(research_id: str, **kwargs: object) -> ResearchRun:
            resumed.append(research_id)
            return a_run(job)

        async def fake_run(question: str, **kwargs: object) -> ResearchRun:
            started.append(question)
            return a_run(job)

        monkeypatch.setattr(runner, "load_state", fake_load)
        monkeypatch.setattr(runner, "resume_research", fake_resume)
        monkeypatch.setattr(runner, "run_research", fake_run)

        worker = Worker(queue, settings=settings(), checkpointer=object())
        monkeypatch.setattr(worker, "_persist", lambda _run: asyncio.sleep(0))

        outcome = await worker.execute(taken)

        assert outcome is JobStatus.COMPLETED
        assert resumed == [job.research_id]
        assert started == [], "the retry started a second run instead of resuming"

    async def test_a_job_with_no_checkpoint_starts_a_run(
        self, queue: RedisJobQueue, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import apps.worker.runner as runner

        job = await queue.enqueue(a_job())
        taken = await queue.reserve("worker-1", timeout=1)
        assert taken is not None

        started: list[str] = []

        async def fake_load(checkpointer: object, research_id: str) -> dict[str, str] | None:
            return None

        async def fake_run(question: str, **kwargs: object) -> ResearchRun:
            started.append(kwargs.get("research_id", ""))  # type: ignore[arg-type]
            return a_run(job)

        monkeypatch.setattr(runner, "load_state", fake_load)
        monkeypatch.setattr(runner, "run_research", fake_run)

        worker = Worker(queue, settings=settings(), checkpointer=object())
        monkeypatch.setattr(worker, "_persist", lambda _run: asyncio.sleep(0))

        await worker.execute(taken)

        assert started == [job.research_id], "a fresh run must keep the job's research id"


class TestPersistenceIsNotTheJob:
    async def test_a_failed_save_does_not_fail_a_finished_run(self, queue: RedisJobQueue) -> None:
        """The research is done and paid for. Losing the job because the archive
        was briefly unreachable would be the worse outcome."""
        job = await queue.enqueue(a_job())
        taken = await queue.reserve("worker-1", timeout=1)
        assert taken is not None

        class BrokenArchive(StubWorker):
            async def _persist(self, run: ResearchRun) -> None:
                # The real _persist swallows and logs; this asserts the worker
                # calls it inside that contract rather than around it.
                await Worker._persist(self, run)

        worker = BrokenArchive(
            queue,
            settings=Settings(_env_file=None, database_url=None),
            run=a_run(job),  # type: ignore[call-arg]
        )

        outcome = await worker.execute(taken)

        assert outcome is JobStatus.COMPLETED
