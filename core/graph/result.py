"""Turning a finished workflow state into a research run.

The graph's state is a dictionary shaped for merging: partial updates, reducers,
keys a node may or may not have written. That is the right shape for a workflow
engine and the wrong shape for everything downstream -- the CLI, the repository,
and the report all want one object whose fields are either present or ``None``.

This module is the single place that translation happens. Keeping it here rather
than in each consumer means a key added to the state is read in one place, and a
consumer never has to know that ``rejected`` and ``evidence`` arrive separately
because reducers cannot merge a nested model.
"""

from __future__ import annotations

from core.config import ResearchDepth
from core.graph.state import ResearchState, ResearchStatus
from core.models.evidence import EvidenceExtractionReport
from core.models.run import ResearchRun
from core.observability.recorder import InMemoryRunRecorder


def _extraction_report(state: ResearchState) -> EvidenceExtractionReport | None:
    """Rebuild the extraction report from the keys the state carries.

    Returns ``None`` when extraction never ran, which is not the same as an
    extraction that produced nothing: a run that failed while planning has no
    report, and a run that processed twenty sources and rejected every passage
    has one that says so. Collapsing the two would make a fabricating source
    look like a stage that never executed.
    """
    if not state.get("sources_processed") and not state.get("evidence"):
        return None

    return EvidenceExtractionReport(
        evidence=list(state.get("evidence", [])),
        rejected=[(claim, reason) for claim, reason in state.get("rejected", [])],
        sources_processed=state.get("sources_processed", 0),
        sources_failed=state.get("sources_failed", 0),
        injection_attempts=list(state.get("injection_attempts", [])),
    )


def run_from_state(
    state: ResearchState,
    *,
    question: str | None = None,
    elapsed_seconds: float = 0.0,
    usage: InMemoryRunRecorder | None = None,
    resumed: bool = False,
) -> ResearchRun:
    """Build the run object every consumer reads.

    Args:
        state: The final state the workflow returned.
        question: The original question. Read from the state when omitted, which
            is what a resumed run needs -- the caller supplying a research id
            does not know what was asked.
        usage: The tally of model and tool calls made during this execution.
    """
    error = state.get("error")
    if error is None and state.get("status") == ResearchStatus.FAILED.value:
        # A status of failed with no recorded error means routing stopped the
        # run rather than a stage raising -- the iteration ceiling. Left
        # unnamed, the run would report failure with nothing to explain it.
        error = "the workflow stopped before completing"

    return ResearchRun(
        research_id=state["research_id"],
        question=question if question is not None else state.get("question", ""),
        depth=ResearchDepth(state.get("depth", ResearchDepth.STANDARD.value)),
        spec=state.get("spec"),
        plan=state.get("plan"),
        task_results=list(state.get("task_results", [])),
        evidence_report=_extraction_report(state),
        elapsed_seconds=elapsed_seconds,
        error=error,
        usage=usage or InMemoryRunRecorder(),
        resumed=resumed,
    )
