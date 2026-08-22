"""Tests for the analyst.

This is the first stage that says something the sources did not, so the tests
that matter are adversarial: what happens when the model cites evidence that
does not exist, when it claims more confidence than the evidence carries, and
when it is handed almost nothing and asked to conclude something anyway.

A fabricated finding reads exactly like a sound one. That is the whole problem,
and it is why none of these guarantees is a prompt instruction.
"""

from __future__ import annotations

import json

import pytest

from core.agents.analyst import AnalystAgent
from core.llm.client import LLMClient, ModelRouter
from core.models.analysis import (
    Analysis,
    Confidence,
    Contradiction,
    Finding,
    Recommendation,
    TradeOff,
    evidence_labels,
    ground,
    publishers,
)
from core.models.evidence import (
    Evidence,
    QuoteStatus,
    QuoteVerification,
    SupportStrength,
)
from core.models.source import Source, SourceType
from tests.fakes import FakeProvider

pytestmark = pytest.mark.unit

ROUTER = ModelRouter("fake", "cheap-model", "strong-model", "embed-model")


def evidence(
    evidence_id: str = "ev_1",
    source_id: str = "src_1",
    *,
    status: QuoteStatus = QuoteStatus.VERBATIM,
    claim: str = "Kafka preserves order within a partition.",
) -> Evidence:
    return Evidence(
        id=evidence_id,
        source_id=source_id,
        task_id="ordering",
        claim=claim,
        supporting_text="Records are appended in the order they are sent.",
        location="Ordering guarantees",
        support_strength=SupportStrength.STRONG,
        verification=QuoteVerification(status=status, similarity=1.0),
        source_quality=0.97,
    )


def source(source_id: str = "src_1", domain: str = "kafka.apache.org") -> Source:
    return Source(
        id=source_id,
        url=f"https://{domain}/docs/{source_id}",
        title="Documentation",
        domain=domain,
        source_type=SourceType.OFFICIAL_DOCS,
        quality_score=0.97,
        task_id="ordering",
        content="Records are appended in the order they are sent." * 12,
        word_count=90,
    )


def analysis(**overrides: object) -> Analysis:
    values: dict[str, object] = {
        "summary": "The evidence describes partition-level ordering guarantees.",
        "findings": [],
        "tradeoffs": [],
        "contradictions": [],
        "recommendations": [],
        "open_questions": [],
    }
    values.update(overrides)
    return Analysis(**values)  # type: ignore[arg-type]


def finding(statement: str = "Kafka preserves order within a partition.", **kw: object) -> Finding:
    return Finding(statement=statement, **kw)  # type: ignore[arg-type]


class TestFabricatedCitations:
    """The rule the whole stage exists for.

    A model asked to cite its sources will occasionally cite one that is not
    there. The citation is checked by lookup, not by asking a model whether the
    citation looks right -- the same reason quotation verification is string
    matching.
    """

    def test_a_finding_citing_evidence_that_does_not_exist_is_dropped(self) -> None:
        report = ground(
            analysis(findings=[finding(evidence_ids=["E7"], confidence=Confidence.HIGH)]),
            [evidence()],
        )

        assert report.analysis.findings == []
        assert report.dropped
        assert "resolve" in report.dropped[0][1]

    def test_a_finding_citing_nothing_at_all_is_dropped(self) -> None:
        """An uncited conclusion is not a weak conclusion, it is an unsupported
        one. Keeping it and marking it low confidence would put a sentence
        nobody can check into the report."""
        report = ground(analysis(findings=[finding(evidence_ids=[])]), [evidence()])

        assert report.analysis.findings == []

    def test_the_resolvable_half_of_a_citation_survives(self) -> None:
        """A finding citing E1 and E9 rests partly on real evidence. It is kept
        with the citation that resolves, not discarded for the one that does
        not -- but it never carries the broken reference forward."""
        report = ground(
            analysis(findings=[finding(evidence_ids=["E1", "E9"])]),
            [evidence("ev_1")],
        )

        kept = report.analysis.findings[0]
        assert kept.evidence_ids == ["ev_1"]

    def test_labels_are_rewritten_into_real_evidence_ids(self) -> None:
        """What the report cites has to be resolvable outside this run. E1 means
        nothing to a reader, or to the database."""
        report = ground(
            analysis(findings=[finding(evidence_ids=["E2"])]),
            [evidence("ev_1"), evidence("ev_2", "src_2")],
        )

        assert report.analysis.findings[0].evidence_ids == ["ev_2"]

    def test_a_label_the_model_invented_has_nowhere_to_resolve(self) -> None:
        """Labels are ours, positional, and handed to the model. That is what
        makes an invented citation detectable rather than merely unlikely."""
        table = evidence_labels([evidence("ev_1"), evidence("ev_2", "src_2")])

        assert set(table) == {"E1", "E2"}
        assert "E3" not in table

    def test_a_contradiction_with_one_side_uncited_is_dropped(self) -> None:
        """One side citing nothing is not a disagreement between sources. It is
        a disagreement between a source and the model."""
        report = ground(
            analysis(
                contradictions=[
                    Contradiction(
                        subject="ordering across partitions",
                        position_a="Order is preserved globally.",
                        evidence_ids_a=["E1"],
                        position_b="Order is only per partition.",
                        evidence_ids_b=[],
                    )
                ]
            ),
            [evidence()],
        )

        assert report.analysis.contradictions == []
        assert "side" in report.dropped[0][1]

    def test_a_genuine_contradiction_survives_with_both_sides(self) -> None:
        """Contradictions are the one thing that must not be smoothed away."""
        report = ground(
            analysis(
                contradictions=[
                    Contradiction(
                        subject="ordering across partitions",
                        position_a="Order is preserved globally.",
                        evidence_ids_a=["E1"],
                        position_b="Order is only per partition.",
                        evidence_ids_b=["E2"],
                    )
                ]
            ),
            [evidence("ev_1"), evidence("ev_2", "src_2")],
        )

        kept = report.analysis.contradictions[0]
        assert kept.evidence_ids_a == ["ev_1"]
        assert kept.evidence_ids_b == ["ev_2"]

    def test_what_was_dropped_is_reported(self) -> None:
        """A conclusion that cited nothing real is a fact about the run.
        Silently removing it leaves an analysis that looks smaller than it was
        with no way to tell why."""
        report = ground(
            analysis(findings=[finding("Something entirely invented here.", evidence_ids=["E9"])]),
            [evidence()],
        )

        assert report.dropped[0][0].startswith("Something entirely invented")
        assert report.drop_rate == 1.0


class TestConfidenceCalibration:
    """A model rates its own confidence, and rates it generously."""

    def test_one_publisher_cannot_support_high_confidence(self) -> None:
        report = ground(
            analysis(findings=[finding(evidence_ids=["E1"], confidence=Confidence.HIGH)]),
            [evidence()],
            domains={"src_1": "kafka.apache.org"},
        )

        assert report.analysis.findings[0].confidence is Confidence.MODERATE

    def test_two_pages_on_one_site_are_one_publisher(self) -> None:
        """The overstatement this guards: a single vendor's documentation
        becoming "corroborated by multiple sources" because it was paginated."""
        report = ground(
            analysis(findings=[finding(evidence_ids=["E1", "E2"], confidence=Confidence.HIGH)]),
            [evidence("ev_1", "src_1"), evidence("ev_2", "src_2")],
            domains={"src_1": "kafka.apache.org", "src_2": "kafka.apache.org"},
        )

        kept = report.analysis.findings[0]
        assert kept.corroborating_domains == 1
        assert kept.confidence is Confidence.MODERATE
        assert kept.is_corroborated is False

    def test_two_publishers_can_support_high_confidence(self) -> None:
        report = ground(
            analysis(findings=[finding(evidence_ids=["E1", "E2"], confidence=Confidence.HIGH)]),
            [evidence("ev_1", "src_1"), evidence("ev_2", "src_2")],
            domains={"src_1": "kafka.apache.org", "src_2": "docs.confluent.io"},
        )

        kept = report.analysis.findings[0]
        assert kept.corroborating_domains == 2
        assert kept.confidence is Confidence.HIGH
        assert kept.is_corroborated

    def test_a_finding_resting_only_on_paraphrase_is_lowered(self) -> None:
        """Paraphrased evidence was matched by token overlap, not quoted. The
        wording the conclusion relies on is not the wording that was checked."""
        report = ground(
            analysis(findings=[finding(evidence_ids=["E1", "E2"], confidence=Confidence.HIGH)]),
            [
                evidence("ev_1", "src_1", status=QuoteStatus.PARAPHRASED),
                evidence("ev_2", "src_2", status=QuoteStatus.PARAPHRASED),
            ],
            domains={"src_1": "kafka.apache.org", "src_2": "docs.confluent.io"},
        )

        assert report.analysis.findings[0].confidence is Confidence.MODERATE

    def test_an_unknown_domain_counts_as_its_own_publisher(self) -> None:
        """The cautious direction: under-merge rather than invent independence
        that was never established."""
        assert len(publishers([evidence("ev_1", "src_1")], {})) == 1

    def test_a_recommendation_is_calibrated_like_a_finding(self) -> None:
        report = ground(
            analysis(
                recommendations=[
                    Recommendation(
                        recommendation="Route ordered records through one partition.",
                        condition="when total ordering matters more than throughput",
                        evidence_ids=["E1"],
                        confidence=Confidence.HIGH,
                    )
                ]
            ),
            [evidence()],
            domains={"src_1": "kafka.apache.org"},
        )

        assert report.analysis.recommendations[0].confidence is Confidence.MODERATE


class TestOpenQuestionsSurvive:
    def test_a_gap_needs_no_citation(self) -> None:
        """An open question is a statement about absence. Requiring it to cite
        evidence would delete exactly the honesty it exists to provide."""
        report = ground(
            analysis(
                open_questions=[
                    {  # type: ignore[list-item]
                        "question": "How does ordering behave during a partition reassignment?",
                        "why_unanswered": "No retrieved source discusses reassignment.",
                    }
                ]
            ),
            [evidence()],
        )

        assert len(report.analysis.open_questions) == 1


class TestTradeOffs:
    def test_a_tradeoff_keeps_both_halves(self) -> None:
        report = ground(
            analysis(
                tradeoffs=[
                    TradeOff(
                        subject="single-partition ordering",
                        benefit="Total ordering across all records is preserved.",
                        cost="Throughput is limited to one consumer per group.",
                        evidence_ids=["E1"],
                    )
                ]
            ),
            [evidence()],
        )

        kept = report.analysis.tradeoffs[0]
        assert kept.benefit and kept.cost
        assert kept.evidence_ids == ["ev_1"]


def make_agent(*responses: object) -> AnalystAgent:
    return AnalystAgent(LLMClient(FakeProvider(responses), router=ROUTER, max_repair_attempts=0))


class TestTheAgent:
    async def test_rejected_evidence_never_reaches_the_model(self) -> None:
        """The filter is upstream of the prompt. No instruction has to hold for
        a conclusion to be unable to rest on a passage the verifier refused."""
        agent = make_agent(
            json.dumps(
                {
                    "summary": "The evidence describes partition ordering guarantees.",
                    "findings": [],
                    "tradeoffs": [],
                    "contradictions": [],
                    "recommendations": [],
                    "open_questions": [],
                }
            )
        )
        pool = [
            evidence("ev_good", "src_1"),
            evidence("ev_bad", "src_2", status=QuoteStatus.NOT_FOUND),
        ]

        await agent.analyse(pool, question="q", sources=[source()])

        sent = agent.client.provider.requests[0].messages[1].content  # type: ignore[attr-defined]
        assert "E1" in sent
        assert "E2" not in sent, "a rejected passage was offered to the analyst"

    async def test_thin_evidence_produces_an_empty_analysis_not_an_invented_one(self) -> None:
        """The adversarial case: nothing to work with, and the stage still has
        to return an answer. The answer is that nothing was established."""
        agent = make_agent()

        report = await agent.analyse([], question="q")

        assert report.analysis.findings == []
        assert report.evidence_considered == 0
        assert "no verified evidence" in report.analysis.summary.lower()
        assert agent.client.provider.calls == 0, "a model was asked to analyse nothing"

    async def test_the_publisher_of_each_passage_is_shown(self) -> None:
        """Independence is something the analyst is asked to weigh, and it
        cannot weigh what it cannot see."""
        agent = make_agent(
            json.dumps(
                {
                    "summary": "The evidence describes partition ordering guarantees.",
                    "findings": [],
                    "tradeoffs": [],
                    "contradictions": [],
                    "recommendations": [],
                    "open_questions": [],
                }
            )
        )

        await agent.analyse([evidence()], question="q", sources=[source()])

        sent = agent.client.provider.requests[0].messages[1].content  # type: ignore[attr-defined]
        assert "kafka.apache.org" in sent

    async def test_the_strongest_evidence_survives_truncation(self) -> None:
        """The cap is on prompt size. What it sheds should be the weakest
        evidence, not whatever happened to be last in the list."""
        agent = AnalystAgent(
            LLMClient(
                FakeProvider(
                    [
                        json.dumps(
                            {
                                "summary": "The evidence describes ordering guarantees.",
                                "findings": [],
                                "tradeoffs": [],
                                "contradictions": [],
                                "recommendations": [],
                                "open_questions": [],
                            }
                        )
                    ]
                ),
                router=ROUTER,
                max_repair_attempts=0,
            ),
            max_evidence=1,
        )
        weak = evidence("ev_weak", "src_2", status=QuoteStatus.PARAPHRASED)
        strong = evidence("ev_strong", "src_1")

        report = await agent.analyse([weak, strong], question="q")

        assert report.evidence_considered == 1
        sent = agent.client.provider.requests[0].messages[1].content  # type: ignore[attr-defined]
        assert "ev_strong" not in sent  # ids are never shown, only labels
        assert sent.count("E1") >= 1
        assert "E2" not in sent
