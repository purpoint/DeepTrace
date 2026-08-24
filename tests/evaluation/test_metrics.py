"""Tests for the evaluation metrics.

These matter more than most tests in this project, because every number the
benchmark ever publishes is computed here. A metric that is subtly wrong does
not fail loudly -- it produces a plausible score that gets written into a
document and quoted, and the error is discovered by nobody.

So each metric is tested at both ends: the perfect run scores 1.00, the broken
run scores what it deserves, and the run that did not get far enough scores
*nothing* rather than zero. That last one is the case a benchmark gets wrong
most easily, and it is the one that misattributes a crash to a quality problem.
"""

from __future__ import annotations

import pytest

from core.config import ResearchDepth
from core.evaluation.metrics import (
    Measurement,
    RunEvaluation,
    aggregate,
    citation_correctness,
    coverage,
    evaluate_run,
    groundedness,
    publisher_diversity,
    source_quality,
    verbatim_rate,
)
from core.models.claim import Claim, ClaimKind, ClaimSet, ClaimStatus, EvidenceLink
from core.models.evidence import (
    Evidence,
    EvidenceExtractionReport,
    QuoteStatus,
    QuoteVerification,
    SupportStrength,
)
from core.models.query import QuerySpec
from core.models.report import Citation, Report, ReportSection, SectionKind
from core.models.research import SufficiencyVerdict, TaskResult
from core.models.run import ResearchRun
from core.models.source import Source, SourceType

PAGE = "Records are appended in the order they are sent to a single partition. " * 6


def a_source(source_id: str = "src_1", domain: str = "kafka.apache.org", quality: float = 0.97):
    return Source(
        id=source_id,
        url=f"https://{domain}/docs",
        title="Documentation",
        domain=domain,
        source_type=SourceType.OFFICIAL_DOCS,
        quality_score=quality,
        task_id="ordering",
        content=PAGE,
        word_count=90,
    )


def an_evidence(evidence_id: str = "ev_1", source_id: str = "src_1", verbatim: bool = True):
    return Evidence(
        id=evidence_id,
        source_id=source_id,
        task_id="ordering",
        claim="Kafka preserves order within a partition.",
        supporting_text="Records are appended in the order they are sent",
        location="Ordering",
        support_strength=SupportStrength.STRONG,
        source_quality=0.97,
        verification=QuoteVerification(
            status=QuoteStatus.VERBATIM if verbatim else QuoteStatus.PARAPHRASED,
            similarity=1.0 if verbatim else 0.8,
        ),
    )


def a_run(
    *,
    sources: list[Source] | None = None,
    evidence: list[Evidence] | None = None,
    claims: list[Claim] | None = None,
    report: Report | None = None,
    spec: QuerySpec | None = None,
) -> ResearchRun:
    sources = sources if sources is not None else [a_source()]
    run = ResearchRun(
        research_id="res_eval",
        question="How does Kafka guarantee message ordering?",
        depth=ResearchDepth.QUICK,
    )
    run.spec = spec
    run.task_results = [
        TaskResult(
            task_id="ordering",
            question="How are records ordered?",
            sources=sources,
            rounds=1,
            verdict=SufficiencyVerdict.SUFFICIENT,
            stop_reason="sufficient",
        )
    ]
    run.evidence_report = EvidenceExtractionReport(
        evidence=evidence if evidence is not None else [an_evidence()],
        sources_processed=len(sources),
    )
    if claims is not None:
        run.claim_set = ClaimSet(claims=claims)
    run.report = report
    return run


def a_claim(claim_id: str = "clm_1", *, evidence_ids: list[str], status: ClaimStatus):
    return Claim(
        id=claim_id,
        text="Kafka preserves record order within a partition.",
        kind=ClaimKind.FINDING,
        status=status,
        evidence=[
            EvidenceLink(evidence_id=eid, source_id="src_1", weight=0.9, verbatim=True)
            for eid in evidence_ids
        ],
    )


def a_report(*, quote: str, source_id: str = "src_1", cited: bool = True) -> Report:
    return Report(
        title="Ordering guarantees",
        question="How does Kafka guarantee message ordering?",
        sections=[
            ReportSection(
                kind=SectionKind.FINDINGS,
                body="Kafka preserves order within a partition [1].",
                claim_ids=["clm_1"],
                citation_numbers=[1] if cited else [],
            )
        ],
        citations=[
            Citation(
                number=1,
                evidence_id="ev_1",
                source_id=source_id,
                url="https://kafka.apache.org/docs",
                title="Documentation",
                domain="kafka.apache.org",
                quote=quote,
            )
        ],
    )


class TestCitationCorrectness:
    def test_a_quote_that_is_on_the_page_scores_one(self) -> None:
        run = a_run(report=a_report(quote="Records are appended in the order they are sent"))

        assert citation_correctness(run).value == pytest.approx(1.0)

    def test_a_fabricated_quote_scores_zero(self) -> None:
        """The failure this whole project exists to prevent, expressed as a
        number. It is re-verified from the stored page rather than read back
        from the extraction's own verdict -- checking that verdict would be
        marking its own homework."""
        run = a_run(
            report=a_report(quote="Kafka guarantees ordering across every partition globally")
        )

        assert citation_correctness(run).value == pytest.approx(0.0)

    def test_a_citation_pointing_at_a_missing_source_counts_as_wrong(self) -> None:
        """Not skipped. An unresolvable citation is the worst kind, and
        skipping it would raise the score."""
        run = a_run(report=a_report(quote="Records are appended", source_id="src_missing"))

        measured = citation_correctness(run)
        assert measured.value == pytest.approx(0.0)
        assert measured.denominator == 1

    def test_a_run_with_no_report_is_not_measured(self) -> None:
        """None, not zero. "No report was produced" and "the report cited
        nothing" are different failures, and averaging the first in as 0.0
        reports a crash as a citation problem."""
        assert citation_correctness(a_run()).measured is False


class TestGroundedness:
    def test_a_claim_backed_by_evidence_scores_one(self) -> None:
        run = a_run(claims=[a_claim(evidence_ids=["ev_1"], status=ClaimStatus.SUPPORTED)])

        assert groundedness(run).value == pytest.approx(1.0)

    def test_a_publishable_claim_with_no_evidence_scores_zero(self) -> None:
        run = a_run(claims=[a_claim(evidence_ids=[], status=ClaimStatus.SUPPORTED)])

        assert groundedness(run).value == pytest.approx(0.0)

    def test_rejected_claims_are_not_counted_against_it(self) -> None:
        """A claim verification threw out is the system working. Counting it as
        ungrounded would penalise a run for catching its own overreach, which
        would push the score down exactly when the safeguard did its job."""
        run = a_run(
            claims=[
                a_claim("clm_1", evidence_ids=["ev_1"], status=ClaimStatus.SUPPORTED),
                a_claim("clm_2", evidence_ids=[], status=ClaimStatus.UNSUPPORTED),
            ]
        )

        measured = groundedness(run)
        assert measured.value == pytest.approx(1.0)
        assert measured.denominator == 1


class TestCoverage:
    def test_concepts_reached_by_the_scope_score(self) -> None:
        spec = QuerySpec(
            normalized_question="How does Kafka order records?",
            research_type="explanation",
            scope=["partition ordering", "producer behaviour"],
            success_criteria=["offset semantics described"],
            time_sensitivity="static",
            requires_current_information=False,
        )
        run = a_run(spec=spec)

        measured = coverage(run, ("partition", "producer", "offset"))
        assert measured.value == pytest.approx(1.0)

    def test_a_narrowed_scope_scores_below_one(self) -> None:
        """The failure this metric exists for: a run that quietly turned a hard
        question into an easy one produces a perfectly well-cited report about
        the easy version, and every other metric is happy."""
        spec = QuerySpec(
            normalized_question="How does Kafka order records?",
            research_type="explanation",
            scope=["partition ordering"],
            success_criteria=["ordering described"],
            time_sensitivity="static",
            requires_current_information=False,
        )
        run = a_run(spec=spec)

        assert coverage(run, ("partition", "producer", "offset")).value == pytest.approx(1 / 3)

    def test_no_specification_is_not_measured(self) -> None:
        assert coverage(a_run(), ("partition",)).measured is False


class TestSourceMetrics:
    def test_publisher_diversity_counts_domains_not_pages(self) -> None:
        """Two pages on one domain are one publisher. Counting them as two is
        how a single vendor's own documentation becomes "corroborated by
        multiple independent sources"."""
        run = a_run(
            sources=[
                a_source("src_1", "kafka.apache.org"),
                a_source("src_2", "kafka.apache.org"),
            ]
        )

        assert publisher_diversity(run).value == pytest.approx(0.5)

    def test_distinct_publishers_score_one(self) -> None:
        run = a_run(
            sources=[a_source("src_1", "kafka.apache.org"), a_source("src_2", "confluent.io")]
        )

        assert publisher_diversity(run).value == pytest.approx(1.0)

    def test_source_quality_is_the_mean(self) -> None:
        run = a_run(
            sources=[
                a_source("src_1", "kafka.apache.org", quality=1.0),
                a_source("src_2", "blog.example.com", quality=0.5),
            ]
        )

        assert source_quality(run).value == pytest.approx(0.75)

    def test_verbatim_rate_separates_paraphrase(self) -> None:
        """A paraphrase is accepted and weighted down rather than rejected, so
        a run can be fully "correct" while resting on rewording."""
        run = a_run(
            evidence=[
                an_evidence("ev_1", verbatim=True),
                an_evidence("ev_2", verbatim=False),
            ]
        )

        assert verbatim_rate(run).value == pytest.approx(0.5)


class TestAggregation:
    def test_unmeasured_runs_are_excluded_not_zeroed(self) -> None:
        """The single most important line in this file.

        A run that crashed before writing a report has no citation correctness.
        Folding that in as 0.00 would report a retrieval outage as a citation
        problem -- attributing a failure to the wrong stage, which is what the
        whole benchmark exists to avoid.
        """
        measured = RunEvaluation(
            question_id="a",
            research_id="r",
            succeeded=True,
            citation_correctness=Measurement(1.0, 3, 3),
        )
        crashed = RunEvaluation(
            question_id="b", research_id="", succeeded=False, error="LLMServerError: 503"
        )

        summary = aggregate([measured, crashed])

        assert summary["citation_correctness"].value == pytest.approx(1.0)
        assert summary["citation_correctness"].numerator == 1  # runs that had one
        assert summary["citation_correctness"].denominator == 2  # runs attempted

    def test_a_metric_no_run_produced_is_not_measured(self) -> None:
        crashed = RunEvaluation(question_id="b", research_id="", succeeded=False)

        assert aggregate([crashed])["groundedness"].measured is False


class TestEvaluateRun:
    def test_classification_is_checked_against_the_expected_type(self) -> None:
        spec = QuerySpec(
            normalized_question="How does this compare?",
            research_type="comparison",
            scope=["a"],
            success_criteria=["b"],
            time_sensitivity="static",
            requires_current_information=False,
        )

        assert (
            evaluate_run(
                a_run(spec=spec), question_id="q1", expected_type="comparison"
            ).classified_correctly
            is True
        )
        assert (
            evaluate_run(
                a_run(spec=spec), question_id="q1", expected_type="explanation"
            ).classified_correctly
            is False
        )

    def test_contradiction_is_only_checked_on_contested_questions(self) -> None:
        """A question with one right answer should not be marked down for
        failing to produce a disagreement."""
        assert evaluate_run(a_run(), question_id="q1").surfaced_contradiction is None
        assert (
            evaluate_run(a_run(), question_id="q1", contested=True).surfaced_contradiction is False
        )

    def test_a_measurement_carries_its_counts(self) -> None:
        """A ratio on its own cannot be argued with. "0.80" invites a shrug;
        "4 of 5" invites someone to go and look at the fifth."""
        run = a_run(report=a_report(quote="Records are appended in the order they are sent"))

        assert str(evaluate_run(run, question_id="q1").citation_correctness) == "1.00 (1/1)"
