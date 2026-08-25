"""What can be measured about a run without anyone's opinion.

**Every metric here is deterministic.** None of them asks a model to score
anything, and that is the same argument the evidence layer makes: asking a model
whether a model's work is good reintroduces the failure being measured. A judge
that shares the generator's blind spots agrees with it, and the agreement reads
as a high score.

The cost of that rule is honesty about what is *not* measured. Nothing here
scores whether an answer is correct, insightful, or well written -- those need a
human, and a number invented for them would be worse than their absence. What
these measure is whether the work was done properly:

*Citation correctness* -- does every quotation actually appear on the page it
cites? Re-run from the stored source text, not trusted from the extraction that
produced it, because the whole point is to catch the case where that extraction
was wrong.

*Citation completeness* -- does the prose cite what it asserts, and does every
marker resolve?

*Groundedness* -- does every published claim trace to evidence that survived
verification?

*Coverage* -- did the specification cover the ground the question needed?

*Source quality* -- how good are the pages, and how many distinct publishers?
Counting publishers rather than pages, because two pages on one domain are one
publisher and counting them twice is how a single vendor's docs become
"corroborated by multiple sources".

*Cost and latency* -- what it took, reported as unknown when unpriced rather
than as zero.

A metric returns ``None`` when the run did not get far enough to have one.
``None`` and ``0.0`` are different claims -- "no report was produced" against
"the report cited nothing" -- and averaging the second into a benchmark while
silently treating the first as a zero would make a crashed run look like a
merely bad one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from core.models.evidence import QuoteStatus, verify_quotation
from core.models.run import ResearchRun
from core.models.text import content_words, similarity


@dataclass(frozen=True, slots=True)
class Measurement:
    """One metric: a value, and the counts it was computed from.

    The counts travel with the value because a ratio on its own cannot be
    argued with. "Citation correctness 0.80" invites a shrug; "4 of 5
    citations verified" invites someone to go and look at the fifth.
    """

    value: float | None
    numerator: int = 0
    denominator: int = 0
    detail: str = ""

    @property
    def measured(self) -> bool:
        return self.value is not None

    def __str__(self) -> str:
        if self.value is None:
            return "not measured"
        return f"{self.value:.2f} ({self.numerator}/{self.denominator})"


@dataclass(frozen=True, slots=True)
class RunEvaluation:
    """Every metric for one benchmark question."""

    question_id: str
    research_id: str
    succeeded: bool
    error: str | None = None

    citation_correctness: Measurement = field(default_factory=lambda: Measurement(None))
    citation_completeness: Measurement = field(default_factory=lambda: Measurement(None))
    groundedness: Measurement = field(default_factory=lambda: Measurement(None))
    coverage: Measurement = field(default_factory=lambda: Measurement(None))
    verbatim_rate: Measurement = field(default_factory=lambda: Measurement(None))
    source_quality: Measurement = field(default_factory=lambda: Measurement(None))
    publisher_diversity: Measurement = field(default_factory=lambda: Measurement(None))

    classified_correctly: bool | None = None
    surfaced_contradiction: bool | None = None

    elapsed_seconds: float = 0.0
    cost_usd: Decimal | None = None
    total_tokens: int = 0
    sources: int = 0
    evidence: int = 0
    claims_published: int = 0
    claims_rejected: int = 0

    # Stamped per row rather than once per report. A benchmark that has to be
    # resumed across days -- which a 20-requests-per-day quota forces -- collects
    # rows from several commits and possibly several models, and a single header
    # over all of them claims a uniformity that was never measured.
    commit: str = ""
    model_cheap: str = ""
    model_strong: str = ""
    measured_at: str = ""

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> RunEvaluation:
        """Rebuild one evaluation from its recorded JSON.

        Written out field by field rather than by unpacking the dict. A
        ``**row`` would accept whatever the file happened to contain and fail at
        the first attribute access somewhere else -- and a results file is
        exactly the kind of thing that outlives the schema that wrote it.
        """

        def measure(name: str) -> Measurement:
            value = row.get(name)
            if not isinstance(value, dict):
                return Measurement(None)
            return Measurement(
                value=value.get("value"),
                numerator=int(value.get("numerator", 0)),
                denominator=int(value.get("denominator", 0)),
                detail=str(value.get("detail", "")),
            )

        cost = row.get("cost_usd")
        return cls(
            question_id=str(row.get("question_id", "")),
            research_id=str(row.get("research_id", "")),
            succeeded=bool(row.get("succeeded", False)),
            error=row.get("error"),
            citation_correctness=measure("citation_correctness"),
            citation_completeness=measure("citation_completeness"),
            groundedness=measure("groundedness"),
            coverage=measure("coverage"),
            verbatim_rate=measure("verbatim_rate"),
            source_quality=measure("source_quality"),
            publisher_diversity=measure("publisher_diversity"),
            classified_correctly=row.get("classified_correctly"),
            surfaced_contradiction=row.get("surfaced_contradiction"),
            elapsed_seconds=float(row.get("elapsed_seconds", 0.0)),
            cost_usd=Decimal(str(cost)) if cost is not None else None,
            total_tokens=int(row.get("total_tokens", 0)),
            sources=int(row.get("sources", 0)),
            evidence=int(row.get("evidence", 0)),
            claims_published=int(row.get("claims_published", 0)),
            claims_rejected=int(row.get("claims_rejected", 0)),
            commit=str(row.get("commit", "")),
            model_cheap=str(row.get("model_cheap", "")),
            model_strong=str(row.get("model_strong", "")),
            measured_at=str(row.get("measured_at", "")),
        )


def citation_correctness(run: ResearchRun) -> Measurement:
    """Does every quotation in the report appear on the page it cites?

    Re-verified here against the stored source text rather than trusting the
    status the extraction recorded. That is the entire point: this metric exists
    to catch the case where the extraction's own verdict was wrong, and reading
    that verdict back would be marking its own homework.
    """
    if run.report is None:
        return Measurement(None, detail="no report")
    if not run.report.citations:
        return Measurement(None, detail="report cites nothing")

    text_by_source = {source.id: source.content for source in run.sources}
    verified = 0
    checked = 0
    for citation in run.report.citations:
        source_text = text_by_source.get(citation.source_id)
        if not source_text:
            # A citation pointing at a source that is not in the run at all.
            # Counted as checked and failed rather than skipped: an unresolvable
            # citation is the worst kind, and skipping it would improve the
            # score.
            checked += 1
            continue
        checked += 1
        if verify_quotation(citation.quote, source_text).status.is_quotable:
            verified += 1

    return Measurement(verified / checked if checked else None, verified, checked)


def citation_completeness(run: ResearchRun) -> Measurement:
    """Do the report's prose sections cite what they assert?

    Measured over the sections that make claims. The question, method and
    sources sections are assembled from the run's own record rather than
    written, so requiring citations from them would penalise the three parts of
    the report that are the most trustworthy.
    """
    if run.report is None:
        return Measurement(None, detail="no report")

    asserting = [section for section in run.report.sections if section.claim_ids]
    if not asserting:
        return Measurement(None, detail="no section makes a claim")

    cited = sum(1 for section in asserting if section.citation_numbers)
    unresolved = len(run.report.unresolved_markers)
    detail = f"{unresolved} unresolved marker(s)" if unresolved else ""
    return Measurement(cited / len(asserting), cited, len(asserting), detail)


def groundedness(run: ResearchRun) -> Measurement:
    """Does every published claim trace to evidence?

    The project's central promise, expressed as a number. A claim that reached
    the report with no evidence link is exactly the thing this system exists to
    make impossible, so a score below 1.00 here is a defect and not a
    dimension to optimise.
    """
    if not run.claims:
        return Measurement(None, detail="no claims")

    # Only the claims that would actually reach a report. A claim verification
    # rejected is not an ungrounded claim -- it is the system working, and
    # counting it here would push the score down exactly when the safeguard did
    # its job.
    published = [claim for claim in run.claims if claim.status.is_publishable]
    if not published:
        return Measurement(None, detail="every claim was rejected")

    grounded = sum(1 for claim in published if claim.evidence)
    return Measurement(grounded / len(published), grounded, len(published))


def coverage(run: ResearchRun, concepts: tuple[str, ...]) -> Measurement:
    """Did the specification set out to cover the ground the question needed?

    Checked against the *specification's scope*, not the report's prose. A
    keyword search over prose rewards a system for mentioning a word in passing;
    what matters is whether the run intended to cover the ground, because a run
    that narrowed a hard question into an easy one fails here while producing a
    perfectly well-cited report about the easy version.
    """
    if not concepts:
        return Measurement(None, detail="no concepts declared")
    if run.spec is None:
        return Measurement(None, detail="no specification")

    scope_text = " ".join([*run.spec.scope, *run.spec.success_criteria])
    scope_words = content_words(scope_text)
    if not scope_words:
        return Measurement(0.0, 0, len(concepts), "specification has empty scope")

    covered = sum(
        1 for concept in concepts if similarity(content_words(concept), scope_words) > 0.0
    )
    return Measurement(covered / len(concepts), covered, len(concepts))


def verbatim_rate(run: ResearchRun) -> Measurement:
    """What share of evidence matched its source word for word.

    Reported separately from correctness because a paraphrase is accepted and
    weighted down rather than rejected -- so a run can be fully "correct" while
    resting on rewording, and that is worth seeing.
    """
    if not run.evidence:
        return Measurement(None, detail="no evidence")

    verbatim = sum(
        1
        for item in run.evidence
        if item.verification and item.verification.status is QuoteStatus.VERBATIM
    )
    return Measurement(verbatim / len(run.evidence), verbatim, len(run.evidence))


def source_quality(run: ResearchRun) -> Measurement:
    """Mean domain-based quality of the sources actually used."""
    used = [source for source in run.sources if not source.fetch_failed]
    if not used:
        return Measurement(None, detail="no sources")

    mean = sum(source.quality_score for source in used) / len(used)
    return Measurement(mean, len(used), len(used))


def publisher_diversity(run: ResearchRun) -> Measurement:
    """Distinct publishers per source.

    Counting domains rather than pages, because two pages on one domain are one
    publisher -- and treating them as two is how a single vendor's own
    documentation becomes "corroborated by multiple independent sources".

    1.0 means every source was a different publisher; 0.2 means five pages from
    one site.
    """
    used = [source for source in run.sources if not source.fetch_failed]
    if not used:
        return Measurement(None, detail="no sources")

    domains = {source.domain for source in used if source.domain}
    return Measurement(len(domains) / len(used), len(domains), len(used))


def surfaced_contradiction(run: ResearchRun) -> bool:
    """Whether the run reported a disagreement rather than averaging it away.

    Checked only on questions marked contested. A system that quietly resolves a
    real disagreement scores well on every other metric while doing the single
    most misleading thing available to it, and nothing else here would notice.
    """
    analysis = run.analysis
    if analysis is not None and getattr(analysis, "contradictions", None):
        return True
    if run.verification and any(
        verdict.contradicting_evidence_ids for verdict in run.verification.verdicts.values()
    ):
        return True
    return any(claim.conflicts_with for claim in run.claims)


def evaluate_run(
    run: ResearchRun,
    *,
    question_id: str,
    concepts: tuple[str, ...] = (),
    expected_type: str | None = None,
    contested: bool = False,
) -> RunEvaluation:
    """Compute every metric for one run."""
    classified = None
    if expected_type is not None and run.spec is not None:
        classified = run.spec.research_type.value == expected_type

    return RunEvaluation(
        question_id=question_id,
        research_id=run.research_id,
        succeeded=run.error is None and run.report is not None,
        error=run.error,
        citation_correctness=citation_correctness(run),
        citation_completeness=citation_completeness(run),
        groundedness=groundedness(run),
        coverage=coverage(run, concepts),
        verbatim_rate=verbatim_rate(run),
        source_quality=source_quality(run),
        publisher_diversity=publisher_diversity(run),
        classified_correctly=classified,
        surfaced_contradiction=surfaced_contradiction(run) if contested else None,
        elapsed_seconds=run.elapsed_seconds,
        cost_usd=run.usage.total_cost(),
        total_tokens=run.usage.total_tokens(),
        sources=len(run.sources),
        evidence=len(run.evidence),
        claims_published=sum(1 for claim in run.claims if claim.status.is_publishable),
        claims_rejected=sum(1 for claim in run.claims if not claim.status.is_publishable),
    )


def aggregate(results: list[RunEvaluation]) -> dict[str, Measurement]:
    """Mean of each metric across runs, over the runs that had one.

    Runs with no value for a metric are excluded from its denominator rather
    than counted as zero. A run that crashed before writing a report has no
    citation correctness, and folding that in as 0.00 would report a retrieval
    outage as a citation problem -- attributing a failure to the wrong stage,
    which is the specific thing this whole benchmark exists to avoid.
    """
    names = (
        "citation_correctness",
        "citation_completeness",
        "groundedness",
        "coverage",
        "verbatim_rate",
        "source_quality",
        "publisher_diversity",
    )

    summary: dict[str, Measurement] = {}
    for name in names:
        values = [
            getattr(result, name).value for result in results if getattr(result, name).measured
        ]
        summary[name] = Measurement(
            sum(values) / len(values) if values else None,
            len(values),
            len(results),
            "" if values else "no run produced this metric",
        )
    return summary


__all__ = [
    "Measurement",
    "RunEvaluation",
    "aggregate",
    "citation_completeness",
    "citation_correctness",
    "coverage",
    "evaluate_run",
    "groundedness",
    "publisher_diversity",
    "source_quality",
    "surfaced_contradiction",
    "verbatim_rate",
]
