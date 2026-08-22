"""Tests for the claim layer.

Claims are what a report cites and what a fact checker rejects, so the tests
that matter are about the two ways the derivation could quietly lose something:
by collapsing two statements that were not the same, and by keeping a claim that
nothing supports.

The dangerous case is a claim and its opposite. "Order is preserved across
partitions" and "order is not preserved across partitions" share every content
word, so any measure of overlap scores them as near-identical -- and merging
them would delete precisely the disagreement a reader most needs.
"""

from __future__ import annotations

import pytest

from core.models.analysis import (
    Analysis,
    Confidence,
    Contradiction,
    Finding,
    Recommendation,
)
from core.models.claim import (
    ClaimKind,
    ClaimStatus,
    EvidenceLink,
    build_claims,
)
from core.models.evidence import (
    Evidence,
    QuoteStatus,
    QuoteVerification,
    SupportStrength,
)
from core.models.text import negation_mismatch

pytestmark = pytest.mark.unit


def evidence(
    evidence_id: str = "ev_1",
    source_id: str = "src_1",
    *,
    status: QuoteStatus = QuoteStatus.VERBATIM,
) -> Evidence:
    return Evidence(
        id=evidence_id,
        source_id=source_id,
        task_id="ordering",
        claim="Kafka preserves order within a partition.",
        supporting_text="Records are appended in the order they are sent.",
        support_strength=SupportStrength.STRONG,
        verification=QuoteVerification(status=status, similarity=1.0),
        source_quality=0.97,
    )


POOL = [evidence("ev_1", "src_1"), evidence("ev_2", "src_2"), evidence("ev_3", "src_3")]


def analysis(**overrides: object) -> Analysis:
    values: dict[str, object] = {
        "summary": "The evidence describes partition-level ordering guarantees.",
    }
    values.update(overrides)
    return Analysis(**values)  # type: ignore[arg-type]


class TestClaimsRestOnEvidence:
    def test_a_finding_becomes_a_claim_linked_to_its_evidence(self) -> None:
        claims = build_claims(
            analysis(
                findings=[
                    Finding(
                        statement="Kafka preserves record order within a partition.",
                        evidence_ids=["ev_1", "ev_2"],
                        confidence=Confidence.HIGH,
                    )
                ]
            ),
            POOL,
            research_id="res_1",
        )

        claim = claims.claims[0]
        assert claim.kind is ClaimKind.FINDING
        assert claim.status is ClaimStatus.PROPOSED
        assert claim.evidence_ids == ["ev_1", "ev_2"]
        assert claim.source_ids == {"src_1", "src_2"}

    def test_a_claim_cannot_be_built_without_evidence(self) -> None:
        """Enforced where claims are made, not left to the hope that every
        caller ran grounding first."""
        claims = build_claims(
            analysis(
                findings=[
                    Finding(
                        statement="Something nothing supports at all.",
                        evidence_ids=["ev_missing"],
                    )
                ]
            ),
            POOL,
        )

        assert claims.claims == []
        assert claims.rejected

    def test_a_recommendation_keeps_the_condition_it_holds_under(self) -> None:
        claims = build_claims(
            analysis(
                recommendations=[
                    Recommendation(
                        recommendation="Route ordered records through one partition.",
                        condition="when total ordering matters more than throughput",
                        evidence_ids=["ev_1"],
                    )
                ]
            ),
            POOL,
        )

        claim = claims.claims[0]
        assert claim.kind is ClaimKind.RECOMMENDATION
        assert claim.condition is not None
        assert "throughput" in claim.condition

    def test_evidence_that_was_not_collected_is_not_linked(self) -> None:
        """A link to evidence outside the run is a citation that cannot be
        walked back, which is the failure the whole system is built around."""
        claims = build_claims(
            analysis(
                findings=[
                    Finding(
                        statement="Kafka preserves record order within a partition.",
                        evidence_ids=["ev_1", "ev_from_another_run"],
                    )
                ]
            ),
            POOL,
        )

        assert claims.claims[0].evidence_ids == ["ev_1"]


class TestRepetitionCollapses:
    def test_two_findings_saying_the_same_thing_become_one_claim(self) -> None:
        claims = build_claims(
            analysis(
                findings=[
                    Finding(
                        statement="Kafka preserves record ordering within a partition.",
                        evidence_ids=["ev_1"],
                    ),
                    Finding(
                        statement="Kafka preserves the ordering of records within a partition.",
                        evidence_ids=["ev_2"],
                    ),
                ]
            ),
            POOL,
        )

        assert len(claims.claims) == 1
        assert claims.claims[0].merged_from == 2

    def test_a_merged_claim_keeps_both_bodies_of_evidence(self) -> None:
        """The merge is a union, not a choice. Dropping one side's evidence
        would make a claim look less supported than it is."""
        claims = build_claims(
            analysis(
                findings=[
                    Finding(
                        statement="Kafka preserves record ordering within a partition.",
                        evidence_ids=["ev_1"],
                    ),
                    Finding(
                        statement="Kafka preserves the ordering of records within a partition.",
                        evidence_ids=["ev_2"],
                    ),
                ]
            ),
            POOL,
        )

        assert set(claims.claims[0].evidence_ids) == {"ev_1", "ev_2"}

    def test_merging_does_not_raise_confidence(self) -> None:
        """Two phrasings of one fact are not corroboration. Taking the higher
        confidence would manufacture certainty out of repetition."""
        claims = build_claims(
            analysis(
                findings=[
                    Finding(
                        statement="Kafka preserves record ordering within a partition.",
                        evidence_ids=["ev_1"],
                        confidence=Confidence.HIGH,
                    ),
                    Finding(
                        statement="Kafka preserves the ordering of records within a partition.",
                        evidence_ids=["ev_2"],
                        confidence=Confidence.LOW,
                    ),
                ]
            ),
            POOL,
        )

        assert claims.claims[0].confidence is Confidence.LOW

    def test_unrelated_findings_are_not_merged(self) -> None:
        claims = build_claims(
            analysis(
                findings=[
                    Finding(
                        statement="Kafka preserves record ordering within a partition.",
                        evidence_ids=["ev_1"],
                    ),
                    Finding(
                        statement="Consumer groups rebalance when a member leaves.",
                        evidence_ids=["ev_2"],
                    ),
                ]
            ),
            POOL,
        )

        assert len(claims.claims) == 2


class TestDisagreementSurvives:
    """The case word overlap gets exactly backwards."""

    def test_a_claim_is_never_merged_with_its_negation(self) -> None:
        claims = build_claims(
            analysis(
                findings=[
                    Finding(
                        statement="Record ordering is preserved across partitions.",
                        evidence_ids=["ev_1"],
                    ),
                    Finding(
                        statement="Record ordering is not preserved across partitions.",
                        evidence_ids=["ev_2"],
                    ),
                ]
            ),
            POOL,
        )

        assert len(claims.claims) == 2, "a claim was merged with its opposite"

    def test_negation_is_what_separates_them(self) -> None:
        """Overlap alone cannot: the two statements differ by one short word
        that carries the entire meaning."""
        assert negation_mismatch(
            "Record ordering is preserved across partitions.",
            "Record ordering is not preserved across partitions.",
        )
        assert not negation_mismatch(
            "Record ordering is preserved.",
            "The ordering of records is preserved.",
        )

    def test_a_contradiction_becomes_two_linked_positions(self) -> None:
        claims = build_claims(
            analysis(
                contradictions=[
                    Contradiction(
                        subject="in-flight requests",
                        position_a="Retries can reorder records above one in flight.",
                        evidence_ids_a=["ev_1"],
                        position_b="Idempotent producers preserve order up to five.",
                        evidence_ids_b=["ev_2"],
                    )
                ]
            ),
            POOL,
            research_id="res_1",
        )

        first, second = claims.claims
        assert first.kind is ClaimKind.POSITION
        assert first.status is ClaimStatus.CONFLICTING
        assert first.conflicts_with == [second.id]
        assert second.conflicts_with == [first.id]

    def test_conflicting_pairs_are_reported_once(self) -> None:
        claims = build_claims(
            analysis(
                contradictions=[
                    Contradiction(
                        subject="in-flight requests",
                        position_a="Retries can reorder records above one in flight.",
                        evidence_ids_a=["ev_1"],
                        position_b="Idempotent producers preserve order up to five.",
                        evidence_ids_b=["ev_2"],
                    )
                ]
            ),
            POOL,
        )

        assert len(claims.conflicting_pairs()) == 1

    def test_positions_never_merge_however_similar(self) -> None:
        """Two positions in a contradiction are the pair word overlap scores
        highest, and they are the pair that must never collapse."""
        claims = build_claims(
            analysis(
                contradictions=[
                    Contradiction(
                        subject="ordering across partitions",
                        position_a="Ordering across partitions is guaranteed by Kafka.",
                        evidence_ids_a=["ev_1"],
                        position_b="Ordering across partitions is guaranteed by Kafka only "
                        "with one partition.",
                        evidence_ids_b=["ev_2"],
                    )
                ]
            ),
            POOL,
        )

        assert len(claims.of_kind(ClaimKind.POSITION)) == 2

    def test_a_one_sided_contradiction_is_rejected(self) -> None:
        claims = build_claims(
            analysis(
                contradictions=[
                    Contradiction(
                        subject="ordering",
                        position_a="Ordering is preserved.",
                        evidence_ids_a=["ev_1"],
                        position_b="Ordering is lost on retry.",
                        evidence_ids_b=["ev_nonexistent"],
                    )
                ]
            ),
            POOL,
        )

        assert claims.claims == []
        assert claims.rejected


class TestTheGraphWalksBothWays:
    def test_everything_resting_on_one_passage_can_be_found(self) -> None:
        """The direction that matters when a source turns out to be wrong."""
        claims = build_claims(
            analysis(
                findings=[
                    Finding(
                        statement="Kafka preserves record ordering within a partition.",
                        evidence_ids=["ev_1"],
                    ),
                    Finding(
                        statement="Consumer groups rebalance when a member leaves.",
                        evidence_ids=["ev_1", "ev_2"],
                    ),
                ]
            ),
            POOL,
        )

        assert len(claims.resting_on("ev_1")) == 2
        assert len(claims.resting_on("ev_2")) == 1

    def test_claims_citing_a_source_can_be_found(self) -> None:
        claims = build_claims(
            analysis(
                findings=[
                    Finding(
                        statement="Kafka preserves record ordering within a partition.",
                        evidence_ids=["ev_3"],
                    )
                ]
            ),
            POOL,
        )

        assert len(claims.citing_source("src_3")) == 1
        assert claims.citing_source("src_1") == []


class TestClaimProperties:
    def test_strength_accumulates_but_is_capped(self) -> None:
        """More supporting passages is stronger, but quantity alone cannot
        manufacture certainty."""
        claims = build_claims(
            analysis(
                findings=[
                    Finding(
                        statement="Kafka preserves record ordering within a partition.",
                        evidence_ids=["ev_1", "ev_2", "ev_3"],
                    )
                ]
            ),
            POOL,
        )

        assert claims.claims[0].strength == 1.0

    def test_an_unsupported_claim_is_not_publishable(self) -> None:
        from core.models.claim import Claim

        claim = Claim(
            id="res_1:find_1",
            text="Something the checker rejected.",
            status=ClaimStatus.UNSUPPORTED,
            evidence=[EvidenceLink(evidence_id="ev_1", source_id="src_1")],
        )

        assert claim.status.is_publishable is False

    def test_a_conflicting_claim_is_publishable_as_a_disagreement(self) -> None:
        assert ClaimStatus.CONFLICTING.is_publishable
