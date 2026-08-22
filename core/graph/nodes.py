"""Workflow nodes.

Each node does one thing: read what it needs from the state, call one agent,
return only the keys it changed. None of them decides what runs next -- routing
is the graph's job, expressed as edges, so the control flow can be read from the
graph definition rather than reconstructed from conditionals scattered across
nodes.

Every node follows the same failure rule, which has two halves.

A *failure* -- this research cannot proceed -- is recorded in the state rather
than raised, because an exception escaping the graph loses everything the run
accumulated in memory, and that accumulation is the trace. A run that fails
during evidence extraction still has its sources, and those are worth keeping.

An *interruption* -- the provider is down, the rate limit is exhausted -- is
allowed to propagate. Recording it as a failure would end the run: routing stops
on ``error``, nothing is left pending, and a resume returns the same failure
while the work already paid for sits in the checkpoint doing nothing. Letting it
out leaves the step owed, and every completed node is already durable.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypedDict

from core.agents.analyst import AnalystAgent
from core.agents.evidence import EvidenceAgent
from core.agents.planner import ResearchPlanner
from core.agents.query_analyzer import QueryAnalyzer
from core.agents.researcher import ResearchAgent
from core.config import DEPTH_BUDGETS, ResearchDepth
from core.graph.state import ResearchState, ResearchStatus, state_summary
from core.llm.client import LLMClient
from core.logging import get_logger
from core.models.plan import ResearchPlan, ResearchTask
from core.models.query import QuerySpec
from core.observability.recorder import RunRecorder
from core.tools.search import SearchProvider

log = get_logger(__name__)

NodeFn = Callable[[ResearchState], Awaitable[ResearchState]]
"""What LangGraph calls: state in, partial update out.

The return type is ResearchState rather than a plain dict because the state is
declared ``total=False``: a partial update genuinely is a valid ResearchState,
and saying so lets the type checker verify that a node only writes keys the
state actually declares.

Nodes are built by factories rather than defined as bare functions so their
dependencies arrive by injection. A node reaching for a global client could not
be tested without patching one.
"""


@dataclass(slots=True)
class NodeContext:
    """What the nodes need to do their work.

    Passed in at graph construction rather than read from global configuration,
    so a test can build the whole workflow around stub agents without patching
    anything.
    """

    client: LLMClient
    search_provider: SearchProvider
    depth: ResearchDepth = ResearchDepth.STANDARD
    max_tasks: int | None = None
    max_concurrency: int = 5
    """How many research tasks may be in flight at once.

    A ceiling on researchers, not on requests: the client's rate limiter shapes
    the request rate against the provider's account-wide limit. This one bounds
    search quota, open connections, and how much page text is held in memory at
    the same time."""

    recorder: RunRecorder | None = None
    """Where tool calls are recorded.

    The LLM client records its own calls because it was built with this
    recorder; the research agent has to be handed it explicitly, and a node that
    forgot to would produce a run whose searches and fetches are missing from
    the trace while its model calls are all present."""


def _advance(status: ResearchStatus) -> ResearchState:
    """The bookkeeping every node shares.

    ``iteration`` is contributed by every node, not only by loop bodies. It is
    the absolute ceiling on graph steps, so counting only the steps a cycle
    happens to pass through would leave a different cycle uncounted.

    Each node contributes one and the channel sums them, rather than each node
    computing ``current + 1``. With concurrent tasks the second form counts one
    step per wave however wide the wave is.
    """
    return {"status": status.value, "iteration": 1}


def _interrupted(exc: Exception) -> bool:
    """Whether the infrastructure stopped this node, rather than the run failing.

    Both error taxonomies carry ``transient``: the request could not be served
    at all, as opposed to being served and answering badly. Reaching a node means
    the client already exhausted its own retries, so a transient error here says
    the provider is unavailable now, not that this research cannot be done.

    Deliberately not ``retryable``, which asks a different question -- whether to
    try again immediately, inside the client's own loop. A structured-output
    failure is retryable and not transient: waiting will not make a model's
    output fit a schema it does not fit, and treating it as an interruption
    would turn a broken schema into a run that invites resuming forever.

    The distinction only started mattering once state was checkpointed. Recording
    such an error as a failure ends the run: the graph routes on ``error`` and
    stops, leaving nothing pending, so resuming returns the failure unchanged
    while the work already paid for sits in the checkpoint unusable. Letting it
    propagate leaves the step owed, and every completed node is already durable.

    Found by a live run: a Gemini 503 during planning ended a run whose question
    analysis had already been paid for, and resuming it did nothing at all.
    """
    return bool(getattr(exc, "transient", False))


def _failed(state: ResearchState, stage: str, exc: Exception) -> ResearchState:
    """Record a failure in the state instead of raising it.

    An exception escaping a node loses everything the run accumulated in memory,
    and that accumulation is the trace. The graph routes on ``error`` instead.

    For failures only. An interruption is re-raised at the call site -- see
    :func:`_interrupted`.
    """
    message = f"{type(exc).__name__}: {exc}"
    log.warning(
        f"graph.{stage}_failed",
        research_id=state.get("research_id"),
        error_type=type(exc).__name__,
        error=str(exc),
    )
    return {
        "status": ResearchStatus.FAILED.value,
        "error": message,
        "errors": [f"{stage}: {message}"],
        "iteration": 1,
    }


def make_analyze_node(ctx: NodeContext) -> NodeFn:
    """Turn the question into a research specification."""

    async def analyze(state: ResearchState) -> ResearchState:
        try:
            spec = await QueryAnalyzer(ctx.client).analyze(
                state["question"],
                depth=ctx.depth,
                research_id=state.get("research_id"),
            )
        except Exception as exc:
            if _interrupted(exc):
                # Leaves the step owed so a resume retries it, instead of
                # burying a temporary outage as a permanent failure.
                raise
            return _failed(state, "analyze", exc)

        return {"spec": spec, **_advance(ResearchStatus.PLANNING)}

    return analyze


def make_plan_node(ctx: NodeContext) -> NodeFn:
    """Decompose the specification into executable tasks."""

    async def plan(state: ResearchState) -> ResearchState:
        spec = state.get("spec")
        if spec is None:
            return _failed(state, "plan", ValueError("no specification to plan from"))

        try:
            research_plan = await ResearchPlanner(ctx.client).plan(
                spec, depth=ctx.depth, research_id=state.get("research_id")
            )
        except Exception as exc:
            if _interrupted(exc):
                # Leaves the step owed so a resume retries it, instead of
                # burying a temporary outage as a permanent failure.
                raise
            return _failed(state, "plan", exc)

        return {"plan": research_plan, **_advance(ResearchStatus.RESEARCHING)}

    return plan


class TaskAssignment(TypedDict):
    """What one research task node is given.

    A fan-out target receives its ``Send`` payload as its input, not the whole
    state, so everything the task needs travels in here. That is a feature
    rather than a workaround: a task node cannot read another task's results,
    so two tasks cannot interfere no matter what order they finish in.
    """

    task: ResearchTask
    spec: QuerySpec | None
    research_id: str | None
    source_budget: int
    """This task's share of the run's source budget.

    Divided by the dispatcher, which knows how many tasks the plan has. The
    researcher cannot divide it -- it sees one task and would apply the whole
    run's ceiling to it, which is how a three-task quick run collected
    twenty-four sources against a budget of eight."""


def planned_waves(plan: ResearchPlan, max_tasks: int | None) -> list[list[ResearchTask]]:
    """The waves to dispatch, honouring the task limit.

    The limit is applied to the plan's task list, then the waves are filtered to
    what survived. Filtering after scheduling rather than before keeps one
    definition of the dependency order -- the plan's -- instead of a second one
    here that could disagree with it.
    """
    waves = plan.execution_waves()
    if max_tasks is None:
        return waves

    allowed = {task.id for task in plan.tasks[:max_tasks]}
    kept = [[task for task in wave if task.id in allowed] for wave in waves]
    return [wave for wave in kept if wave]


def make_dispatch_node() -> NodeFn:
    """Advance to the next wave of research.

    Does no research itself. It exists because a fan-out needs a node to fan out
    *from*, and because something has to own the keys a concurrent task may not
    write: the status, and the count of waves already dispatched.
    """

    async def dispatch(state: ResearchState) -> ResearchState:
        if state.get("plan") is None:
            return _failed(state, "research", ValueError("no plan to execute"))

        return {
            "wave": state.get("wave", 0) + 1,
            **_advance(ResearchStatus.RESEARCHING),
        }

    return dispatch


def make_task_node(ctx: NodeContext) -> Callable[[TaskAssignment], Awaitable[ResearchState]]:
    """Research one task. Many of these run at once.

    Two rules make concurrency safe here, and both are structural rather than
    conventional:

    *It writes only keys that have a reducer.* ``task_results``, ``sources`` and
    ``errors`` are appended, and ``iteration`` is summed. LangGraph raises on two
    concurrent writes to a key without one, so a node that reached for ``status``
    would not merely race -- it would fail the run, loudly, on the first plan
    wide enough to matter.

    *It never raises for a task that found nothing.* A thin task is a gap in
    coverage, recorded so the report can say which aspect is thin. Failing the
    wave for it would discard the tasks that succeeded alongside it.
    """
    # One semaphore per compiled graph, shared by every task execution: the
    # provider's limits apply to the account, not to a wave. The client's rate
    # limiter shapes request rate; this bounds how many researchers are in
    # flight at once, which is what protects search quota and memory.
    gate = asyncio.Semaphore(ctx.max_concurrency)

    async def research_task(assignment: TaskAssignment) -> ResearchState:
        task = assignment["task"]
        agent = ResearchAgent(ctx.client, ctx.search_provider, recorder=ctx.recorder)

        async with gate:
            try:
                result = await agent.research(
                    task,
                    spec=assignment.get("spec"),
                    depth=ctx.depth,
                    research_id=assignment.get("research_id"),
                    source_budget=assignment["source_budget"],
                )
            except Exception as exc:
                if _interrupted(exc):
                    # Left owed, so a resume re-runs this task. Siblings that
                    # had already finished keep their results -- LangGraph
                    # stores a completed task's writes even when the wave
                    # fails. Siblings still in flight are cancelled and re-run.
                    raise
                log.warning(
                    "graph.task_failed",
                    research_id=assignment.get("research_id"),
                    task_id=task.id,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                return {
                    "errors": [f"task {task.id}: {type(exc).__name__}: {exc}"],
                    "iteration": 1,
                }

        problems = [] if result.succeeded else [f"task {result.task_id}: {result.stop_reason}"]
        return {
            "task_results": [result],
            "sources": result.sources,
            "errors": problems,
            "iteration": 1,
        }

    return research_task


def make_evidence_node(ctx: NodeContext) -> NodeFn:
    """Extract verified evidence from the collected sources."""

    async def extract(state: ResearchState) -> ResearchState:
        sources = state.get("sources", [])
        spec = state.get("spec")
        question = spec.normalized_question if spec else state["question"]

        try:
            report = await EvidenceAgent(ctx.client).extract(
                sources,
                question=question,
                research_id=state.get("research_id"),
                limit=DEPTH_BUDGETS[ctx.depth].max_sources,
            )
        except Exception as exc:
            if _interrupted(exc):
                # Leaves the step owed so a resume retries it, instead of
                # burying a temporary outage as a permanent failure.
                raise
            return _failed(state, "evidence", exc)

        problems = [f"rejected: {claim}" for claim, _ in report.rejected]

        # A run that extracted nothing has not completed successfully, even
        # though no stage raised. Every source failing extraction produces an
        # empty report rather than an exception, because the evidence agent
        # isolates per-source failures -- so without this check the run would
        # report "completed" while having produced no evidence at all.
        produced_nothing = bool(report.sources_processed) and not report.evidence
        if produced_nothing:
            problems.append(
                f"no evidence extracted from {report.sources_processed} sources "
                f"({report.sources_failed} failed)"
            )

        status = ResearchStatus.FAILED if produced_nothing else ResearchStatus.SYNTHESIZING
        update: ResearchState = {
            "evidence": report.evidence,
            "rejected": report.rejected,
            "injection_attempts": report.injection_attempts,
            "sources_processed": report.sources_processed,
            "sources_failed": report.sources_failed,
            "errors": problems,
            **_advance(status),
        }
        if produced_nothing:
            update["error"] = "evidence extraction produced no results"

        return update

    return extract


def make_analysis_node(ctx: NodeContext) -> NodeFn:
    """Draw conclusions from the verified evidence.

    The last stage, and the only one that says something the sources did not.
    It runs after extraction rather than alongside it because it needs the whole
    pool: a contradiction is a relationship between two passages, and neither is
    visible from inside the source that produced one of them.
    """

    async def analyse(state: ResearchState) -> ResearchState:
        spec = state.get("spec")
        question = spec.normalized_question if spec else state["question"]

        try:
            report = await AnalystAgent(ctx.client).analyse(
                state.get("evidence", []),
                question=question,
                spec=spec,
                sources=state.get("sources", []),
                task_results=state.get("task_results", []),
                research_id=state.get("research_id"),
            )
        except Exception as exc:
            if _interrupted(exc):
                raise
            # A failed analysis is not a failed run. The evidence is collected,
            # verified, and stored, and it is worth more than the conclusions
            # drawn from it -- so the run keeps what it has and says what is
            # missing rather than discarding the expensive part.
            log.warning(
                "graph.analysis_failed",
                research_id=state.get("research_id"),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return {
                "errors": [f"analysis: {type(exc).__name__}: {exc}"],
                **_advance(ResearchStatus.COMPLETED),
            }

        problems = [f"analysis discarded: {statement}" for statement, _ in report.dropped]
        log.info(
            "graph.finished",
            **{**state_summary(state), "status": ResearchStatus.COMPLETED.value},
        )
        return {
            "analysis": report,
            "errors": problems,
            **_advance(ResearchStatus.COMPLETED),
        }

    return analyse
