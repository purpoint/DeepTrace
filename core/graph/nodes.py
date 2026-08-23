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
from dataclasses import dataclass, field
from typing import TypedDict

from core.agents.analyst import AnalystAgent
from core.agents.evidence import EvidenceAgent
from core.agents.fact_checker import FactChecker
from core.agents.planner import ResearchPlanner
from core.agents.query_analyzer import QueryAnalyzer
from core.agents.reporter import Reporter
from core.agents.researcher import ResearchAgent
from core.config import DEPTH_BUDGETS, ResearchDepth
from core.graph.state import ResearchState, ResearchStatus, state_summary
from core.llm.client import LLMClient
from core.logging import get_logger
from core.models.claim import build_claims
from core.models.plan import FOLLOW_UP_PREFIX, ResearchPlan, ResearchTask
from core.models.query import QuerySpec
from core.observability.progress import (
    EventKind,
    NullProgressEmitter,
    ProgressEmitter,
    ProgressEvent,
)
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

    progress: ProgressEmitter = field(default_factory=NullProgressEmitter)
    """Where progress events go while a run is in flight.

    Defaults to a sink that discards them, so a node emits unconditionally and
    a run without a fan-out layer -- a test, the CLI -- executes exactly the
    same code. A null check at each emit is one more thing to get wrong at ten
    call sites."""

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


async def _report(
    ctx: NodeContext,
    state: ResearchState,
    kind: EventKind,
    message: str,
    **data: object,
) -> None:
    """Tell whoever is watching what just happened.

    Never raises. Progress is narration, and a run that fails because the thing
    describing it failed would be the tail wagging the dog -- so the emitter's
    own errors are its problem, and this call is safe to make from anywhere.
    """
    await ctx.progress.emit(
        ProgressEvent(
            research_id=state.get("research_id", "unknown"),
            kind=kind,
            message=message,
            data=dict(data),
        )
    )


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

        await _report(
            ctx,
            state,
            EventKind.STAGE,
            "Question analysed; planning the research",
            stage=ResearchStatus.PLANNING.value,
            research_type=spec.research_type.value,
            scope=len(spec.scope),
        )
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

        await _report(
            ctx,
            state,
            EventKind.STAGE,
            f"Planned {len(research_plan.tasks)} research tasks",
            stage=ResearchStatus.RESEARCHING.value,
            tasks=[task.question for task in research_plan.tasks],
        )
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

    Follow-up tasks are exempt. The limit exists to keep a smoke test cheap by
    truncating what the *planner* produced; a follow-up exists because a claim
    could not be settled, and dropping it would turn a debugging convenience
    into a hole in the verification that asked for it.
    """
    waves = plan.execution_waves()
    if max_tasks is None:
        return waves

    allowed = {task.id for task in plan.tasks[:max_tasks]}
    allowed |= {task.id for task in plan.tasks if task.id.startswith(FOLLOW_UP_PREFIX)}
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
        await ctx.progress.emit(
            ProgressEvent(
                research_id=assignment.get("research_id") or "unknown",
                kind=EventKind.TASK_COMPLETED,
                message=f"Researched: {task.question[:120]}",
                data={
                    "task_id": task.id,
                    "sources": len(result.usable_sources),
                    "verdict": result.verdict.value,
                    "stop_reason": result.stop_reason,
                },
            )
        )
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
        spec = state.get("spec")
        question = spec.normalized_question if spec else state["question"]

        # Only what has not been read yet. Extraction is one model call per
        # source, so a second research loop re-reading the first loop's sources
        # would pay the run's largest cost again to learn nothing new.
        already = set(state.get("extracted_source_ids", []))
        fresh = [source for source in state.get("sources", []) if source.id not in already]

        # The budget is a ceiling on the run, not on each pass, so what earlier
        # passes spent comes off what this one may.
        remaining = max(0, DEPTH_BUDGETS[ctx.depth].max_sources - len(already))
        if not fresh or not remaining:
            log.info(
                "graph.extraction_skipped",
                research_id=state.get("research_id"),
                already_extracted=len(already),
                new_sources=len(fresh),
                budget_remaining=remaining,
            )
            return {**_advance(ResearchStatus.SYNTHESIZING)}

        try:
            report = await EvidenceAgent(ctx.client).extract(
                fresh,
                question=question,
                research_id=state.get("research_id"),
                limit=remaining,
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
            "extracted_source_ids": report.extracted_source_ids,
            "rejected": report.rejected,
            "injection_attempts": report.injection_attempts,
            "sources_processed": report.sources_processed,
            "sources_failed": report.sources_failed,
            "errors": problems,
            **_advance(status),
        }
        if produced_nothing:
            update["error"] = "evidence extraction produced no results"

        await _report(
            ctx,
            state,
            EventKind.EVIDENCE_EXTRACTED,
            f"Extracted {len(report.evidence)} passages and checked each against its source",
            stage=status.value,
            evidence=len(report.evidence),
            verbatim=len(report.verified_evidence),
            rejected=len(report.rejected),
            sources_read=report.sources_processed,
        )
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
        await _report(
            ctx,
            state,
            EventKind.STAGE,
            f"Analysed the evidence: {report.analysis.summary_line()}",
            stage=ResearchStatus.CLAIMING.value,
            findings=len(report.analysis.findings),
            contradictions=len(report.analysis.contradictions),
            discarded=len(report.dropped),
        )
        return {
            "analysis": report,
            "errors": problems,
            **_advance(ResearchStatus.CLAIMING),
        }

    return analyse


def make_claims_node(ctx: NodeContext) -> NodeFn:
    """Turn the analysis into individually checkable claims.

    No model call: the analyst already decided what the evidence supports, and
    asking a model to restate its own conclusions as claims would add cost,
    latency, and a second chance to invent something. This node exists as a
    stage anyway, for two reasons.

    It is where verification will attach. A fact checker rejects, revises, or
    re-researches individual claims, and that loop needs somewhere to stand that
    is not inside the analyst.

    And it is a checkpoint boundary. Deriving claims inside the analysis node
    would mean a crash between the two re-running the analysis, which is a paid
    call on the strong tier, to redo work that costs nothing.
    """

    async def derive(state: ResearchState) -> ResearchState:
        report = state.get("analysis")
        if report is None:
            # Not a failure: a run whose analysis never happened has no claims
            # to derive, and the evidence it collected is still worth keeping.
            return {**_advance(ResearchStatus.COMPLETED)}

        claims = build_claims(
            report.analysis,
            state.get("evidence", []),
            research_id=state.get("research_id"),
        )
        problems = [f"claim rejected: {text}" for text, _ in claims.rejected]

        log.info(
            "graph.claims_derived",
            research_id=state.get("research_id"),
            claims=len(claims.claims),
            conflicting=len(claims.conflicting_pairs()),
            merged=sum(claim.merged_from - 1 for claim in claims.claims),
            rejected=len(claims.rejected),
        )
        await _report(
            ctx,
            state,
            EventKind.STAGE,
            f"Derived {len(claims.claims)} claims; checking each against the evidence",
            stage=ResearchStatus.VERIFYING.value,
            claims=len(claims.claims),
            conflicting=len(claims.conflicting_pairs()),
        )
        return {
            "claims": claims,
            "errors": problems,
            **_advance(ResearchStatus.VERIFYING),
        }

    return derive


def make_verify_node(ctx: NodeContext) -> NodeFn:
    """Check each claim against the evidence, including evidence it did not cite.

    The last stage, and the only adversarial one. Everything before it asks what
    the evidence supports; this asks whether a specific statement is supported,
    and brings in passages from other tasks that were never compared against it.
    """

    async def verify(state: ResearchState) -> ResearchState:
        claims = state.get("claims")
        if claims is None or not claims.claims:
            return {**_advance(ResearchStatus.COMPLETED)}

        spec = state.get("spec")
        question = spec.normalized_question if spec else state["question"]

        try:
            checked, report = await FactChecker(ctx.client).check(
                claims,
                state.get("evidence", []),
                question=question,
                research_id=state.get("research_id"),
            )
        except Exception as exc:
            if _interrupted(exc):
                raise
            # Unverified claims are still claims, and the evidence behind them
            # is still collected. A failed check must not mark them refuted:
            # failing to check something is not evidence against it.
            log.warning(
                "graph.verification_failed",
                research_id=state.get("research_id"),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return {
                "errors": [f"verification: {type(exc).__name__}: {exc}"],
                **_advance(ResearchStatus.COMPLETED),
            }

        problems = [f"claim unverified: {claim_id}" for claim_id, _ in report.failed]
        update: ResearchState = {
            "claims": checked,
            "verification": report,
            "errors": problems,
        }

        await _report(
            ctx,
            state,
            EventKind.CLAIMS_VERIFIED,
            report.summary(),
            **report.counts(),
            unchecked=len(report.failed),
        )

        scheduled, research_again = _schedule_follow_ups(state, report.follow_up_questions, ctx)
        if not research_again:
            return {**update, **scheduled, **_advance(ResearchStatus.REPORTING)}

        return {**update, **scheduled, **_advance(ResearchStatus.RESEARCHING)}

    return verify


def make_report_node(ctx: NodeContext) -> NodeFn:
    """Write the report from the claims that survived verification.

    Last, and deliberately downstream of everything: it is handed claims, and
    the sources and page text never reach it. A generator that cannot see a
    rejected page cannot cite one.

    A failed report is not a failed run. The claims, the evidence and the trace
    are all stored, and they are the expensive part -- so the run keeps them and
    says the writing failed, rather than discarding a completed research effort
    because the last call did not return.
    """

    async def write(state: ResearchState) -> ResearchState:
        claims = state.get("claims")
        if claims is None:
            return {**_advance(ResearchStatus.COMPLETED)}

        spec = state.get("spec")
        analysis = state.get("analysis")

        try:
            report = await Reporter(ctx.client).write(
                claims,
                question=spec.normalized_question if spec else state["question"],
                evidence=state.get("evidence", []),
                sources=state.get("sources", []),
                spec=spec,
                task_results=state.get("task_results", []),
                verification=state.get("verification"),
                open_questions=[item.question for item in analysis.analysis.open_questions]
                if analysis
                else [],
                research_loops=state.get("verification_loops", 0),
                research_id=state.get("research_id"),
            )
        except Exception as exc:
            if _interrupted(exc):
                raise
            log.warning(
                "graph.report_failed",
                research_id=state.get("research_id"),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return {
                "errors": [f"report: {type(exc).__name__}: {exc}"],
                **_advance(ResearchStatus.COMPLETED),
            }

        problems = [
            f"citation removed, points at nothing: {marker}" for marker in report.unresolved_markers
        ]
        problems.extend(
            f"report referenced an unpublishable claim: {claim_id}"
            for claim_id in report.unsupported_claim_ids
        )

        await _report(
            ctx,
            state,
            EventKind.REPORT_READY,
            f"Report written: {report.summary()}",
            title=report.title,
            citations=len(report.citations),
            fully_cited=report.is_fully_cited,
        )
        log.info(
            "graph.finished",
            **{**state_summary(state), "status": ResearchStatus.COMPLETED.value},
        )
        return {
            "report": report,
            "errors": problems,
            **_advance(ResearchStatus.COMPLETED),
        }

    return write


def _schedule_follow_ups(
    state: ResearchState, questions: list[str], ctx: NodeContext
) -> tuple[ResearchState, bool]:
    """Decide whether to research again, and set up the round if so.

    Returns the state to write and whether the run continues. The state is
    written either way: a follow-up refused as a duplicate is a fact about the
    run -- verification asked for something and the plan already covered it --
    and dropping it silently would leave no trace that anything was asked.

    Three ways the answer is no, and each is a ceiling rather than a judgement,
    so no prompt can argue past one:

    *Nothing to ask.* Verification settled everything it could, or the
    follow-ups it proposed only repeated the question already asked.

    *The budget is spent.* ``max_verification_loops`` is zero for a quick run,
    one for standard, three for deep. A loop costs a full research round plus a
    fresh analysis and a re-check of every claim, so it is the depth setting's
    business how many a run may have.

    *The last loop found nothing.* If the previous round of research produced no
    new evidence, another round searches the same web for the same answers. This
    is the same convergence rule the research loop already uses one level down,
    applied to the loop above it.

    The new tasks go through the plan, which refuses one that repeats existing
    coverage -- so duplicate prevention is the planner's rule rather than a
    second definition living here.
    """
    if not questions:
        return {}, False

    plan = state.get("plan")
    if plan is None:  # pragma: no cover - verification implies a plan
        return {}, False

    taken = state.get("verification_loops", 0)
    allowed = DEPTH_BUDGETS[ctx.depth].max_verification_loops
    if taken >= allowed:
        log.info(
            "graph.follow_up_budget_spent",
            research_id=state.get("research_id"),
            loops_taken=taken,
            allowed=allowed,
            unasked=len(questions),
        )
        return {
            "errors": [
                f"follow-up not researched, loop budget spent: {question}" for question in questions
            ]
        }, False

    evidence_now = len(state.get("evidence", []))
    if taken and evidence_now <= state.get("evidence_at_last_loop", 0):
        log.info(
            "graph.follow_up_converged",
            research_id=state.get("research_id"),
            evidence=evidence_now,
            loops_taken=taken,
        )
        return {
            "errors": ["follow-up not researched, the previous round produced no new evidence"]
        }, False

    extended, refused = plan.with_follow_ups(questions)
    problems = [f"follow-up refused as duplicate: {reason}" for reason in refused]
    if extended is plan:
        log.info(
            "graph.follow_ups_all_duplicates",
            research_id=state.get("research_id"),
            refused=len(refused),
        )
        return {"errors": problems}, False

    waves = planned_waves(extended, ctx.max_tasks)
    log.info(
        "graph.researching_again",
        research_id=state.get("research_id"),
        loop=taken + 1,
        tasks=len(extended.tasks) - len(plan.tasks),
        refused_duplicates=len(refused),
    )
    return {
        "plan": extended,
        # Rewound so the dispatcher's next pass hands out the wave the
        # follow-ups landed in. It increments before reading, so this is one
        # short of the wave count rather than equal to it.
        "wave": len(waves) - 1,
        "verification_loops": 1,
        "evidence_at_last_loop": evidence_now,
        "errors": problems,
    }, True
