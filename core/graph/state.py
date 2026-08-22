"""Workflow state: what flows between nodes.

The architecture document's rule is "structured state over hidden agent memory".
This module is where that becomes concrete. Every node receives the state, reads
what it needs, and returns only the keys it changed. Nothing is remembered inside
an agent between steps, and nothing is passed by side effect.

That has three consequences the sequential pipeline could not offer:

*A run can be inspected mid-flight.* The state is a plain dictionary, so the API
can answer "where is this research now" by reading it rather than by inferring
progress from log lines.

*A run can be resumed.* Because the state is complete and serialisable, a worker
that dies after the research step restarts at evidence extraction instead of
re-running searches that already cost money.

*A node is testable alone.* It takes a dict and returns a dict, so testing one
requires no graph, no database, and no other node.

The reducers below are what make concurrent nodes safe. When parallel research
tasks land at the same time, LangGraph merges their updates using the annotated
reducer instead of last-write-wins -- which would silently discard whichever task
happened to finish first.
"""

from __future__ import annotations

import operator
from enum import StrEnum
from typing import Annotated, Any, TypedDict

from core.models.evidence import Evidence
from core.models.plan import ResearchPlan
from core.models.query import QuerySpec
from core.models.research import TaskResult
from core.models.source import Source


class ResearchStatus(StrEnum):
    """Where a run is. Written into the state so progress is readable, not inferred."""

    QUEUED = "queued"
    ANALYZING = "analyzing"
    PLANNING = "planning"
    RESEARCHING = "researching"
    EXTRACTING = "extracting"
    COMPLETED = "completed"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (ResearchStatus.COMPLETED, ResearchStatus.FAILED)


def merge_errors(existing: list[str], incoming: list[str]) -> list[str]:
    """Accumulate errors rather than replacing them.

    A run can fail in more than one place -- two research tasks can both fail
    without the run failing -- and keeping only the most recent error would hide
    the earlier one from the trace.
    """
    return [*existing, *incoming]


class ResearchState(TypedDict, total=False):
    """The complete state of one research run.

    ``total=False`` so a node can return a partial update. LangGraph merges what
    is returned into the existing state, and requiring every node to return every
    key would make each node depend on fields it has no business knowing about.
    """

    # -- identity and input ------------------------------------------------
    research_id: str
    question: str
    depth: str

    # -- stage outputs -----------------------------------------------------
    spec: QuerySpec | None
    plan: ResearchPlan | None
    task_results: Annotated[list[TaskResult], operator.add]
    """Appended, not replaced. Parallel research tasks return their own result,
    and last-write-wins would discard every task but one."""

    sources: Annotated[list[Source], operator.add]
    evidence: Annotated[list[Evidence], operator.add]

    # -- control -----------------------------------------------------------
    status: str
    iteration: int
    """Incremented by every node. Compared against a hard ceiling so a cycle in
    the graph cannot run forever regardless of what any agent decides."""

    errors: Annotated[list[str], merge_errors]
    error: str | None
    """The failure that ended the run, if it ended. Distinct from ``errors``,
    which collects problems the run survived."""

    metadata: dict[str, Any]


def initial_state(*, research_id: str, question: str, depth: str) -> ResearchState:
    """Build the state a run starts from.

    Every collection is initialised empty rather than left absent, so a node can
    read a key without checking whether an earlier node happened to set it.
    """
    return ResearchState(
        research_id=research_id,
        question=question,
        depth=depth,
        spec=None,
        plan=None,
        task_results=[],
        sources=[],
        evidence=[],
        status=ResearchStatus.QUEUED.value,
        iteration=0,
        errors=[],
        error=None,
        metadata={},
    )


def state_summary(state: ResearchState) -> dict[str, Any]:
    """A compact view for logs and progress events.

    Deliberately excludes source and evidence bodies. The full state holds entire
    page texts, and logging it on every transition would produce megabytes of
    output per run.
    """
    plan = state.get("plan")
    return {
        "research_id": state.get("research_id"),
        "status": state.get("status"),
        "iteration": state.get("iteration", 0),
        "tasks_planned": len(plan.tasks) if plan else 0,
        "tasks_completed": len(state.get("task_results", [])),
        "sources": len(state.get("sources", [])),
        "evidence": len(state.get("evidence", [])),
        "errors": len(state.get("errors", [])),
    }
