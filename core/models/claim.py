"""Claims: the unit a report cites, and the unit a fact checker can reject.

A finding is what the analyst concluded. A claim is that conclusion turned into
something the rest of the system can operate on individually: it has an id, a
status, and explicit links to the evidence it rests on, so it can be verified,
revised, or thrown out without disturbing anything else.

The difference is not bookkeeping. A report built from findings can only accept
or reject an analysis whole. A report built from claims can publish the eleven
that survived verification and say plainly that three did not -- which is the
only version of "shows its work" that survives contact with a model that is
sometimes wrong.

Three rules are structural here:

*A claim cannot exist without evidence.* Not "should not" -- the builder refuses
to make one. Deriving claims from a grounded analysis means this should never
trigger, and it is enforced anyway: the rule belongs where claims are made, not
in the hope that every future caller ran grounding first.

*Repetition is collapsed, disagreement is not.* Two findings saying the same
thing become one claim citing both bodies of evidence. Two findings saying
opposite things stay two claims, linked as conflicting -- and telling those
apart is the interesting part, because they look nearly identical to any measure
of word overlap.

*A conflict is recorded at derivation, not discovered later.* The analyst already
identified which positions contradict each other. Dropping that and re-deriving
it downstream would mean losing information the system already had.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from core.models.analysis import Analysis, Confidence
from core.models.evidence import Evidence
from core.models.text import content_words, negation_mismatch, similarity

MERGE_SIMILARITY_THRESHOLD = 0.85
"""Word overlap above which two claims of the same kind are treated as one.

The same threshold the planner uses for duplicate tasks, and the same
reasoning: deterministic, free, and run on every analysis. It is deliberately
high. Merging two claims that were not really the same loses a distinction a
reader needed; leaving two near-duplicates costs a repeated line.
"""


class ClaimStatus(StrEnum):
    """Where a claim stands.

    ``PROPOSED`` is the honest starting point: derived from evidence, not yet
    checked. The verified statuses are set by the fact checker, which is a
    separate milestone -- they are defined here because the claim is what
    carries them, but nothing in this module invents one.

    ``CONFLICTING`` is the exception. It is set at derivation, because a
    contradiction the analyst already identified is knowledge the system has
    now, and waiting for a verifier to rediscover it would be pretending not to
    know something.
    """

    PROPOSED = "proposed"
    SUPPORTED = "supported"
    PARTIALLY_SUPPORTED = "partially_supported"
    UNSUPPORTED = "unsupported"
    CONFLICTING = "conflicting"

    @property
    def is_publishable(self) -> bool:
        """Whether a report may state this claim.

        Conflicting claims are publishable -- as a disagreement, with both
        sides. Unsupported ones are not, at all.
        """
        return self is not ClaimStatus.UNSUPPORTED


class ClaimKind(StrEnum):
    """What kind of assertion a claim is.

    Kept because they are not interchangeable downstream: a report states
    findings, offers recommendations with their conditions, and presents
    positions in pairs. Flattening them would leave the report guessing.
    """

    FINDING = "finding"
    TRADEOFF = "tradeoff"
    RECOMMENDATION = "recommendation"
    POSITION = "position"


class EvidenceLink(BaseModel):
    """One claim's dependence on one piece of evidence.

    An edge, not a copy. It carries the ids needed to walk back to the passage
    and the page, plus the two properties a verifier reads without loading the
    evidence itself: how much weight it carries and whether it was quoted or
    only paraphrased.
    """

    model_config = {"extra": "forbid"}

    evidence_id: str = Field(min_length=3)
    source_id: str = Field(min_length=3)
    weight: float = Field(default=0.0, ge=0.0, le=1.0)
    verbatim: bool = False

    @classmethod
    def to(cls, evidence: Evidence) -> EvidenceLink:
        return cls(
            evidence_id=evidence.id,
            source_id=evidence.source_id,
            weight=evidence.weight,
            verbatim=evidence.is_verified,
        )


class Claim(BaseModel):
    """One assertion, with everything needed to check it."""

    model_config = {"extra": "forbid"}

    id: str = Field(min_length=3)
    text: str = Field(min_length=10, max_length=600)
    kind: ClaimKind = ClaimKind.FINDING
    status: ClaimStatus = ClaimStatus.PROPOSED
    confidence: Confidence = Confidence.MODERATE

    evidence: list[EvidenceLink] = Field(default_factory=list)
    conflicts_with: list[str] = Field(
        default_factory=list,
        description="Ids of claims this one contradicts.",
    )

    condition: str | None = Field(
        default=None,
        max_length=300,
        description="For a recommendation, when it holds.",
    )
    merged_from: int = Field(
        default=1,
        description="How many of the analyst's conclusions collapsed into this claim.",
    )
    corroborating_publishers: int = Field(
        default=1,
        description="Distinct publishers behind this claim, carried from the analysis.",
    )
    """Carried rather than recomputed, because a claim knows its sources but not
    who published them -- and counting sources instead of publishers is how two
    pages of one vendor's documentation become "multiple sources agree".

    Defaults to one for claims the analyst did not count, which under-claims
    rather than over-claims: the cautious direction is to leave corroboration
    unstated, not to invent it."""

    @field_validator("text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be blank")
        return cleaned

    @property
    def evidence_ids(self) -> list[str]:
        return [link.evidence_id for link in self.evidence]

    @property
    def source_ids(self) -> set[str]:
        return {link.source_id for link in self.evidence}

    @property
    def is_quoted(self) -> bool:
        """Whether any supporting passage was found verbatim in its source."""
        return any(link.verbatim for link in self.evidence)

    @property
    def strength(self) -> float:
        """Combined weight of the evidence, capped at one.

        Summed rather than averaged, so a claim supported by four passages is
        stronger than the same claim supported by one -- but capped, so quantity
        cannot manufacture certainty on its own.
        """
        return round(min(1.0, sum(link.weight for link in self.evidence)), 3)

    def summary(self) -> str:
        return (
            f"[{self.status.value}/{self.confidence.value} "
            f"{len(self.evidence)}ev {self.strength}] {self.text[:60]}"
        )


class ClaimSet(BaseModel):
    """Every claim from one run, and the links between them.

    The claim-to-evidence relation is many to many in both directions: a claim
    can rest on several passages, and one passage can support several claims.
    Holding it as links rather than as nesting is what lets the question be
    asked either way -- "what supports this claim" for verification, and "what
    rests on this source" for the moment a source turns out to be wrong.
    """

    model_config = {"extra": "forbid"}

    claims: list[Claim] = Field(default_factory=list)
    rejected: list[tuple[str, str]] = Field(
        default_factory=list,
        description="Conclusions that could not become claims, as (text, reason).",
    )

    def by_id(self, claim_id: str) -> Claim | None:
        return next((claim for claim in self.claims if claim.id == claim_id), None)

    def resting_on(self, evidence_id: str) -> list[Claim]:
        """Every claim that depends on one piece of evidence.

        The direction that matters when something turns out to be wrong: a
        retracted page has to be traceable to everything built on top of it.
        """
        return [claim for claim in self.claims if evidence_id in claim.evidence_ids]

    def citing_source(self, source_id: str) -> list[Claim]:
        return [claim for claim in self.claims if source_id in claim.source_ids]

    def conflicting_pairs(self) -> list[tuple[Claim, Claim]]:
        """Contradicting claims, each pair reported once."""
        pairs: list[tuple[Claim, Claim]] = []
        for claim in self.claims:
            for other_id in claim.conflicts_with:
                other = self.by_id(other_id)
                if other is not None and claim.id < other.id:
                    pairs.append((claim, other))
        return pairs

    @property
    def publishable(self) -> list[Claim]:
        return [claim for claim in self.claims if claim.status.is_publishable]

    def of_kind(self, kind: ClaimKind) -> list[Claim]:
        return [claim for claim in self.claims if claim.kind is kind]

    def summary(self) -> str:
        merged = sum(claim.merged_from - 1 for claim in self.claims)
        parts = [
            f"{len(self.claims)} claims",
            f"{len(self.conflicting_pairs())} conflicting pairs",
        ]
        if merged:
            parts.append(f"{merged} duplicates merged")
        if self.rejected:
            parts.append(f"{len(self.rejected)} rejected")
        return ", ".join(parts)


def _claim_id(research_id: str | None, kind: ClaimKind, index: int) -> str:
    prefix = research_id or "run"
    return f"{prefix}:{kind.value[:4]}_{index}"


def _mergeable(left: Claim, right: Claim) -> bool:
    """Whether two claims say the same thing.

    Word overlap decides similarity, and negation vetoes it. "Order is preserved
    across partitions" and "order is not preserved across partitions" share
    every content word and score as near-identical, so overlap alone would merge
    a claim with its opposite and delete the disagreement.

    Positions never merge, however similar. They are the two halves of a
    contradiction the analyst deliberately kept apart, and they are precisely
    the pairs that word overlap scores highest.
    """
    if left.kind is not right.kind or left.kind is ClaimKind.POSITION:
        return False
    if negation_mismatch(left.text, right.text):
        return False
    return similarity(content_words(left.text), content_words(right.text)) >= (
        MERGE_SIMILARITY_THRESHOLD
    )


def _merge(into: Claim, other: Claim) -> Claim:
    """Fold one claim into another, keeping the union of their evidence.

    The more specific text wins, on the assumption that a longer statement of
    the same fact carries the qualifications the shorter one dropped. The higher
    confidence does not win: the merged claim keeps the lower, because two
    phrasings of one fact from overlapping evidence are not corroboration.
    """
    seen = {link.evidence_id for link in into.evidence}
    evidence = [*into.evidence, *(link for link in other.evidence if link.evidence_id not in seen)]
    text = into.text if len(into.text) >= len(other.text) else other.text

    return into.model_copy(
        update={
            "text": text,
            "evidence": evidence,
            "confidence": Confidence.at_most(other.confidence, into.confidence),
            "corroborating_publishers": max(
                into.corroborating_publishers, other.corroborating_publishers
            ),
            "merged_from": into.merged_from + other.merged_from,
            "conflicts_with": sorted({*into.conflicts_with, *other.conflicts_with}),
        }
    )


def build_claims(
    analysis: Analysis,
    evidence: list[Evidence],
    *,
    research_id: str | None = None,
) -> ClaimSet:
    """Turn an analysis into claims linked to the evidence they rest on.

    Deterministic and free: no model is involved. The analyst already decided
    what the evidence supports, and re-asking a model to restate its own
    conclusions as claims would add cost, latency, and a second opportunity to
    invent something.

    Findings and recommendations become claims. Each side of a contradiction
    becomes a claim too, marked conflicting and linked to its opposite -- so a
    disagreement travels as two positions a reader can weigh, rather than as a
    note attached to a single sentence.
    """
    by_id = {item.id: item for item in evidence}
    claims: list[Claim] = []
    rejected: list[tuple[str, str]] = []

    def links(evidence_ids: list[str]) -> list[EvidenceLink]:
        return [EvidenceLink.to(by_id[eid]) for eid in evidence_ids if eid in by_id]

    for index, finding in enumerate(analysis.findings, start=1):
        supporting = links(finding.evidence_ids)
        if not supporting:
            rejected.append((finding.statement, "no evidence resolved for this finding"))
            continue
        claims.append(
            Claim(
                id=_claim_id(research_id, ClaimKind.FINDING, index),
                text=finding.statement,
                kind=ClaimKind.FINDING,
                confidence=finding.confidence,
                evidence=supporting,
                corroborating_publishers=max(1, finding.corroborating_domains),
            )
        )

    for index, tradeoff in enumerate(analysis.tradeoffs, start=1):
        supporting = links(tradeoff.evidence_ids)
        if not supporting:
            rejected.append((tradeoff.subject, "no evidence resolved for this trade-off"))
            continue
        claims.append(
            Claim(
                id=_claim_id(research_id, ClaimKind.TRADEOFF, index),
                # Stated as one sentence because a trade-off is one assertion:
                # this benefit costs that. Split across two claims, each half
                # can be verified, published, and read without the other -- which
                # is how a report ends up recommending a benefit whose cost was
                # checked separately and left out.
                text=f"{tradeoff.benefit} At the cost of: {tradeoff.cost}",
                kind=ClaimKind.TRADEOFF,
                confidence=Confidence.MODERATE,
                evidence=supporting,
            )
        )

    for index, recommendation in enumerate(analysis.recommendations, start=1):
        supporting = links(recommendation.evidence_ids)
        if not supporting:
            rejected.append(
                (recommendation.recommendation, "no evidence resolved for this recommendation")
            )
            continue
        claims.append(
            Claim(
                id=_claim_id(research_id, ClaimKind.RECOMMENDATION, index),
                text=recommendation.recommendation,
                kind=ClaimKind.RECOMMENDATION,
                confidence=recommendation.confidence,
                evidence=supporting,
                condition=recommendation.condition,
            )
        )

    for index, contradiction in enumerate(analysis.contradictions, start=1):
        side_a = links(contradiction.evidence_ids_a)
        side_b = links(contradiction.evidence_ids_b)
        if not (side_a and side_b):
            rejected.append(
                (contradiction.subject, "a side of the contradiction resolved to no evidence")
            )
            continue

        id_a = _claim_id(research_id, ClaimKind.POSITION, index * 2 - 1)
        id_b = _claim_id(research_id, ClaimKind.POSITION, index * 2)
        for claim_id, other_id, text, supporting in (
            (id_a, id_b, contradiction.position_a, side_a),
            (id_b, id_a, contradiction.position_b, side_b),
        ):
            claims.append(
                Claim(
                    id=claim_id,
                    text=text,
                    kind=ClaimKind.POSITION,
                    # Known now, not pending a verifier. The analyst identified
                    # the disagreement and discarding that would be pretending
                    # not to know something the system already established.
                    status=ClaimStatus.CONFLICTING,
                    confidence=Confidence.LOW,
                    evidence=supporting,
                    conflicts_with=[other_id],
                )
            )

    return ClaimSet(claims=_collapse(claims), rejected=rejected)


def _collapse(claims: list[Claim]) -> list[Claim]:
    """Merge claims that repeat each other, in order.

    Order-stable: each claim is folded into the first one it matches, so the
    result does not depend on comparison order. Merging into the *last* match
    instead would make the output depend on how the analyst happened to sequence
    its findings.
    """
    collapsed: list[Claim] = []
    for claim in claims:
        for index, existing in enumerate(collapsed):
            if _mergeable(existing, claim):
                collapsed[index] = _merge(existing, claim)
                break
        else:
            collapsed.append(claim)
    return collapsed
