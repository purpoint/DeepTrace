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
from core.models.analysis import Analysis, AnalysisReport
from core.models.claim import Claim, ClaimSet
from core.models.evidence import Evidence, EvidenceExtractionReport
from core.models.plan import ResearchPlan
from core.models.query import QuerySpec
from core.models.report import Report
from core.models.research import TaskResult
from core.models.source import Source
from core.models.verification import VerificationReport
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
    analysis_report: AnalysisReport | None = None
    claim_set: ClaimSet | None = None
    verification: VerificationReport | None = None
    report: Report | None = None
    elapsed_seconds: float = 0.0
    error: str | None = None
    usage: InMemoryRunRecorder = field(default_factory=InMemoryRunRecorder)
    """Every model and tool call this run made, for the cost and trace summary."""

    research_loops: int = 0
    """Extra research rounds verification asked for and the budget allowed.

    Reported because it changes how the numbers read: a run that researched
    twice collected its sources in two passes, and a reader comparing source
    counts across runs should be able to see that."""

    problems: list[str] = field(default_factory=list)
    """Non-fatal failures the run recorded and continued past.

    Distinct from ``error``, which is the thing that ended a run. A failed
    analysis is deliberately not a failed run -- the evidence is collected and
    verified and worth more than the conclusions -- but the reason the report is
    missing has to survive to somebody who can read it. It did not: the graph
    accumulated these and `run_from_state` dropped them, so a run whose analyst
    never answered reported `status: completed, error: null` and gave a reader
    nothing at all to go on.
    """

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
    def analysis(self) -> Analysis | None:
        return self.analysis_report.analysis if self.analysis_report else None

    @property
    def claims(self) -> list[Claim]:
        return self.claim_set.claims if self.claim_set else []

    @property
    def publishable_claims(self) -> list[Claim]:
        """The claims a report may state.

        What "shows its work" means at the end: a run publishes what survived
        checking and says plainly that the rest did not."""
        return self.claim_set.publishable if self.claim_set else []

    @property
    def sources(self) -> list[Source]:
        return [source for result in self.task_results for source in result.sources]

    @property
    def succeeded(self) -> bool:
        """Whether the run produced the thing a run is for.

        A report, not merely evidence. Evidence with no report is a run that
        collected sources, verified passages, and then stopped -- which is worth
        keeping, and is not success. The first run of the deployed stack was
        exactly that: 7 sources, 18 verified passages, no analysis, no claims,
        and a status of "completed" that was indistinguishable from a run which
        answered the question.

        A report with nothing publishable still counts. `Reporter` assembles a
        "No verified answer" report when every claim was rejected, and that is
        the system working: it did the research and said plainly that nothing
        survived checking. Refusing to call that success would punish the
        honesty the whole pipeline is built around.
        """
        return self.error is None and bool(self.evidence) and self.report is not None
