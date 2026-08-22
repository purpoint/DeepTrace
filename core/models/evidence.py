"""Evidence: specific passages that support specific claims.

A source is a document that was retrieved. Evidence is a passage from it that
supports a particular statement. Keeping them separate is what makes a citation
mean something: "this page is about Kafka" is a source, while "this sentence on
this page says Kafka preserves order within a partition" is evidence.

The load-bearing mechanism in this module is **quotation verification**. A model
asked to extract a supporting passage will sometimes paraphrase it, tidy it, or
produce a sentence that reads exactly like something the page would say but does
not appear on it. All three are indistinguishable from a real quote by eye, and
all three produce a citation that does not survive being clicked.

So every extracted passage is checked against the retrieved text before it is
allowed to become evidence:

    verbatim        the passage appears in the source
    normalised      appears after whitespace and unicode normalisation
    paraphrased     the source says something close, but not in these words
    not found       the source does not contain this -- rejected

The check is deterministic string matching, not another model call. Asking a
model whether a model's quote is real reintroduces the problem it is meant to
solve.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

_WHITESPACE = re.compile(r"\s+")
_WORD = re.compile(r"\w+", re.UNICODE)

# Typographic variants that differ only in rendering. A model reproducing a
# passage will often normalise these silently, and rejecting the quote over a
# curly apostrophe would discard evidence that is genuinely present.
_LOOKALIKES = {
    # Written as escapes rather than literal characters: a table of lookalikes
    # is unreadable when the entries look alike.
    "\u2018": "'",  # left single quote
    "\u2019": "'",  # right single quote / apostrophe
    "\u201a": "'",  # single low-9 quote
    "\u201b": "'",  # single high-reversed-9 quote
    "\u201c": '"',  # left double quote
    "\u201d": '"',  # right double quote
    "\u201e": '"',  # double low-9 quote
    "\u00ab": '"',  # left guillemet
    "\u00bb": '"',  # right guillemet
    "\u2010": "-",  # hyphen
    "\u2011": "-",  # non-breaking hyphen
    "\u2012": "-",  # figure dash
    "\u2013": "-",  # en dash
    "\u2014": "-",  # em dash
    "\u2015": "-",  # horizontal bar
    "\u2212": "-",  # minus sign
    "\u00a0": " ",  # non-breaking space
    "\u200b": "",  # zero-width space
    "\ufeff": "",  # byte order mark
    "\u2026": "...",  # ellipsis
}

PARAPHRASE_THRESHOLD = 0.75
"""Token overlap above which a passage counts as a paraphrase rather than absent.

Below it, the source does not support the passage in any recognisable form and
the evidence is rejected. Above it, the source says something close enough that
the extraction is probably honest but reworded -- which is still a defect,
because a citation should quote, so it is recorded as ``paraphrased`` and
weakened rather than silently accepted as verbatim.
"""


class QuoteStatus(StrEnum):
    """How well an extracted passage matches its source."""

    VERBATIM = "verbatim"
    NORMALISED = "normalised"
    PARAPHRASED = "paraphrased"
    NOT_FOUND = "not_found"

    @property
    def is_usable(self) -> bool:
        """Whether evidence with this status may enter the pool.

        Paraphrases are kept but marked. Passages absent from the source are
        not, because that is the failure the whole check exists to catch.
        """
        return self is not QuoteStatus.NOT_FOUND

    @property
    def is_quotable(self) -> bool:
        """Whether the text may be presented to a reader as a quotation."""
        return self in (QuoteStatus.VERBATIM, QuoteStatus.NORMALISED)


class SupportStrength(StrEnum):
    """How strongly a passage supports the claim attached to it."""

    STRONG = "strong"
    """States the claim directly."""

    MODERATE = "moderate"
    """Supports it, but requires a short inferential step."""

    WEAK = "weak"
    """Consistent with it, but does not establish it."""


def normalise(text: str) -> str:
    """Collapse the differences that do not change meaning.

    Unicode compatibility normalisation, typographic lookalikes folded to ASCII,
    whitespace collapsed. Case is preserved, because a quotation that changes
    case is not verbatim.
    """
    text = unicodedata.normalize("NFKC", text)
    for original, replacement in _LOOKALIKES.items():
        text = text.replace(original, replacement)
    return _WHITESPACE.sub(" ", text).strip()


def _tokens(text: str) -> list[str]:
    return _WORD.findall(normalise(text).lower())


def _best_window_overlap(quote_tokens: list[str], source_tokens: list[str]) -> float:
    """Highest token overlap between the quote and any same-length window.

    A sliding window rather than whole-document overlap: a long page contains
    most short quotes' words *somewhere*, which would make every passage look
    supported. Requiring them to appear together is the point.
    """
    if not quote_tokens or len(source_tokens) < len(quote_tokens):
        return 0.0

    needed = set(quote_tokens)
    width = len(quote_tokens)
    best = 0.0

    # Step by a fraction of the window so a passage straddling a boundary is
    # still found, without paying for a comparison at every single offset.
    step = max(1, width // 4)
    for start in range(0, len(source_tokens) - width + 1, step):
        window = set(source_tokens[start : start + width])
        overlap = len(needed & window) / len(needed)
        if overlap > best:
            best = overlap
            if best == 1.0:
                break
    return best


class QuoteVerification(BaseModel):
    """The result of checking a passage against its source."""

    model_config = {"extra": "forbid"}

    status: QuoteStatus
    similarity: float = Field(ge=0.0, le=1.0)
    detail: str = ""

    @property
    def is_usable(self) -> bool:
        return self.status.is_usable


def verify_quotation(quote: str, source_text: str) -> QuoteVerification:
    """Check whether a passage actually appears in the source it cites.

    This is the anti-fabrication mechanism. It is deliberately deterministic:
    asking a model whether a model's quote is genuine reintroduces exactly the
    failure it exists to catch.
    """
    if not quote.strip():
        return QuoteVerification(
            status=QuoteStatus.NOT_FOUND, similarity=0.0, detail="empty passage"
        )
    if not source_text.strip():
        return QuoteVerification(
            status=QuoteStatus.NOT_FOUND, similarity=0.0, detail="source has no text"
        )

    if quote in source_text:
        return QuoteVerification(status=QuoteStatus.VERBATIM, similarity=1.0)

    clean_quote = normalise(quote)
    clean_source = normalise(source_text)
    if clean_quote and clean_quote in clean_source:
        return QuoteVerification(
            status=QuoteStatus.NORMALISED,
            similarity=1.0,
            detail="matched after whitespace and typographic normalisation",
        )

    overlap = _best_window_overlap(_tokens(quote), _tokens(source_text))
    if overlap >= PARAPHRASE_THRESHOLD:
        return QuoteVerification(
            status=QuoteStatus.PARAPHRASED,
            similarity=round(overlap, 3),
            detail="source expresses this, but not in these words",
        )

    return QuoteVerification(
        status=QuoteStatus.NOT_FOUND,
        similarity=round(overlap, 3),
        detail="passage does not appear in the source",
    )


class Evidence(BaseModel):
    """A passage supporting a statement, with provenance.

    Provenance is not optional decoration. ``source_id`` plus ``supporting_text``
    is what lets a reader move from a sentence in the report back to the page it
    came from, which is the promise the whole system makes.
    """

    model_config = {"extra": "forbid"}

    id: str = Field(min_length=3)
    source_id: str = Field(min_length=3, description="The source this passage came from.")
    task_id: str | None = None

    claim: str = Field(
        min_length=10,
        max_length=400,
        description="What this passage supports, stated as a single assertion.",
    )
    supporting_text: str = Field(
        min_length=10,
        max_length=2000,
        description="The passage itself, quoted from the source.",
    )
    location: str = Field(
        default="",
        max_length=200,
        description="Where in the document it appears, for a reader to find it.",
    )
    support_strength: SupportStrength = SupportStrength.MODERATE

    verification: QuoteVerification | None = Field(
        default=None,
        description="Result of checking the passage against the source text.",
    )
    source_quality: float = Field(default=0.5, ge=0.0, le=1.0)
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("claim", "supporting_text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be blank")
        return cleaned

    @property
    def is_verified(self) -> bool:
        """Whether the passage was found in its source."""
        return self.verification is not None and self.verification.status.is_quotable

    @property
    def is_usable(self) -> bool:
        return self.verification is None or self.verification.is_usable

    @property
    def weight(self) -> float:
        """How much this evidence should count, combining several signals.

        Support strength and source quality both matter, and so does whether the
        passage was actually quoted: a paraphrase is weaker evidence than a
        verbatim quotation even when the source genuinely says the thing, because
        the wording that was checked is not the wording being relied on.
        """
        strength = {
            SupportStrength.STRONG: 1.0,
            SupportStrength.MODERATE: 0.7,
            SupportStrength.WEAK: 0.4,
        }[self.support_strength]

        fidelity = 1.0
        if self.verification is not None:
            fidelity = {
                QuoteStatus.VERBATIM: 1.0,
                QuoteStatus.NORMALISED: 1.0,
                QuoteStatus.PARAPHRASED: 0.6,
                QuoteStatus.NOT_FOUND: 0.0,
            }[self.verification.status]

        return round(strength * fidelity * self.source_quality, 3)

    def summary(self) -> str:
        status = self.verification.status.value if self.verification else "unchecked"
        return f"[{self.support_strength.value}/{status} w={self.weight}] {self.claim[:60]}"


class EvidenceExtractionReport(BaseModel):
    """What extraction produced, including what it rejected.

    Rejections are reported rather than dropped. If a source yields three
    fabricated passages, that is a fact about the run worth surfacing, and a
    silent filter would hide it.

    It lives in the models layer rather than beside the agent that fills it
    because it is a contract, not an implementation detail: the workflow state,
    the run object, and the report reader all hold one, and none of them should
    have to import an agent to name the type they are holding.
    """

    model_config = {"extra": "forbid"}

    evidence: list[Evidence] = Field(default_factory=list)
    rejected: list[tuple[str, str]] = Field(
        default_factory=list,
        description="Discarded passages, as (claim, reason).",
    )
    sources_processed: int = 0
    sources_failed: int = 0

    duplicates_collapsed: int = 0
    """Sources dropped because another source was the same page.

    Counted rather than ignored: it is the measure of how much of the search
    budget two tasks spent finding the same document."""

    over_budget: int = 0
    """Sources collected but not extracted from, because the run's source budget
    was already spent. A gap in the evidence with a cause attached."""
    injection_attempts: list[str] = Field(
        default_factory=list, description="Domains whose content addressed the model."
    )

    @property
    def verified_evidence(self) -> list[Evidence]:
        """Evidence whose passage was found verbatim in its source."""
        return [item for item in self.evidence if item.is_verified]

    @property
    def rejection_rate(self) -> float:
        total = len(self.evidence) + len(self.rejected)
        return round(len(self.rejected) / total, 3) if total else 0.0

    def summary(self) -> str:
        skipped = ""
        if self.duplicates_collapsed or self.over_budget:
            skipped = (
                f", {self.duplicates_collapsed} duplicate / "
                f"{self.over_budget} over budget not extracted"
            )
        return (
            f"{len(self.evidence)} evidence ({len(self.verified_evidence)} verbatim) "
            f"from {self.sources_processed} sources, {len(self.rejected)} rejected{skipped}"
        )
