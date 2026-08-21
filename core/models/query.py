"""The structured research specification produced by the query analyzer.

This schema is the first place DeepTrace turns a vague human sentence into
something the rest of the system can execute against. Everything downstream --
the plan, the searches, the completion checks -- reads from here.

One property of this schema is load-bearing: **there is no field for an answer.**
The analyzer's rule is "do not answer the question", and rather than relying on
the prompt to enforce that, the output shape makes it impossible. A model that
tries to answer has nowhere to put the answer, and validation fails.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class ResearchType(StrEnum):
    """What kind of research the question calls for.

    This drives planning. A comparison needs symmetric coverage of each option;
    an investigation needs to follow evidence wherever it leads. Classifying up
    front means the planner is not re-deriving intent from raw text.
    """

    COMPARISON = "comparison"
    """Weighing two or more named options against each other."""

    EXPLANATION = "explanation"
    """Understanding how or why something works."""

    INVESTIGATION = "investigation"
    """Establishing facts about a situation, incident, or current state."""

    RECOMMENDATION = "recommendation"
    """Choosing a course of action for a stated context."""

    REVIEW = "review"
    """Surveying a field, technology, or body of work."""


class TimeSensitivity(StrEnum):
    """How much the correct answer depends on recency.

    Determines whether stale sources are acceptable evidence. A question about
    how TCP works tolerates a ten-year-old source; a question about a library's
    current recommended API does not.
    """

    STATIC = "static"
    """Stable knowledge. Source age is largely irrelevant."""

    EVOLVING = "evolving"
    """Changes over years. Prefer recent sources, tolerate older ones."""

    VOLATILE = "volatile"
    """Changes within months. Old sources are actively misleading."""


class Ambiguity(BaseModel):
    """Something underspecified in the question, with a stated way forward.

    Ambiguities are recorded rather than raised as blocking questions. A research
    system that refuses to start until every detail is pinned down is not useful;
    one that proceeds on hidden assumptions is not trustworthy. Carrying an
    explicit ``assumption`` lets research proceed while keeping the choice
    visible, so the final report can state what was assumed.
    """

    model_config = {"extra": "forbid"}

    aspect: str = Field(
        min_length=3,
        description="What is unclear, stated in a few words.",
    )
    why_it_matters: str = Field(
        min_length=10,
        description="How a different reading would change the research.",
    )
    assumption: str = Field(
        min_length=3,
        description="The reading research will proceed with unless corrected.",
    )


class QuerySpec(BaseModel):
    """A research question, normalised into an executable specification.

    Deliberately absent: any field that could hold an answer, a finding, or a
    conclusion. See the module docstring.
    """

    model_config = {"extra": "forbid"}

    normalized_question: str = Field(
        min_length=10,
        max_length=500,
        description="The question restated precisely, preserving the user's intent.",
    )
    research_type: ResearchType
    scope: list[str] = Field(
        min_length=1,
        max_length=15,
        description="Aspects that must be covered for the research to be complete.",
    )
    out_of_scope: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="Adjacent topics deliberately excluded, to stop scope creep.",
    )
    constraints: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="Limits the user stated or clearly implied.",
    )
    ambiguities: list[Ambiguity] = Field(
        default_factory=list,
        max_length=10,
        description="Underspecified aspects, each with the assumption being made.",
    )
    success_criteria: list[str] = Field(
        min_length=1,
        max_length=10,
        description="Conditions under which the research can stop.",
    )
    time_sensitivity: TimeSensitivity
    requires_current_information: bool = Field(
        description="Whether answering correctly needs information from the live web.",
    )

    @field_validator("normalized_question")
    @classmethod
    def _must_not_be_an_answer(cls, value: str) -> str:
        """Reject output that reads as a conclusion rather than a question.

        A weak check by design -- the real guarantee is that the schema has no
        answer field. This catches the specific failure where a model restates
        the question as a declarative finding, which would silently bias every
        downstream stage toward confirming it.
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError("normalized_question must not be empty")

        lowered = stripped.lower()
        verdict_markers = (
            "the answer is",
            "in conclusion",
            "you should use",
            "the best option is",
            "i recommend",
            "it is recommended that",
        )
        if any(marker in lowered for marker in verdict_markers):
            raise ValueError(
                "normalized_question reads as an answer. The analyzer must "
                "restate the question, not resolve it."
            )
        return stripped

    @field_validator("scope", "out_of_scope", "constraints", "success_criteria")
    @classmethod
    def _no_blank_entries(cls, value: list[str]) -> list[str]:
        """Drop empty strings and reject a list that was only padding.

        Models occasionally emit `[""]` to satisfy a minimum-length constraint.
        Accepting that would let an empty scope pass as a populated one.
        """
        cleaned = [item.strip() for item in value if item and item.strip()]
        if value and not cleaned:
            raise ValueError("list contained only blank entries")
        return cleaned

    @property
    def is_ambiguous(self) -> bool:
        return bool(self.ambiguities)

    @property
    def freshness_required(self) -> bool:
        """Whether stale sources should be treated as weak evidence."""
        return self.time_sensitivity is TimeSensitivity.VOLATILE or (
            self.requires_current_information
        )

    def summary(self) -> str:
        """One-line description, for logs and progress events."""
        return (
            f"{self.research_type.value} | {len(self.scope)} scope items | "
            f"{len(self.ambiguities)} ambiguities | {self.time_sensitivity.value}"
        )
