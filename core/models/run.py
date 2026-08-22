"""The result of one research run.

Every entry point produces one of these: the CLI, the worker, and eventually the
API. It holds the intermediate stages, not only the final output, because the
point of the system is that a conclusion can be walked back to what produced it.
The plan and the per-task results are part of the result rather than scaffolding
discarded once the next stage consumes them.

It lives in the models layer rather than beside whichever module happens to
execute a run. The persistence layer writes one, the CLI prints one, and the
workflow produces one -- so it belongs to none of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.config import ResearchDepth
from core.models.evidence import Evidence, EvidenceExtractionReport
from core.models.plan import ResearchPlan
from core.models.query import QuerySpec
from core.models.research import TaskResult
from core.models.source import Source
from core.observability.recorder import InMemoryRunRecorder


@dataclass(slots=True)
class ResearchRun:
    """Everything one end-to-end run produced."""

    research_id: str
    question: str
    depth: ResearchDepth
    spec: QuerySpec | None = None
    plan: ResearchPlan | None = None
    task_results: list[TaskResult] = field(default_factory=list)
    evidence_report: EvidenceExtractionReport | None = None
    elapsed_seconds: float = 0.0
    error: str | None = None
    usage: InMemoryRunRecorder = field(default_factory=InMemoryRunRecorder)
    """Every model and tool call this run made, for the cost and trace summary."""

    resumed: bool = False
    """Whether this run continued from a checkpoint rather than starting fresh.

    Recorded because it changes how the cost summary should be read: a resumed
    run's tally covers only the steps executed this time, and the work restored
    from the checkpoint was paid for on an earlier attempt.
    """

    @property
    def evidence(self) -> list[Evidence]:
        return self.evidence_report.evidence if self.evidence_report else []

    @property
    def sources(self) -> list[Source]:
        return [source for result in self.task_results for source in result.sources]

    @property
    def succeeded(self) -> bool:
        return self.error is None and bool(self.evidence)
