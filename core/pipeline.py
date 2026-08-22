"""The composition root: what an entry point calls to run research.

    question -> analyse -> plan -> research each task -> extract evidence

Two functions, one for a new run and one for continuing a checkpointed one, and
both return the same object. Everything above this module -- the CLI today, the
worker and the API later -- gets a finished :class:`ResearchRun` without knowing
that a workflow engine produced it.

Execution itself lives in ``core.graph``. This module only assembles the pieces
that need configuration -- the model client, the search provider, the recorder
that tallies what a run cost -- and translates the workflow's state into the run
object every consumer reads.

The sequential loop that used to live here is gone. It ran the same four agents
in the same order as the graph, which meant every fix had two homes and only one
of them was exercised. What the graph adds is why: state written after each node,
so a run that dies during evidence extraction resumes there instead of paying
again for searches it already made.
"""

from __future__ import annotations

import time
from typing import Any

from core.config import ResearchDepth, Settings, get_settings
from core.graph.result import run_from_state
from core.graph.workflow import (
    DEFAULT_MAX_ITERATIONS,
    RunAlreadyCheckpointed,
    build_context,
    load_state,
    resume_workflow,
    run_workflow,
)
from core.logging import get_logger
from core.models.run import ResearchRun
from core.observability.recorder import (
    InMemoryRunRecorder,
    MultiRunRecorder,
    RunRecorder,
    new_run_id,
)
from core.tools.search import SearchProvider, build_search_provider

__all__ = [
    "ResearchRun",
    "build_search_provider",
    "resume_research",
    "run_research",
]

log = get_logger(__name__)


def _tally(recorder: RunRecorder | None) -> tuple[InMemoryRunRecorder, RunRecorder]:
    """Always tally usage in memory, and additionally wherever the caller asked.

    The in-memory tally is not optional. It is what the cost summary reads, and
    making it depend on the caller passing a recorder would mean a run without
    persistence configured reports no cost rather than its actual cost.
    """
    tally = InMemoryRunRecorder()
    return tally, MultiRunRecorder(tally, recorder) if recorder else tally


async def _interrupted_run(
    exc: Exception,
    *,
    research_id: str,
    question: str | None,
    depth: ResearchDepth,
    checkpointer: Any | None,
    elapsed_seconds: float,
    usage: InMemoryRunRecorder,
    resumed: bool = False,
) -> ResearchRun:
    """Turn an escaping exception into the run object callers expect.

    Reached two ways: a failure the graph cannot record in its own state (a
    missing credential, an unreachable checkpointer), or a node that was
    interrupted and let the error out so the step stays owed.

    Either way an entry point gets a run rather than a traceback. That is not
    cosmetic -- an interruption is the expected outcome of a provider outage,
    and a CLI that printed a stack trace for one would be reporting a bug where
    the honest answer is "the provider is down; resume when it is back".
    """
    log.warning("research.interrupted", error_type=type(exc).__name__, error=str(exc))
    error = f"{type(exc).__name__}: {exc}"

    # The exception carries none of the run, but the nodes that completed are
    # checkpointed -- so the partial trace is recoverable even here, and a
    # caller sees what was established before the interruption.
    salvaged = await load_state(checkpointer, research_id) if checkpointer else None
    if salvaged:
        run = run_from_state(
            salvaged,
            question=question,
            elapsed_seconds=elapsed_seconds,
            usage=usage,
            resumed=resumed,
        )
        run.error = error
        return run

    return ResearchRun(
        research_id=research_id,
        question=question or "",
        depth=depth,
        elapsed_seconds=elapsed_seconds,
        error=error,
        usage=usage,
        resumed=resumed,
    )


async def run_research(
    question: str,
    *,
    depth: ResearchDepth = ResearchDepth.STANDARD,
    max_tasks: int | None = None,
    settings: Settings | None = None,
    recorder: RunRecorder | None = None,
    search_provider: SearchProvider | None = None,
    checkpointer: Any | None = None,
    research_id: str | None = None,
) -> ResearchRun:
    """Run the full workflow for one question.

    Args:
        question: The research question.
        depth: Budget ceilings for the run.
        max_tasks: Research only the first N tasks of the plan. Useful for a
            cheap smoke test; the full plan runs when omitted.
        checkpointer: Where workflow state is written after each node. Without
            one the run still completes, but it cannot be resumed.
        research_id: The run's id, which doubles as its checkpoint thread id.

    Never raises for research failure. A run that fails partway returns what it
    completed with ``error`` set, because a partial trace is more useful than an
    exception, and this is the object the report and the trace view read from.

    Raises:
        RunAlreadyCheckpointed: if a caller-supplied id already has state.
    """
    settings = settings or get_settings()
    supplied_id = research_id is not None
    research_id = research_id or new_run_id("res")
    tally, sink = _tally(recorder)
    started = time.perf_counter()

    # Checked only when the caller chose the id. A generated one cannot collide,
    # and checking anyway spends a query per run to rule out the impossible.
    if (
        supplied_id
        and checkpointer is not None
        and await load_state(checkpointer, research_id) is not None
    ):
        raise RunAlreadyCheckpointed(research_id)

    try:
        ctx = build_context(
            settings,
            search_provider=search_provider,
            recorder=sink,
            depth=depth,
            max_tasks=max_tasks,
        )
        final = await run_workflow(
            question,
            ctx=ctx,
            research_id=research_id,
            depth=depth,
            max_iterations=settings.max_graph_iterations,
            checkpointer=checkpointer,
        )
    except Exception as exc:
        return await _interrupted_run(
            exc,
            research_id=research_id,
            question=question,
            depth=depth,
            checkpointer=checkpointer,
            elapsed_seconds=round(time.perf_counter() - started, 2),
            usage=tally,
        )

    return run_from_state(
        final,
        question=question,
        elapsed_seconds=round(time.perf_counter() - started, 2),
        usage=tally,
    )


async def resume_research(
    research_id: str,
    *,
    checkpointer: Any,
    settings: Settings | None = None,
    recorder: RunRecorder | None = None,
    search_provider: SearchProvider | None = None,
) -> ResearchRun:
    """Continue a checkpointed run from wherever it stopped.

    The depth and the task limit are read from the checkpoint rather than taken
    as arguments. They are properties of the run that was started, and letting a
    resume change them would mean the budgets a run executed under are not the
    ones it is recorded as having used.

    Found live: a run started with ``--max-tasks 1`` was interrupted, and the
    resume researched all three of its planned tasks -- the same run, finished
    under limits it never had.

    Raises:
        CheckpointNotFound: if the id has no checkpointed state.
    """
    settings = settings or get_settings()
    tally, sink = _tally(recorder)
    started = time.perf_counter()

    saved = await load_state(checkpointer, research_id)
    if saved is None:
        from core.graph.workflow import CheckpointNotFound

        raise CheckpointNotFound(research_id)

    depth = ResearchDepth(saved.get("depth", settings.default_depth.value))
    ctx = build_context(
        settings,
        search_provider=search_provider,
        recorder=sink,
        depth=depth,
        max_tasks=saved.get("max_tasks"),
    )

    log.info("research.resuming", research_id=research_id, status=saved.get("status"))
    try:
        final = await resume_workflow(
            research_id,
            ctx=ctx,
            max_iterations=settings.max_graph_iterations or DEFAULT_MAX_ITERATIONS,
            checkpointer=checkpointer,
        )
    except Exception as exc:
        # A resume can be interrupted exactly as the original run was -- the
        # provider that was down can still be down. Handled the same way, so a
        # second outage produces the same resumable run rather than a traceback.
        return await _interrupted_run(
            exc,
            research_id=research_id,
            question=None,
            depth=depth,
            checkpointer=checkpointer,
            elapsed_seconds=round(time.perf_counter() - started, 2),
            usage=tally,
            resumed=True,
        )

    return run_from_state(
        final,
        elapsed_seconds=round(time.perf_counter() - started, 2),
        usage=tally,
        resumed=True,
    )
