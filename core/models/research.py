"""Structured outputs for the researcher agent."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from core.models.source import Source


class SearchQueries(BaseModel):
    """Search queries generated for one research task."""

    model_config = {"extra": "forbid"}

    queries: list[str] = Field(min_length=1, max_length=6)
    reasoning: str = Field(
        default="",
        max_length=500,
        description="Why these angles were chosen. Recorded in the trace.",
    )

    @field_validator("queries")
    @classmethod
    def _clean(cls, value: list[str]) -> list[str]:
        """Drop blanks and near-identical queries.

        Case-insensitive exact matching only. Two genuinely different phrasings
        that share words are the point of generating several queries, so this
        removes only literal repeats.
        """
        seen: set[str] = set()
        cleaned: list[str] = []
        for query in value:
            text = " ".join(query.split())
            key = text.lower()
            if text and key not in seen:
                seen.add(key)
                cleaned.append(text)
        if not cleaned:
            raise ValueError("no usable queries were produced")
        return cleaned


class SufficiencyVerdict(StrEnum):
    """Whether gathered material answers the task."""

    SUFFICIENT = "sufficient"
    INSUFFICIENT = "insufficient"

    NOT_AVAILABLE = "not_available"
    """The information does not appear to exist on the open web.

    Distinct from insufficient, and the distinction is what stops a pointless
    loop: another round of searching for something that is not published spends
    budget and finds nothing. A report saying "this is not publicly documented"
    is a legitimate and useful finding.
    """


class SufficiencyCheck(BaseModel):
    """The researcher's judgement on whether to keep searching."""

    model_config = {"extra": "forbid"}

    verdict: SufficiencyVerdict
    reason: str = Field(min_length=10, max_length=800)
    missing_topics: list[str] = Field(
        default_factory=list,
        max_length=6,
        description="Specific enough that a search query could be written from each.",
    )
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    @property
    def should_continue(self) -> bool:
        """Whether another round of searching is worth its cost."""
        return self.verdict is SufficiencyVerdict.INSUFFICIENT and bool(self.missing_topics)


class TaskResult(BaseModel):
    """Everything one research task produced.

    Records how the task stopped, not only what it found. A task that ran out of
    budget and a task that satisfied its criteria produced different quality of
    material, and the report must be able to say which happened.
    """

    model_config = {"extra": "forbid"}

    task_id: str
    question: str
    sources: list[Source] = Field(default_factory=list)
    queries_used: list[str] = Field(default_factory=list)
    rounds: int = 0
    verdict: SufficiencyVerdict = SufficiencyVerdict.INSUFFICIENT
    stop_reason: str = ""
    missing_topics: list[str] = Field(default_factory=list)
    failed_urls: list[tuple[str, str]] = Field(
        default_factory=list,
        description="URLs that could not be retrieved, with the reason. A gap in "
        "the evidence with a recorded cause rather than an unexplained absence.",
    )

    @property
    def usable_sources(self) -> list[Source]:
        """Sources with enough retrieved text to extract evidence from."""
        return [source for source in self.sources if source.has_content]

    @property
    def primary_sources(self) -> list[Source]:
        return [source for source in self.usable_sources if source.is_primary]

    @property
    def succeeded(self) -> bool:
        return self.verdict is SufficiencyVerdict.SUFFICIENT

    @property
    def mean_quality(self) -> float:
        usable = self.usable_sources
        if not usable:
            return 0.0
        return round(sum(s.quality_score for s in usable) / len(usable), 3)

    def summary(self) -> str:
        return (
            f"{self.task_id}: {len(self.usable_sources)} usable sources "
            f"({len(self.primary_sources)} primary), {self.rounds} rounds, "
            f"{self.verdict.value} -- {self.stop_reason}"
        )
