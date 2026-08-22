"""Workflow nodes.

Each node does one thing: read what it needs from the state, call one agent,
return only the keys it changed. None of them decides what runs next -- routing
is the graph's job, expressed as edges, so the control flow can be read from the
graph definition rather than reconstructed from conditionals scattered across
nodes.

Every node follows the same failure rule. An agent raising must not raise out of
the node, because an exception escaping the graph loses the state accumulated so
far, and that state is the trace. Instead the node records the failure in the
state and lets the graph route on it. A run that fails during evidence extraction
still has its sources, and those are worth keeping.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from core.agents.evidence import EvidenceAgent
from core.agents.planner import ResearchPlanner
from core.agents.query_analyzer import QueryAnalyzer
from core.agents.researcher import ResearchAgent
from core.config import ResearchDepth
from core.graph.state import ResearchState, ResearchStatus, state_summary
from core.llm.client import LLMClient
from core.logging import get_logger
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


def _advance(state: ResearchState, status: ResearchStatus) -> ResearchState:
    """The bookkeeping every node shares.

    ``iteration`` increments on every node, not only on loop bodies. It is the
    absolute ceiling on graph steps, so counting only the steps a cycle happens
    to pass through would leave a different cycle uncounted.
    """
    return {"status": status.value, "iteration": state.get("iteration", 0) + 1}


def _failed(state: ResearchState, stage: str, exc: Exception) -> ResearchState:
    """Record a failure in the state instead of raising it.

    An exception escaping a node loses everything the run accumulated, and that
    accumulation is the trace. The graph routes on ``error`` instead.
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
        "iteration": state.get("iteration", 0) + 1,
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
            return _failed(state, "analyze", exc)

        return {"spec": spec, **_advance(state, ResearchStatus.PLANNING)}

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
            return _failed(state, "plan", exc)

        return {"plan": research_plan, **_advance(state, ResearchStatus.RESEARCHING)}

    return plan


def make_research_node(ctx: NodeContext) -> NodeFn:
    """Research every planned task.

    Sequential for now. Bounded parallel execution is its own milestone, and
    doing it here first would make the latency improvement that milestone
    delivers impossible to measure against a baseline.
    """

    async def research(state: ResearchState) -> ResearchState:
        research_plan = state.get("plan")
        if research_plan is None:
            return _failed(state, "research", ValueError("no plan to execute"))

        tasks = (
            research_plan.tasks if ctx.max_tasks is None else research_plan.tasks[: ctx.max_tasks]
        )
        agent = ResearchAgent(ctx.client, ctx.search_provider)

        results = []
        sources = []
        problems = []
        for task in tasks:
            result = await agent.research(
                task,
                spec=state.get("spec"),
                depth=ctx.depth,
                research_id=state.get("research_id"),
            )
            results.append(result)
            sources.extend(result.sources)
            if not result.succeeded:
                # A task that found nothing is not a run failure. It is a gap in
                # coverage, recorded so the report can say which aspect is thin.
                problems.append(f"task {result.task_id}: {result.stop_reason}")

        return {
            "task_results": results,
            "sources": sources,
            "errors": problems,
            **_advance(state, ResearchStatus.EXTRACTING),
        }

    return research


def make_evidence_node(ctx: NodeContext) -> NodeFn:
    """Extract verified evidence from the collected sources."""

    async def extract(state: ResearchState) -> ResearchState:
        sources = state.get("sources", [])
        spec = state.get("spec")
        question = spec.normalized_question if spec else state["question"]

        try:
            report = await EvidenceAgent(ctx.client).extract(
                sources, question=question, research_id=state.get("research_id")
            )
        except Exception as exc:
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

        status = ResearchStatus.FAILED if produced_nothing else ResearchStatus.COMPLETED
        update: ResearchState = {
            "evidence": report.evidence,
            "errors": problems,
            **_advance(state, status),
        }
        if produced_nothing:
            update["error"] = "evidence extraction produced no results"

        # state_summary reports the status the node was entered with; the run
        # is finishing, so the outgoing status is the accurate one.
        log.info("graph.finished", **{**state_summary(state), "status": status.value})
        return update

    return extract
