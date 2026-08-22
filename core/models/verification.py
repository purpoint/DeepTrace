"""Verification: whether a claim's evidence actually supports it.

The distinction this stage exists for is between a citation being *present* and
a claim being *supported*. Everything upstream establishes presence: the quote
verifier proves a passage appears in its source, and grounding proves a citation
resolves to a passage that exists. Neither asks the question a reader would ask,
which is whether the passage says what the claim says it says.

Three ways a claim with perfect citations can still be wrong:

*The passage is about the topic, not about the claim.* It mentions partitions;
the claim asserts something specific about partitions that the passage never
states.

*The claim is broader than its evidence.* One page describing one system becomes
"always", "never", or "all systems". This is the most common failure and the
hardest to see, because every individual citation checks out.

*Something else in the run contradicts it.* The analyst cited what supported the
claim. A passage from another task that undercuts it was never compared.

The first and third need judgement, so a model makes them. The second has a
deterministic component -- a universal quantifier in a claim that appears in
none of its evidence -- and that part is code, because it is the failure most
likely to be waved through by a model asked to assess its own kind of output.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from core.models.claim import Claim, ClaimStatus
from core.models.evidence import Evidence
from core.models.text import WORD, content_words, similarity

QUANTIFIERS = frozenset(
    [
        "all",
        "always",
        "any",
        "cannot",
        "completely",
        "entirely",
        "every",
        "everything",
        "guarantee",
        "guaranteed",
        "guarantees",
        "impossible",
        "invariably",
        "never",
        "none",
        "universally",
        "must",
    ]
)
"""Words that widen a claim past what a single passage can establish.

A claim saying "Kafka always preserves order" needs a source that says always.
If no supporting passage contains the word, the claim has been widened somewhere
between the page and the conclusion -- which is the overgeneralization this
check exists to catch, and the one hardest to see by eye because every citation
still resolves.
"""

FOLLOW_UP_SIMILARITY_THRESHOLD = 0.7
"""Above this overlap with the original question, a follow-up is a repeat.

Re-running the search that already failed to settle a claim spends the same
money to get the same result. A follow-up has to be *narrower* -- aimed at the
specific thing verification could not confirm -- and this is the check that a
model asked for one actually produced one.
"""


class Disposition(StrEnum):
    """What to do about a claim, given its verdict.

    Separate from the verdict because they answer different questions. The
    verdict is about the evidence: does it support this. The disposition is
    about the run: what happens now. A claim can be partially supported and
    still pass, if what is missing is not worth another search.
    """

    PASS = "pass"  # noqa: S105 - a disposition, not a credential
    REVISE = "revise"
    RESEARCH_MORE = "research_more"


class ClaimVerification(BaseModel):
    """One claim, checked against the evidence available to the whole run."""

    model_config = {"extra": "forbid"}

    verdict: ClaimStatus = Field(
        description="Whether the evidence supports the claim as stated.",
    )
    disposition: Disposition = Disposition.PASS
    reasoning: str = Field(
        min_length=10,
        max_length=600,
        description="Why, in terms of what the evidence does and does not say.",
    )

    supporting_evidence_ids: list[str] = Field(default_factory=list, max_length=12)
    contradicting_evidence_ids: list[str] = Field(default_factory=list, max_length=12)

    overgeneralization: str | None = Field(
        default=None,
        max_length=300,
        description="How the claim reaches past its evidence, if it does.",
    )
    suggested_revision: str | None = Field(
        default=None,
        max_length=600,
        description="The claim restated within what the evidence supports.",
    )
    follow_up_question: str | None = Field(
        default=None,
        max_length=300,
        description="A narrower question that would settle this claim.",
    )

    @field_validator("verdict")
    @classmethod
    def _must_be_a_verdict(cls, value: ClaimStatus) -> ClaimStatus:
        """``proposed`` means "not yet checked", which is not an outcome.

        Allowing it would let a verification that decided nothing look like one
        that decided the claim was fine, since both would leave the status
        untouched.
        """
        if value is ClaimStatus.PROPOSED:
            raise ValueError("a verification must reach a verdict, not leave the claim proposed")
        return value


class VerificationReport(BaseModel):
    """What verification concluded about a run's claims."""

    model_config = {"extra": "forbid"}

    verdicts: dict[str, ClaimVerification] = Field(default_factory=dict)
    failed: list[tuple[str, str]] = Field(
        default_factory=list,
        description="Claims that could not be checked, as (claim id, reason).",
    )
    evidence_compared: int = 0
    """Passages weighed across all claims, including ones no claim cited.

    Reported because it is the measure of how wide the net was. A verification
    that only re-read what the analyst already cited confirms the analyst read
    them, and little else."""

    def verdict_for(self, claim_id: str) -> ClaimVerification | None:
        return self.verdicts.get(claim_id)

    @property
    def follow_up_questions(self) -> list[str]:
        return [
            verification.follow_up_question
            for verification in self.verdicts.values()
            if verification.follow_up_question
        ]

    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for verification in self.verdicts.values():
            tally[verification.verdict.value] = tally.get(verification.verdict.value, 0) + 1
        return tally

    def summary(self) -> str:
        counts = self.counts()
        parts = [f"{count} {status}" for status, count in sorted(counts.items())]
        if self.failed:
            parts.append(f"{len(self.failed)} could not be checked")
        return (
            f"{len(self.verdicts)} claims checked against {self.evidence_compared} "
            f"passages: {', '.join(parts) if parts else 'nothing to check'}"
        )


def overgeneralization(claim_text: str, passages: list[Evidence]) -> str | None:
    """The universal quantifier a claim uses that its evidence does not.

    Deterministic and deliberately narrow. It does not judge whether a claim is
    too broad in general -- that needs reading. It catches the specific, common
    case where a conclusion acquires an "always" or a "never" that appears
    nowhere in the text it rests on.

    Checked in code rather than asked of the model because a model assessing
    whether a confident sentence is too confident tends to agree with the
    sentence. The word is either in the evidence or it is not.
    """
    claim_words = set(WORD.findall(claim_text.lower()))
    reaching = claim_words & QUANTIFIERS
    if not reaching:
        return None

    supported = set()
    for passage in passages:
        supported |= set(WORD.findall(f"{passage.claim} {passage.supporting_text}".lower()))

    unsupported = sorted(reaching - supported)
    if not unsupported:
        return None
    return (
        f"the claim says {', '.join(repr(word) for word in unsupported)}, "
        f"which no supporting passage states"
    )


def is_narrower(follow_up: str, original: str) -> bool:
    """Whether a follow-up question asks something new.

    A verification that could not settle a claim and responds by re-asking the
    question that produced the claim has learned nothing and will spend the
    same money to find the same thing. The check is word overlap against the
    original, which is crude but catches the actual failure: a "narrower"
    question that is the original with two words changed.
    """
    if not follow_up.strip():
        return False
    return similarity(content_words(follow_up), content_words(original)) < (
        FOLLOW_UP_SIMILARITY_THRESHOLD
    )


def cap_verdict(claim: Claim, verdict: ClaimStatus) -> ClaimStatus:
    """Lower a verdict the evidence cannot carry, whatever the model decided.

    A claim resting entirely on paraphrase cannot be fully supported: what was
    checked against the source is a token overlap, not the wording the claim
    relies on. It can be partially supported, and it can be unsupported, but
    "supported" asserts more than the pipeline established.

    A cap rather than a rewrite: a model that says unsupported is believed. This
    only refuses to let a verdict be stronger than its foundation.
    """
    if verdict is ClaimStatus.SUPPORTED and not claim.is_quoted:
        return ClaimStatus.PARTIALLY_SUPPORTED
    return verdict


def apply(claim: Claim, verification: ClaimVerification) -> Claim:
    """Return the claim with its verdict recorded.

    A conflicting claim keeps that status: a contradiction the analyst
    identified is not overwritten by a verifier that looked at one side of it.
    Verification can find a *new* conflict, but it cannot resolve an existing
    one by ignoring it.
    """
    if claim.status is ClaimStatus.CONFLICTING:
        return claim
    return claim.model_copy(update={"status": cap_verdict(claim, verification.verdict)})
