"""The research workflow graph.

    START -> analyze -> plan -> research -> evidence -> END
                 |        |         |          |
                 └────────┴─────────┴──────────┴──> END on failure

Routing lives here, as edges, rather than inside nodes. A node that decided what
ran next would put the control flow in four places, and reading the sequence
would mean reading every node instead of one graph definition.

Two guarantees the sequential pipeline could not make:

*Bounded execution.* Every node increments ``iteration``, and routing refuses to
continue past a ceiling. That is checked in code on every transition, so no
prompt, agent, or future cycle can exceed it.

*Resumability.* With a checkpointer, state is written after each node. A worker
that dies during evidence extraction restarts there rather than re-running the
searches that already cost money -- which is the entire reason to use a stateful
workflow engine instead of calling the agents in sequence.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from core.config import ResearchDepth, Settings, get_settings
from core.graph.nodes import (
    NodeContext,
    make_analyze_node,
    make_evidence_node,
    make_plan_node,
    make_research_node,
)
from core.graph.serde import build_serializer
from core.graph.state import ResearchState, ResearchStatus, initial_state
from core.llm.client import LLMClient
from core.logging import bind_research_context, clear_research_context, get_logger
from core.observability.recorder import RunRecorder, new_run_id
from core.tools.search import SearchProvider, build_search_provider

log = get_logger(__name__)

DEFAULT_MAX_ITERATIONS = 25


def make_router(max_iterations: int) -> Callable[[ResearchState], Literal["continue", "stop"]]:
    """Build the predicate every transition passes through.

    One router for all transitions rather than one per edge, so the two reasons
    to stop -- a failure, or the iteration ceiling -- are enforced identically
    everywhere instead of being repeated and eventually diverging.
    """

    def route(state: ResearchState) -> Literal["continue", "stop"]:
        if state.get("error"):
            return "stop"

        iteration = state.get("iteration", 0)
        if iteration >= max_iterations:
            # Not an agent decision. A hard ceiling that holds regardless of what
            # any prompt says, which is what makes an unbounded loop impossible
            # rather than merely unlikely.
            log.warning(
                "graph.iteration_limit_reached",
                research_id=state.get("research_id"),
                iteration=iteration,
                limit=max_iterations,
            )
            return "stop"

        return "continue"

    return route


def memory_checkpointer() -> Any:
    """An in-process checkpointer, for tests and single-process runs.

    Built here rather than by callers so it always carries the serializer that
    knows DeepTrace's models. A checkpointer constructed without it appears to
    work and stops loading checkpoints after a library upgrade.
    """
    from langgraph.checkpoint.memory import MemorySaver

    return MemorySaver(serde=build_serializer())


def build_graph(
    ctx: NodeContext,
    *,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    checkpointer: Any | None = None,
) -> Any:
    """Assemble and compile the workflow.

    Args:
        ctx: The agents and providers the nodes use. Injected rather than read
            from configuration, so a test can build the real graph around stubs.
        max_iterations: Absolute ceiling on node executions.
        checkpointer: Where state is written after each node. Without one the
            graph still runs, but a crashed run cannot resume.
    """
    graph = StateGraph(ResearchState)

    # add_node's overloads do not accept a plain async callable returning the
    # state TypedDict, though that is exactly what LangGraph invokes and the
    # graph runs correctly. This is a limitation of the library's type stubs,
    # not a defect here, so it is silenced narrowly rather than by loosening the
    # node signatures -- which would remove real checking from the nodes
    # themselves to satisfy a third party's annotations.
    graph.add_node("analyze", make_analyze_node(ctx))  # type: ignore[call-overload]
    graph.add_node("plan", make_plan_node(ctx))  # type: ignore[call-overload]
    graph.add_node("research", make_research_node(ctx))  # type: ignore[call-overload]
    graph.add_node("evidence", make_evidence_node(ctx))  # type: ignore[call-overload]

    route = make_router(max_iterations)
    graph.add_edge(START, "analyze")
    for source, following in (
        ("analyze", "plan"),
        ("plan", "research"),
        ("research", "evidence"),
    ):
        graph.add_conditional_edges(source, route, {"continue": following, "stop": END})
    graph.add_edge("evidence", END)

    return graph.compile(checkpointer=checkpointer)


def build_context(
    settings: Settings | None = None,
    *,
    search_provider: SearchProvider | None = None,
    recorder: RunRecorder | None = None,
    depth: ResearchDepth = ResearchDepth.STANDARD,
    max_tasks: int | None = None,
) -> NodeContext:
    """Build node dependencies from configuration."""
    settings = settings or get_settings()
    return NodeContext(
        client=LLMClient.from_settings(settings, recorder=recorder),
        search_provider=search_provider or build_search_provider(settings),
        depth=depth,
        max_tasks=max_tasks,
        recorder=recorder,
    )


async def run_workflow(
    question: str,
    *,
    ctx: NodeContext,
    research_id: str | None = None,
    depth: ResearchDepth = ResearchDepth.STANDARD,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    checkpointer: Any | None = None,
) -> ResearchState:
    """Execute the workflow for one question and return the final state.

    ``research_id`` doubles as the checkpoint thread id, so resuming a run means
    invoking the graph again with the same id. There is no separate resume path
    to keep correct.
    """
    research_id = research_id or new_run_id("res")
    app = build_graph(ctx, max_iterations=max_iterations, checkpointer=checkpointer)

    bind_research_context(research_id=research_id, depth=depth.value)
    try:
        log.info("graph.started", research_id=research_id, question=question[:200])
        final: ResearchState = await app.ainvoke(
            initial_state(
                research_id=research_id,
                question=question,
                depth=depth.value,
                max_tasks=ctx.max_tasks,
            ),
            config={"configurable": {"thread_id": research_id}},
        )
    finally:
        clear_research_context()

    return _finalise(final)


def _finalise(final: ResearchState) -> ResearchState:
    """Give a run that stopped early an honest terminal status.

    Reached when routing stopped the graph -- an iteration ceiling, or a failure
    recorded without a status change. Left explicit rather than inferred, so a
    run never ends sitting in a status that says it is still working.
    """
    if final.get("status") not in (
        ResearchStatus.COMPLETED.value,
        ResearchStatus.FAILED.value,
    ):
        final["status"] = ResearchStatus.FAILED.value
    return final


class RunAlreadyCheckpointed(ValueError):
    """Raised when a new run is started under an id that already has state.

    Invoking a thread that already exists does not start over. LangGraph merges
    the input into the saved state, and the reducers append -- so a second run
    under the same id produces one run holding both runs' sources, both runs'
    task results, and evidence extracted from the union of them, with nothing
    anywhere saying that happened.

    Ids from :func:`new_run_id` never collide, so reaching this means a caller
    supplied an id deliberately. Refusing is the only answer that cannot corrupt
    the earlier run: continuing it is what ``resume`` is for, and that is a
    different verb.
    """

    def __init__(self, research_id: str) -> None:
        super().__init__(
            f"{research_id} already has checkpointed state. Starting a run under "
            f"an existing id would merge the two. Resume it, or use a new id."
        )
        self.research_id = research_id


class CheckpointNotFound(LookupError):
    """Raised when a run is resumed but nothing was ever checkpointed for it.

    Distinct from a run that failed: there is no state to continue from, so
    resuming would silently start a fresh run under an id whose history the
    caller believes already exists.
    """

    def __init__(self, research_id: str) -> None:
        super().__init__(
            f"No checkpoint exists for {research_id}. Either the id is wrong, or "
            f"the run was executed without a checkpointer and left nothing to resume."
        )
        self.research_id = research_id


async def load_state(checkpointer: Any, research_id: str) -> ResearchState | None:
    """Read a run's last checkpointed state without executing anything.

    Read from the checkpointer directly rather than through a compiled graph,
    because building the graph needs the depth and the search provider that this
    state is what tells us. It is also what "inspect a run in flight" will be
    built on: the state is the progress, so answering "where is this research
    now" is a read, not an inference from log lines.
    """
    config = {"configurable": {"thread_id": research_id}}
    saved = await checkpointer.aget_tuple(config)
    if saved is None:
        return None

    values: ResearchState = saved.checkpoint.get("channel_values", {})
    return values


async def resume_workflow(
    research_id: str,
    *,
    ctx: NodeContext,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    checkpointer: Any,
) -> ResearchState:
    """Continue a checkpointed run from wherever it stopped.

    Invoking with ``None`` as the input is what tells LangGraph to continue the
    saved thread rather than start a new one. There is deliberately no separate
    resume path through the graph: the same nodes and the same routing run, so a
    resumed run cannot behave differently from one that never stopped.
    """
    if await load_state(checkpointer, research_id) is None:
        # Checked against the checkpointer rather than the compiled graph:
        # ``aget_state`` answers with an empty snapshot for a thread that was
        # never written, so resuming an unknown id would quietly start a fresh
        # run under an id whose history the caller believes already exists.
        raise CheckpointNotFound(research_id)

    app = build_graph(ctx, max_iterations=max_iterations, checkpointer=checkpointer)
    config: Any = {"configurable": {"thread_id": research_id}}

    bind_research_context(research_id=research_id, depth=ctx.depth.value)
    try:
        log.info("graph.resumed", research_id=research_id)
        final: ResearchState = await app.ainvoke(None, config=config)
    finally:
        clear_research_context()

    return _finalise(final)
