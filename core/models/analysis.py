"""What analysis produces, and the rules it cannot talk its way past.

The analyst is the first stage that says something the sources did not. Evidence
is quotation -- checked against the page it came from -- while a finding is a
statement *about* the evidence, and nothing in a sentence's grammar reveals
whether it was supported or invented.

So the same discipline the evidence layer uses applies here, one level up. The
evidence layer refuses a quotation that is not in its source; this layer refuses
a conclusion that does not cite evidence, and refuses citations that do not
resolve. Both are deterministic and neither asks a model to check a model.

Two rules are enforced here rather than requested in the prompt:

*A conclusion with no resolvable evidence is dropped.* Not softened, not marked
uncertain -- removed, along with its reasoning. A model asked to cite sources
will occasionally cite one that does not exist, and a plausible sentence with a
broken citation is exactly what this project exists to prevent.

*Confidence is capped by what the evidence can carry.* A model rates its own
confidence, and rates it generously. One source is not corroboration however
authoritative it sounds, and two pages on one domain are one publisher, not two
independent observations.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from core.models.evidence import Evidence


class Confidence(StrEnum):
    """How much weight a conclusion can bear.

    Ordered, so calibration can lower one without a lookup table.
    """

    HIGH = "high"
    MODERATE = "moderate"
    LOW = "low"

    @property
    def rank(self) -> int:
        return {Confidence.HIGH: 2, Confidence.MODERATE: 1, Confidence.LOW: 0}[self]

    @classmethod
    def at_most(cls, ceiling: Confidence, proposed: Confidence) -> Confidence:
        return proposed if proposed.rank <= ceiling.rank else ceiling

    def lowered(self) -> Confidence:
        return Confidence.LOW if self is Confidence.MODERATE else Confidence.MODERATE


class Cited(BaseModel):
    """Base for anything the analyst asserts.

    ``evidence_ids`` holds the short labels the prompt showed the model until
    grounding rewrites them into real evidence ids. Labels rather than raw ids
    because an id is a random string a model has to transcribe exactly, and a
    transcription slip is indistinguishable from a fabricated citation -- both
    fail to resolve, but only one of them is the model's fault.
    """

    model_config = {"extra": "forbid"}

    evidence_ids: list[str] = Field(
        default_factory=list,
        max_length=12,
        description="Labels of the evidence supporting this, e.g. E1, E4.",
    )


class Finding(Cited):
    """One substantive statement the evidence supports."""

    statement: str = Field(min_length=15, max_length=500)
    confidence: Confidence = Confidence.MODERATE

    corroborating_domains: int = Field(
        default=0,
        description=(
            "Distinct publishers backing this. Computed during grounding, not "
            "asserted by the model."
        ),
    )

    @field_validator("statement")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be blank")
        return cleaned

    @property
    def is_corroborated(self) -> bool:
        """Whether more than one publisher supports it."""
        return self.corroborating_domains > 1

    def summary(self) -> str:
        return (
            f"[{self.confidence.value}/{self.corroborating_domains} sources] {self.statement[:70]}"
        )


class TradeOff(Cited):
    """A benefit that costs something, stated with both halves.

    Modelled as a pair rather than prose because a trade-off written as a
    sentence tends to lose one side of itself by the time it reaches a report.
    """

    subject: str = Field(min_length=3, max_length=200)
    benefit: str = Field(min_length=10, max_length=400)
    cost: str = Field(min_length=10, max_length=400)


class Contradiction(BaseModel):
    """Two sources that disagree, kept as a disagreement.

    Both positions carry their own evidence, so the reader can see who says
    what. Averaging them into a hedged sentence destroys the most useful thing
    the research found: that the question is contested.
    """

    model_config = {"extra": "forbid"}

    subject: str = Field(min_length=3, max_length=200)
    position_a: str = Field(min_length=10, max_length=400)
    evidence_ids_a: list[str] = Field(default_factory=list, max_length=8)
    position_b: str = Field(min_length=10, max_length=400)
    evidence_ids_b: list[str] = Field(default_factory=list, max_length=8)

    def summary(self) -> str:
        return f"{self.subject}: {self.position_a[:50]} vs {self.position_b[:50]}"


class Recommendation(Cited):
    """A course of action, with the condition under which it holds.

    ``condition`` is required because an unconditional recommendation drawn from
    a handful of sources is an opinion wearing research as a costume. "Use X"
    is a claim about every situation; "use X when throughput matters more than
    ordering" is a claim about the evidence.
    """

    recommendation: str = Field(min_length=10, max_length=400)
    condition: str = Field(min_length=5, max_length=300)
    confidence: Confidence = Confidence.MODERATE


class OpenQuestion(BaseModel):
    """Something the research did not answer.

    Carries no evidence by construction -- it is a statement about absence. It
    exists so a gap is reported rather than filled in, which is what a model
    does with a gap when nothing gives it somewhere else to put one.
    """

    model_config = {"extra": "forbid"}

    question: str = Field(min_length=10, max_length=300)
    why_unanswered: str = Field(min_length=10, max_length=300)


class Analysis(BaseModel):
    """Everything the analyst concluded from the evidence."""

    model_config = {"extra": "forbid"}

    summary: str = Field(
        min_length=20,
        max_length=1200,
        description="What the evidence shows overall, in a few sentences.",
    )
    findings: list[Finding] = Field(default_factory=list, max_length=15)
    tradeoffs: list[TradeOff] = Field(default_factory=list, max_length=10)
    contradictions: list[Contradiction] = Field(default_factory=list, max_length=10)
    recommendations: list[Recommendation] = Field(default_factory=list, max_length=8)
    open_questions: list[OpenQuestion] = Field(default_factory=list, max_length=8)

    @property
    def corroborated_findings(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.is_corroborated]

    def summary_line(self) -> str:
        return (
            f"{len(self.findings)} findings ({len(self.corroborated_findings)} corroborated), "
            f"{len(self.tradeoffs)} trade-offs, {len(self.contradictions)} contradictions, "
            f"{len(self.open_questions)} open questions"
        )


class AnalysisReport(BaseModel):
    """The grounded analysis, plus what grounding removed.

    Discards are reported for the same reason rejected quotations are: a
    conclusion that cited nothing real is a fact about the run. Silently
    dropping it would leave an analysis that looks smaller than it was, with no
    way to tell a thorough run from one whose citations did not resolve.
    """

    model_config = {"extra": "forbid"}

    analysis: Analysis
    dropped: list[tuple[str, str]] = Field(
        default_factory=list,
        description="Discarded conclusions, as (statement, reason).",
    )
    evidence_considered: int = 0

    @property
    def drop_rate(self) -> float:
        kept = (
            len(self.analysis.findings)
            + len(self.analysis.tradeoffs)
            + len(self.analysis.recommendations)
        )
        total = kept + len(self.dropped)
        return round(len(self.dropped) / total, 3) if total else 0.0

    def summary(self) -> str:
        discarded = f", {len(self.dropped)} discarded" if self.dropped else ""
        return f"{self.analysis.summary_line()}{discarded}"


def evidence_labels(evidence: list[Evidence]) -> dict[str, Evidence]:
    """Number the evidence for the prompt: E1, E2, ...

    Positional and stable within one analysis. The mapping is ours, so a label
    the model invents has nowhere to resolve to -- which is the property that
    makes a fabricated citation detectable rather than merely unlikely.
    """
    return {f"E{index}": item for index, item in enumerate(evidence, start=1)}


def _resolve(labels: list[str], table: dict[str, Evidence]) -> tuple[list[str], list[Evidence]]:
    """Map labels to real evidence ids, dropping any that do not resolve."""
    resolved: list[str] = []
    backing: list[Evidence] = []
    for label in labels:
        item = table.get(label.strip().upper())
        if item is not None and item.id not in resolved:
            resolved.append(item.id)
            backing.append(item)
    return resolved, backing


def publishers(backing: list[Evidence], domains: dict[str, str]) -> set[str]:
    """The distinct publishers behind a set of evidence.

    Counted by domain rather than by source, because two pages on one site are
    one publisher. Treating them as two independent observations is how a single
    vendor's documentation becomes "corroborated by multiple sources" -- the
    exact overstatement this layer exists to prevent.

    Falls back to the source id when a domain is unknown, which counts a source
    as its own publisher: the cautious direction is to under-merge rather than
    to invent independence that was not established.
    """
    return {domains.get(item.source_id, item.source_id) for item in backing}


def _calibrate(
    proposed: Confidence, backing: list[Evidence], domains: dict[str, str]
) -> Confidence:
    """Cap a model's stated confidence at what its evidence can carry.

    One publisher is not corroboration however authoritative it sounds, and a
    conclusion resting only on paraphrase rests on wording that was never
    checked. Both ceilings are arithmetic on the evidence rather than a
    judgement, so no prompt can argue past them.
    """
    confidence = proposed
    if len(publishers(backing, domains)) < 2:
        confidence = Confidence.at_most(Confidence.MODERATE, confidence)
    if backing and not any(item.is_verified for item in backing):
        confidence = confidence.lowered()
    return confidence


def ground(
    analysis: Analysis,
    evidence: list[Evidence],
    *,
    domains: dict[str, str] | None = None,
) -> AnalysisReport:
    """Keep only the conclusions whose citations resolve, and calibrate them.

    Everything the analyst asserts must point at evidence that was actually
    collected. A conclusion citing a label with nothing behind it is removed
    rather than repaired: rewriting it would mean guessing which evidence the
    model meant, and a guess is how a fabricated citation becomes a real-looking
    one.

    Open questions survive untouched. They are statements about absence and have
    no evidence to cite by construction.

    Args:
        domains: Source id to publisher domain, used to decide what counts as
            corroboration. Supplied by the caller, which holds the sources; this
            layer holds only the evidence, and evidence knows its source's id
            but not who published it.
    """
    table = evidence_labels(evidence)
    domains = domains or {}
    dropped: list[tuple[str, str]] = []

    findings: list[Finding] = []
    for finding in analysis.findings:
        ids, backing = _resolve(finding.evidence_ids, table)
        if not ids:
            dropped.append((finding.statement, "no evidence citation resolved"))
            continue
        findings.append(
            finding.model_copy(
                update={
                    "evidence_ids": ids,
                    "corroborating_domains": len(publishers(backing, domains)),
                    "confidence": _calibrate(finding.confidence, backing, domains),
                }
            )
        )

    tradeoffs: list[TradeOff] = []
    for tradeoff in analysis.tradeoffs:
        ids, _ = _resolve(tradeoff.evidence_ids, table)
        if not ids:
            dropped.append((tradeoff.subject, "no evidence citation resolved"))
            continue
        tradeoffs.append(tradeoff.model_copy(update={"evidence_ids": ids}))

    contradictions: list[Contradiction] = []
    for contradiction in analysis.contradictions:
        side_a, _ = _resolve(contradiction.evidence_ids_a, table)
        side_b, _ = _resolve(contradiction.evidence_ids_b, table)
        if not (side_a and side_b):
            # A contradiction needs both sides evidenced. One side citing
            # nothing is not a disagreement between sources; it is a
            # disagreement between a source and the model.
            dropped.append((contradiction.subject, "a side of the contradiction cited nothing"))
            continue
        contradictions.append(
            contradiction.model_copy(update={"evidence_ids_a": side_a, "evidence_ids_b": side_b})
        )

    recommendations: list[Recommendation] = []
    for recommendation in analysis.recommendations:
        ids, backing = _resolve(recommendation.evidence_ids, table)
        if not ids:
            dropped.append((recommendation.recommendation, "no evidence citation resolved"))
            continue
        recommendations.append(
            recommendation.model_copy(
                update={
                    "evidence_ids": ids,
                    "confidence": _calibrate(recommendation.confidence, backing, domains),
                }
            )
        )

    return AnalysisReport(
        analysis=analysis.model_copy(
            update={
                "findings": findings,
                "tradeoffs": tradeoffs,
                "contradictions": contradictions,
                "recommendations": recommendations,
            }
        ),
        dropped=dropped,
        evidence_considered=len(evidence),
    )
