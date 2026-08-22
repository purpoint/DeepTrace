"""Tests for verification.

The question this stage answers is not "is this claim true" but "do these
passages establish it". The tests are built around the three ways a claim with
perfect citations is still wrong: the passage is merely about the topic, the
claim is broader than its evidence, or something else in the run contradicts it.

The last one is why retrieval exists. Research is decomposed into tasks that
search separately, so the passage that undercuts a claim about producers is
often the one a consumer task retrieved -- and nothing before this stage brings
them into contact.
"""

from __future__ import annotations

import json

import pytest

from core.agents.fact_checker import FactChecker
from core.llm.client import LLMClient, ModelRouter
from core.models.claim import Claim, ClaimSet, ClaimStatus, EvidenceLink
from core.models.evidence import (
    Evidence,
    QuoteStatus,
    QuoteVerification,
    SupportStrength,
)
from core.models.verification import (
    ClaimVerification,
    Disposition,
    apply,
    cap_verdict,
    is_narrower,
    overgeneralization,
)
from core.retrieval import LexicalRetriever
from tests.fakes import FakeProvider

pytestmark = pytest.mark.unit

ROUTER = ModelRouter("fake", "cheap-model", "strong-model", "embed-model")

QUESTION = "How does Kafka guarantee message ordering within a partition?"


def evidence(
    evidence_id: str = "ev_1",
    source_id: str = "src_1",
    *,
    text: str = "Records are appended to a partition in the order they are sent.",
    claim: str = "Kafka preserves order within a partition.",
    status: QuoteStatus = QuoteStatus.VERBATIM,
) -> Evidence:
    return Evidence(
        id=evidence_id,
        source_id=source_id,
        task_id="ordering",
        claim=claim,
        supporting_text=text,
        support_strength=SupportStrength.STRONG,
        verification=QuoteVerification(status=status, similarity=1.0),
        source_quality=0.97,
    )


def claim(
    text: str = "Kafka preserves record order within a partition.",
    *,
    claim_id: str = "res_1:find_1",
    evidence_ids: tuple[str, ...] = ("ev_1",),
    verbatim: bool = True,
) -> Claim:
    return Claim(
        id=claim_id,
        text=text,
        evidence=[
            EvidenceLink(evidence_id=eid, source_id="src_1", weight=0.97, verbatim=verbatim)
            for eid in evidence_ids
        ],
    )


def verdict_json(**overrides: object) -> str:
    payload: dict[str, object] = {
        "verdict": "supported",
        "disposition": "pass",
        "reasoning": "The cited passage states the claim directly.",
        "supporting_evidence_ids": ["C1"],
        "contradicting_evidence_ids": [],
    }
    payload.update(overrides)
    return json.dumps(payload)


def checker(*responses: object, **kwargs: object) -> FactChecker:
    return FactChecker(
        LLMClient(FakeProvider(responses), router=ROUTER, max_repair_attempts=0),
        **kwargs,  # type: ignore[arg-type]
    )


class TestOvergeneralization:
    """The most common way a well-cited claim is wrong, and the hardest to see:
    every citation resolves, and the claim still says more than they do."""

    def test_a_quantifier_absent_from_the_evidence_is_caught(self) -> None:
        reaching = overgeneralization(
            "Kafka always preserves record order.",
            [evidence(text="Records are appended in the order they are sent.")],
        )

        assert reaching is not None
        assert "always" in reaching

    def test_a_quantifier_the_evidence_states_is_not_flagged(self) -> None:
        """A source that says "always" supports a claim that says always."""
        assert (
            overgeneralization(
                "Kafka always preserves record order.",
                [evidence(text="Ordering is always maintained within a partition.")],
            )
            is None
        )

    def test_a_claim_making_no_universal_statement_is_not_flagged(self) -> None:
        assert overgeneralization("Kafka preserves record order.", [evidence()]) is None

    async def test_a_supported_verdict_is_lowered_when_the_claim_reaches(self) -> None:
        """Checked in code because a model assessing whether a confident
        sentence is too confident tends to agree with the sentence."""
        agent = checker(verdict_json(verdict="supported"))
        claims = ClaimSet(claims=[claim("Kafka always preserves record order.")])

        checked, report = await agent.check(claims, [evidence()], question=QUESTION)

        assert checked.claims[0].status is ClaimStatus.PARTIALLY_SUPPORTED
        assert report.verdicts["res_1:find_1"].overgeneralization is not None


class TestVerdictsCannotExceedTheirFoundation:
    def test_a_claim_resting_only_on_paraphrase_cannot_be_supported(self) -> None:
        """What was checked against the source was token overlap, not the
        wording the claim relies on."""
        paraphrased = claim(verbatim=False)

        assert cap_verdict(paraphrased, ClaimStatus.SUPPORTED) is (ClaimStatus.PARTIALLY_SUPPORTED)

    def test_a_quoted_claim_can_be_supported(self) -> None:
        assert cap_verdict(claim(), ClaimStatus.SUPPORTED) is ClaimStatus.SUPPORTED

    def test_the_cap_never_raises_a_verdict(self) -> None:
        """A model that says unsupported is believed. This only refuses to let a
        verdict be stronger than its foundation."""
        assert cap_verdict(claim(), ClaimStatus.UNSUPPORTED) is ClaimStatus.UNSUPPORTED

    def test_a_verification_must_reach_a_verdict(self) -> None:
        """ "Proposed" means not yet checked. Allowing it would let a check that
        decided nothing look like one that found no problem."""
        with pytest.raises(ValueError, match="verdict"):
            ClaimVerification(verdict=ClaimStatus.PROPOSED, reasoning="undecided for now")


class TestConflictsAreNotOverwritten:
    def test_a_conflicting_claim_keeps_its_status(self) -> None:
        """Verification can find a new conflict; it cannot resolve an existing
        one by looking at a single side of it."""
        conflicted = claim().model_copy(update={"status": ClaimStatus.CONFLICTING})
        verification = ClaimVerification(
            verdict=ClaimStatus.SUPPORTED, reasoning="The cited passage states this."
        )

        assert apply(conflicted, verification).status is ClaimStatus.CONFLICTING


class TestFollowUpsMustBeNarrower:
    def test_repeating_the_research_question_is_rejected(self) -> None:
        assert not is_narrower(QUESTION, QUESTION)

    def test_a_genuinely_narrower_question_is_accepted(self) -> None:
        assert is_narrower(
            "Does max.in.flight above one reorder records when a retry occurs?",
            QUESTION,
        )

    async def test_a_repeated_question_is_dropped_and_the_disposition_falls_back(self) -> None:
        """Re-running the search that already failed to settle a claim spends
        the same money for the same result, so research_more with nothing
        narrower to ask is not a plan."""
        agent = checker(
            verdict_json(
                verdict="partially_supported",
                disposition="research_more",
                follow_up_question=QUESTION,
            )
        )

        _, report = await agent.check(ClaimSet(claims=[claim()]), [evidence()], question=QUESTION)

        verification = report.verdicts["res_1:find_1"]
        assert verification.follow_up_question is None
        assert verification.disposition is Disposition.REVISE

    async def test_a_narrower_question_survives_and_is_surfaced(self) -> None:
        narrower = "Does enabling idempotence change ordering with five in-flight requests?"
        agent = checker(
            verdict_json(
                verdict="partially_supported",
                disposition="research_more",
                follow_up_question=narrower,
            )
        )

        _, report = await agent.check(ClaimSet(claims=[claim()]), [evidence()], question=QUESTION)

        assert report.follow_up_questions == [narrower]


class TestEvidenceTheClaimDidNotCite:
    """The gap only this stage can close."""

    def test_retrieval_finds_a_passage_from_another_task(self) -> None:
        pool = [
            evidence("ev_1", text="Records are appended in the order they are sent."),
            evidence(
                "ev_2",
                "src_2",
                text="Ordering is not preserved when retries occur with several "
                "requests in flight.",
                claim="Retries can reorder records.",
            ),
            evidence(
                "ev_3",
                "src_3",
                text="Consumer groups rebalance when a member leaves.",
                claim="Rebalancing happens on membership change.",
            ),
        ]

        related = LexicalRetriever().related("Kafka preserves record ordering", pool, limit=2)

        assert [item.id for item in related][:1] == ["ev_1"]
        assert "ev_2" in [item.id for item in related]

    async def test_uncited_evidence_is_offered_to_the_model(self) -> None:
        agent = checker(verdict_json())
        pool = [
            evidence("ev_1"),
            evidence(
                "ev_2",
                "src_2",
                text="Ordering is not preserved when a retry occurs.",
                claim="Retries can reorder records.",
            ),
        ]

        await agent.check(ClaimSet(claims=[claim()]), pool, question=QUESTION)

        sent = agent.client.provider.requests[0].messages[1].content  # type: ignore[attr-defined]
        assert "C1" in sent
        assert "R1" in sent, "no uncited passage was brought into the comparison"

    async def test_how_wide_the_net_was_is_reported(self) -> None:
        """A verification that only re-read what the analyst cited confirms the
        analyst read them, and little else."""
        agent = checker(verdict_json())
        pool = [
            evidence("ev_1"),
            evidence("ev_2", "src_2", text="Records are appended in order to the log."),
        ]

        _, report = await agent.check(ClaimSet(claims=[claim()]), pool, question=QUESTION)

        assert report.evidence_compared == 2

    async def test_a_contradicting_passage_is_recorded_against_the_claim(self) -> None:
        agent = checker(
            verdict_json(
                verdict="conflicting",
                disposition="revise",
                contradicting_evidence_ids=["R1"],
            )
        )
        pool = [
            evidence("ev_1"),
            evidence(
                "ev_2",
                "src_2",
                text="Ordering is not preserved when a retry occurs in flight.",
                claim="Retries can reorder records.",
            ),
        ]

        _, report = await agent.check(ClaimSet(claims=[claim()]), pool, question=QUESTION)

        assert report.verdicts["res_1:find_1"].contradicting_evidence_ids == ["ev_2"]

    async def test_a_label_the_model_invented_resolves_to_nothing(self) -> None:
        """Same discipline as grounding an analysis: a verdict cannot cite a
        passage that was never compared."""
        agent = checker(verdict_json(supporting_evidence_ids=["C1", "R9"]))

        _, report = await agent.check(ClaimSet(claims=[claim()]), [evidence()], question=QUESTION)

        assert report.verdicts["res_1:find_1"].supporting_evidence_ids == ["ev_1"]


class TestFailureHandling:
    async def test_a_claim_that_could_not_be_checked_is_not_marked_refuted(self) -> None:
        """Failing to check something is not evidence against it. Converting one
        into the other would let a provider outage look like a refutation."""
        agent = checker("not valid json at all")

        checked, report = await agent.check(
            ClaimSet(claims=[claim()]), [evidence()], question=QUESTION
        )

        assert checked.claims[0].status is ClaimStatus.PROPOSED
        assert report.failed
        assert report.verdicts == {}

    async def test_one_claim_failing_does_not_stop_the_others(self) -> None:
        agent = FactChecker(
            LLMClient(
                FakeProvider(["not json", verdict_json()]),
                router=ROUTER,
                max_repair_attempts=0,
            ),
            max_concurrency=1,
        )
        claims = ClaimSet(
            claims=[
                claim(claim_id="res_1:find_1"),
                claim("Ordering is per partition, not global.", claim_id="res_1:find_2"),
            ]
        )

        checked, report = await agent.check(claims, [evidence()], question=QUESTION)

        assert len(report.failed) == 1
        assert len(report.verdicts) == 1
        assert checked.claims[1].status is ClaimStatus.SUPPORTED

    async def test_nothing_to_check_is_not_a_failure(self) -> None:
        agent = checker()

        checked, report = await agent.check(ClaimSet(), [], question=QUESTION)

        assert checked.claims == []
        assert report.verdicts == {}


class TestUnsupportedClaimsDoNotReachAReport:
    async def test_an_unsupported_claim_is_excluded_from_the_publishable_set(self) -> None:
        agent = checker(
            verdict_json(
                verdict="unsupported",
                disposition="revise",
                reasoning="The passage mentions partitions but never states this.",
            )
        )

        checked, _ = await agent.check(ClaimSet(claims=[claim()]), [evidence()], question=QUESTION)

        assert checked.claims[0].status is ClaimStatus.UNSUPPORTED
        assert checked.publishable == []


class TestWhatLexicalRetrievalMisses:
    """The limitation, pinned rather than left implicit.

    Word overlap finds passages that share vocabulary with a claim. A passage
    that contradicts a claim in different words is invisible to it -- and
    different words are exactly what a contradiction often uses. This is the gap
    embedding-based retrieval closes, and it is recorded here so the next
    implementation has a failing case to aim at rather than a vague ambition.
    """

    def test_a_contradiction_sharing_vocabulary_is_found(self) -> None:
        pool = [
            evidence(
                "ev_2",
                "src_2",
                text="Ordering is not preserved across partitions when keys differ.",
                claim="Ordering is not global.",
            )
        ]

        found = LexicalRetriever().related(
            "Kafka preserves record order across partitions.", pool, limit=3
        )

        assert [item.id for item in found] == ["ev_2"]

    def test_a_contradiction_in_different_words_is_missed(self) -> None:
        """The honest limit. A reader of the report should know the net was
        lexical, which is why the report states how much was compared."""
        pool = [
            evidence(
                "ev_2",
                "src_2",
                text="Producers may emit duplicates that arrive out of sequence.",
                claim="Duplicate delivery can disturb sequence.",
            )
        ]

        found = LexicalRetriever().related(
            "Kafka preserves record order within a partition.", pool, limit=3
        )

        assert found == [], "the lexical retriever is not expected to catch this yet"
