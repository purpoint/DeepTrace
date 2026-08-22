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

from core.models.analysis import AnalysisReport
from core.models.claim import ClaimSet
from core.models.evidence import Evidence
from core.models.plan import ResearchPlan
from core.models.query import QuerySpec
from core.models.report import Report
from core.models.research import TaskResult
from core.models.source import Source
from core.models.verification import VerificationReport


class ResearchStatus(StrEnum):
    """Where a run is. Written into the state so progress is readable, not inferred."""

    QUEUED = "queued"
    ANALYZING = "analyzing"
    PLANNING = "planning"
    RESEARCHING = "researching"
    EXTRACTING = "extracting"
    SYNTHESIZING = "synthesizing"
    CLAIMING = "claiming"
    VERIFYING = "verifying"
    REPORTING = "reporting"
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
    max_tasks: int | None
    """How many of the plan's tasks to research, or None for all of them.

    A run parameter, so it belongs in the state for the same reason depth does:
    a resume that took it from its caller would execute the run under limits it
    was not started with, and record it as having used the ones it was."""

    # -- stage outputs -----------------------------------------------------
    spec: QuerySpec | None
    plan: ResearchPlan | None
    task_results: Annotated[list[TaskResult], operator.add]
    """Appended, not replaced. Parallel research tasks return their own result,
    and last-write-wins would discard every task but one."""

    sources: Annotated[list[Source], operator.add]
    evidence: Annotated[list[Evidence], operator.add]

    rejected: Annotated[list[tuple[str, str]], operator.add]
    """Passages the verifier refused, as (claim, reason).

    Kept in the state rather than only in the extracting node's own return value
    because a rejection is a finding about the run, not a detail of the step that
    produced it. A run whose evidence was mostly fabricated and one that found
    little are different outcomes, and only this distinguishes them."""

    injection_attempts: Annotated[list[str], operator.add]
    """Domains whose retrieved content addressed the model."""

    analysis: AnalysisReport | None
    """What the evidence supports, and what grounding discarded.

    Written by one node, so it needs no reducer. Held whole rather than split
    into findings and discards because the two are only meaningful together:
    five findings from an analysis that discarded ten is a different result from
    five that discarded none."""

    claims: ClaimSet | None
    """The analysis expressed as individually checkable assertions.

    Held next to the analysis rather than replacing it: the analysis is what was
    concluded, the claims are what can be verified or rejected one at a time,
    and a run that loses either loses part of its trace."""

    verification: VerificationReport | None
    """What checking the claims concluded, and what it could not check.

    Kept beside the claims rather than folded into them: a claim carries its
    verdict, and this carries the reasoning, the contradicting passages, and the
    questions that would settle what is still open."""

    report: Report | None
    """The document a reader sees, and what assembling it had to remove.

    The last thing the run produces and the only one most people will read, so
    it is state like everything else: inspectable, checkpointed, and stored
    beside the claims it was written from rather than regenerated on demand."""

    extracted_source_ids: Annotated[list[str], operator.add]
    """Sources extraction has already read.

    Extraction is one model call per source and the largest line item in a run,
    so a second research loop must not pay for the first loop's sources again.
    Appended rather than replaced because extraction can run several times."""

    verification_loops: Annotated[int, operator.add]
    """Additional research rounds taken after verification asked for them.

    Compared against the depth budget's ceiling. Summed rather than assigned so
    the count survives however many nodes contribute to it, and so it cannot be
    reset by a node that forgot the previous value."""

    evidence_at_last_loop: int
    """How much evidence existed when the last loop was decided.

    The stop condition that matters most: a loop that researched more and
    produced no new evidence has found what is available, and running it again
    spends money to confirm that."""

    sources_processed: Annotated[int, operator.add]
    sources_failed: Annotated[int, operator.add]
    """Summed rather than replaced, for the same reason the lists are appended:
    when extraction eventually runs per task, each writes its own count."""

    # -- control -----------------------------------------------------------
    status: str
    wave: int
    """How many waves of the plan have been dispatched.

    Written only by the dispatcher, which runs alone, so it needs no reducer.
    It is what makes the research loop finite: the plan has a fixed number of
    waves and the dispatcher advances one per pass."""

    iteration: Annotated[int, operator.add]
    """Contributed by every node, one each. Compared against a hard ceiling so a
    cycle in the graph cannot run forever regardless of what any agent decides.

    Summed rather than assigned because research tasks now run concurrently.
    Concurrent nodes each computing ``current + 1`` would count one step for the
    whole wave -- and LangGraph refuses two concurrent writes to a key with no
    reducer, so the alternative is not an undercount but a crash."""

    errors: Annotated[list[str], merge_errors]
    error: str | None
    """The failure that ended the run, if it ended. Distinct from ``errors``,
    which collects problems the run survived."""

    metadata: dict[str, Any]


def initial_state(
    *, research_id: str, question: str, depth: str, max_tasks: int | None = None
) -> ResearchState:
    """Build the state a run starts from.

    Every collection is initialised empty rather than left absent, so a node can
    read a key without checking whether an earlier node happened to set it.
    """
    return ResearchState(
        research_id=research_id,
        question=question,
        depth=depth,
        max_tasks=max_tasks,
        spec=None,
        plan=None,
        task_results=[],
        sources=[],
        evidence=[],
        analysis=None,
        claims=None,
        verification=None,
        extracted_source_ids=[],
        verification_loops=0,
        evidence_at_last_loop=0,
        report=None,
        rejected=[],
        injection_attempts=[],
        sources_processed=0,
        sources_failed=0,
        status=ResearchStatus.QUEUED.value,
        wave=0,
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
        "wave": state.get("wave", 0),
        "tasks_planned": len(plan.tasks) if plan else 0,
        "tasks_completed": len(state.get("task_results", [])),
        "sources": len(state.get("sources", [])),
        "evidence": len(state.get("evidence", [])),
        "findings": len(analysis.analysis.findings) if (analysis := state.get("analysis")) else 0,
        "claims": len(claims.claims) if (claims := state.get("claims")) else 0,
        "verified": len(check.verdicts) if (check := state.get("verification")) else 0,
        "loops": state.get("verification_loops", 0),
        "rejected": len(state.get("rejected", [])),
        "errors": len(state.get("errors", [])),
    }
