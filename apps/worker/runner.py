"""The worker: takes jobs off the queue and runs research.

This is where two guarantees built separately finally meet. The queue promises a
job survives the worker that was holding it. The workflow promises a run resumes
from its last checkpoint. Neither is worth much alone -- a job that survives but
restarts from nothing has only preserved the request, and a run that could
resume but is never picked up again resumes never. Together they mean a worker
can be killed mid-run and the research continues, on another machine, from the
step it reached.

That is why a retried job keeps its research id. The id is the checkpoint
thread, so the second attempt is a continuation rather than a second run under a
new name, paid for twice.

Three things the loop takes care of that are easy to leave out:

*It heartbeats while it works.* A run takes minutes and makes no queue calls in
between. Without a heartbeat the queue cannot distinguish a busy worker from a
dead one, and would either reclaim jobs that are still running or never reclaim
any.

*It stops when asked.* Cancellation cancels the task, which stops the model
calls. Marking a job cancelled while its research continues would be a status
that lies and a bill that grows.

*It shuts down deliberately.* On a signal it stops taking new jobs and lets the
current one finish, because abandoning a run mid-flight to exit a second sooner
throws away work that was already paid for.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import socket
import time
from typing import Any

from core.config import Settings, get_settings
from core.graph.workflow import CheckpointNotFound, load_state
from core.logging import bind_research_context, clear_research_context, get_logger
from core.models.run import ResearchRun
from core.observability.progress import (
    EventKind,
    NullProgressEmitter,
    ProgressEmitter,
    ProgressEvent,
)
from core.observability.recorder import new_run_id
from core.pipeline import resume_research, run_research
from infrastructure.queue.job import Job, JobStatus
from infrastructure.queue.redis_queue import RedisJobQueue

log = get_logger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 15
"""How often the worker says it is alive.

A third of the reservation's time to live. One missed beat then costs nothing,
and a worker has to be silent for three consecutive intervals before the queue
concludes it is gone -- which is the difference between a slow model call and a
dead process.
"""

CANCEL_POLL_SECONDS = 5
"""How often a running job is checked for a cancellation request.

A poll rather than a subscription because the request may arrive at a worker on
another machine, and because the useful precision here is seconds: the thing
being stopped is a sequence of multi-second model calls.
"""


class Worker:
    """Consumes research jobs until told to stop."""

    def __init__(
        self,
        queue: RedisJobQueue,
        *,
        settings: Settings | None = None,
        name: str | None = None,
        checkpointer: Any | None = None,
        progress: ProgressEmitter | None = None,
    ) -> None:
        self.queue = queue
        self.settings = settings or get_settings()
        self.name = name or f"{socket.gethostname()}:{new_run_id('w')}"
        self.checkpointer = checkpointer
        self.progress = progress or NullProgressEmitter()
        self._running = True

    def stop(self) -> None:
        """Stop taking new jobs. The current one runs to completion."""
        self._running = False

    async def run_forever(self, *, reclaim_every: int = 30) -> None:
        """The loop: reclaim what was abandoned, take a job, run it, repeat.

        Reclaiming happens here rather than in a separate process because every
        worker is already awake and connected. A dedicated reaper would be one
        more thing to deploy and one more thing whose own death goes unnoticed.
        """
        log.info("worker.started", worker=self.name)
        last_reclaim = 0.0

        while self._running:
            if time.monotonic() - last_reclaim > reclaim_every:
                reclaimed = await self.queue.reclaim_stalled()
                if reclaimed:
                    log.warning("worker.reclaimed_jobs", count=len(reclaimed))
                last_reclaim = time.monotonic()

            job = await self.queue.reserve(self.name, timeout=5)
            if job is None:
                continue

            await self.execute(job)

        log.info("worker.stopped", worker=self.name)

    async def execute(self, job: Job) -> JobStatus:
        """Run one job to a terminal state, or hand it back for another attempt.

        The research runs as a task so two things can happen alongside it: the
        heartbeat, which keeps the reservation alive, and the cancellation
        check, which can stop it. Running the research inline would make both
        impossible for the several minutes it takes.
        """
        bind_research_context(research_id=job.research_id, depth=job.depth.value)
        await self._announce(
            job,
            EventKind.STARTED,
            f"Researching: {job.question[:150]}",
            attempt=job.attempts,
            depth=job.depth.value,
        )
        keepalive = asyncio.create_task(self._heartbeat(job))
        research = asyncio.create_task(self._research(job))

        try:
            cancelled = await self._await_or_cancel(job, research)
            if cancelled:
                await self.queue.cancelled(job)
                await self._announce(job, EventKind.CANCELLED, "Research was cancelled.")
                log.info("worker.job_cancelled", job_id=job.id, research_id=job.research_id)
                return JobStatus.CANCELLED

            run = await research
        except Exception as exc:
            # An interruption rather than a research failure: research failures
            # are returned, so anything raised here is the infrastructure --
            # a checkpoint store that vanished, a credential that expired.
            outcome = await self.queue.fail(job, f"{type(exc).__name__}: {exc}")
            await self._announce_failure(job, outcome, f"{type(exc).__name__}: {exc}")
            log.warning(
                "worker.job_failed",
                job_id=job.id,
                outcome=outcome.value,
                error_type=type(exc).__name__,
                error=str(exc)[:300],
            )
            return outcome
        finally:
            keepalive.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keepalive
            clear_research_context()

        if run.error:
            # The research reported a failure rather than raising. Retried the
            # same way, because the reasons are the same reasons -- a provider
            # outage looks identical from here -- and the next attempt resumes
            # rather than restarting.
            outcome = await self.queue.fail(job, run.error)
            await self._announce_failure(job, outcome, run.error)
            log.warning("worker.research_failed", job_id=job.id, outcome=outcome.value)
            return outcome

        await self._persist(run, owner_id=job.user_id)
        await self.queue.complete(job)
        await self._announce(
            job,
            EventKind.COMPLETED,
            "Research complete.",
            claims=len(run.claims),
            sources=len(run.sources),
            evidence=len(run.evidence),
            elapsed_seconds=run.elapsed_seconds,
        )
        log.info(
            "worker.job_completed",
            job_id=job.id,
            research_id=run.research_id,
            elapsed_seconds=run.elapsed_seconds,
            claims=len(run.claims),
        )
        return JobStatus.COMPLETED

    async def _announce(self, job: Job, kind: EventKind, message: str, **data: object) -> None:
        await self.progress.emit(
            ProgressEvent(
                research_id=job.research_id,
                kind=kind,
                message=message,
                data={"job_id": job.id, **data},
            )
        )

    async def _announce_failure(self, job: Job, outcome: JobStatus, error: str) -> None:
        """Say whether this is the end, or another attempt.

        A client shown "failed" for a job that is about to be retried will tell
        a user their research is gone, and then it will finish. The two are
        different events because they are different news.
        """
        retrying = outcome is not JobStatus.DEAD
        await self._announce(
            job,
            EventKind.STARTED if retrying else EventKind.FAILED,
            (
                f"Attempt {job.attempts} failed; trying again. ({error[:120]})"
                if retrying
                else f"Research failed after {job.attempts} attempts. ({error[:200]})"
            ),
            error=error[:300],
            attempts=job.attempts,
            will_retry=retrying,
        )

    async def _research(self, job: Job) -> ResearchRun:
        """Start the research, or continue it if this job has run before.

        Which one it is is decided by the checkpoint, not by the attempt
        counter. A first attempt that died before writing anything has no state
        to resume, and a job reclaimed from a crashed worker may have most of a
        run behind it -- and only the checkpoint knows which.
        """
        if self.checkpointer is not None:
            state = await load_state(self.checkpointer, job.research_id)
            if state is not None:
                log.info(
                    "worker.resuming",
                    job_id=job.id,
                    research_id=job.research_id,
                    status=state.get("status"),
                )
                try:
                    return await resume_research(
                        job.research_id,
                        checkpointer=self.checkpointer,
                        settings=self.settings,
                        progress=self.progress,
                    )
                except CheckpointNotFound:  # pragma: no cover - raced with expiry
                    pass

        return await run_research(
            job.question,
            depth=job.depth,
            max_tasks=job.max_tasks,
            settings=self.settings,
            checkpointer=self.checkpointer,
            research_id=job.research_id,
            progress=self.progress,
        )

    async def _await_or_cancel(self, job: Job, research: asyncio.Task[ResearchRun]) -> bool:
        """Wait for the research, checking for a cancellation request.

        Returns whether it was cancelled. The task is cancelled rather than
        abandoned, so the model calls stop -- an abandoned task keeps running,
        and keeps spending, behind a job that already says it stopped.
        """
        while True:
            done, _ = await asyncio.wait({research}, timeout=CANCEL_POLL_SECONDS)
            if done:
                return False

            if await self.queue.is_cancelled(job.id):
                research.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await research
                return True

    async def _heartbeat(self, job: Job) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            await self.queue.heartbeat(job.id)

    async def _persist(self, run: ResearchRun, *, owner_id: str | None) -> None:
        """Save the run under the account that queued it.

        The owner comes from the job rather than from the research, because the
        research engine has no concept of one -- it takes a question and
        produces a run. The job is the only record that remembers who asked, and
        carrying the id through to here is what turns a finished run into
        something its owner can find and nobody else can.

        Never let saving it fail the job.

        The research is done and its results are in memory. Losing them because
        the database was briefly unreachable would be worse than a job recorded
        as complete with its archive missing -- and the failure is logged, so
        the gap has a cause rather than being unexplained.
        """
        try:
            from infrastructure.db.engine import session_scope
            from infrastructure.db.recorder import PostgresRunRecorder
            from infrastructure.db.repositories.research import ResearchRepository
            from infrastructure.db.repositories.scope import Viewer

            async with session_scope(self.settings) as session:
                recorder = PostgresRunRecorder(session, research_id=run.research_id)
                for record in run.usage.agent_runs:
                    recorder.record_agent_run(record)
                for call in run.usage.tool_calls:
                    recorder.record_tool_call(call)

                # A system viewer: the worker is not acting as a person, and
                # is the only kind of caller allowed to name an owner other
                # than itself.
                repository = ResearchRepository(session, Viewer.system())
                await repository.save_run(run, user_id=owner_id)
                await recorder.flush()
        except Exception as exc:
            log.error(
                "worker.persist_failed",
                research_id=run.research_id,
                error_type=type(exc).__name__,
                error=str(exc)[:300],
            )


def install_signal_handlers(worker: Worker) -> None:
    """Stop taking jobs on SIGINT or SIGTERM, and finish the current one.

    A deploy sends SIGTERM to every worker at once. Exiting immediately would
    abandon whatever each was running, and those runs have already been paid
    for -- so the signal ends the loop rather than the process, and the job in
    flight finishes first.
    """
    loop = asyncio.get_running_loop()
    for received in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # not available on all platforms
            loop.add_signal_handler(received, worker.stop)
