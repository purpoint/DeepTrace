"""The report: the only thing most readers will ever see.

Every guarantee the pipeline built is worth exactly as much as this document's
fidelity to it. A verified claim that gets restated slightly too strongly, or a
citation number that points at the wrong passage, undoes the quote verifier, the
grounding pass, and the fact checker in one sentence.

So the report is assembled, not written. Three of its nine sections -- what was
asked, how it was researched, and what it cites -- contain no model output at
all: they are rendered from the run's own record, because a method section a
model wrote could describe searches that never happened, and a citation list a
model wrote could contain a URL that does not exist.

The six prose sections are written by a model, from **claims only**. It is not
given sources, search results, or the analyst's reasoning. It cannot cite a page
the fact checker rejected because it is never shown one, which is a stronger
guarantee than instructing it not to.

Citation numbers are ours. The model writes ``[3]``; the mapping from 3 to a
passage in a page belongs to this module, so a number it invents resolves to
nothing and is removed before anyone reads it -- the same discipline the analyst
and the fact checker use for evidence labels, applied to the last stage.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from core.models.claim import Claim, ClaimKind, ClaimSet, ClaimStatus
from core.models.evidence import Evidence
from core.models.source import Source

CITATION_MARKER = re.compile(r"\[(\d{1,3})\]")
"""How a citation appears in prose: a bracketed number, and nothing else.

Numbers rather than author-date because the reader's next action is to click,
and a number maps to exactly one passage in exactly one page. A citation style
that identifies a document identifies the wrong thing: this system's unit of
support is the passage, not the paper.
"""


class SectionKind(StrEnum):
    """The nine sections, in the order they are read.

    Three are assembled from the run's record and six are written from claims.
    The split is the point: a reader who trusts nothing the model wrote can
    still check the question it answered, the work it did, and every source it
    used, because no model touched those.
    """

    QUESTION = "question"
    SUMMARY = "summary"
    FINDINGS = "findings"
    TRADEOFFS = "tradeoffs"
    DISAGREEMENTS = "disagreements"
    RECOMMENDATIONS = "recommendations"
    LIMITATIONS = "limitations"
    METHOD = "method"
    SOURCES = "sources"

    @property
    def is_assembled(self) -> bool:
        """Whether this section is rendered from the record rather than written."""
        return self in (SectionKind.QUESTION, SectionKind.METHOD, SectionKind.SOURCES)

    @property
    def heading(self) -> str:
        return {
            SectionKind.QUESTION: "Question",
            SectionKind.SUMMARY: "Summary",
            SectionKind.FINDINGS: "Findings",
            SectionKind.TRADEOFFS: "Trade-offs",
            SectionKind.DISAGREEMENTS: "Where sources disagree",
            SectionKind.RECOMMENDATIONS: "Recommendations",
            SectionKind.LIMITATIONS: "Limitations",
            SectionKind.METHOD: "How this was researched",
            SectionKind.SOURCES: "Sources",
        }[self]


class Citation(BaseModel):
    """One numbered reference: a passage, in a page, supporting a claim.

    Carries the quotation itself, not just the URL. A citation a reader has to
    open a page to evaluate is a citation most readers will not evaluate, and
    the passage is the thing that was actually verified.
    """

    model_config = {"extra": "forbid"}

    number: int = Field(ge=1)
    evidence_id: str
    source_id: str
    url: str
    title: str = ""
    domain: str = ""
    location: str = ""
    quote: str
    quote_status: str = "verbatim"
    """How the passage matched its source. Shown because a paraphrase and a
    quotation are different kinds of support, and flattening them would make the
    weaker one look like the stronger."""

    claim_ids: list[str] = Field(default_factory=list)

    @property
    def is_quoted(self) -> bool:
        return self.quote_status in ("verbatim", "normalised")


class DraftSection(BaseModel):
    """One prose section as the model wrote it, before validation."""

    model_config = {"extra": "forbid"}

    kind: SectionKind
    body: str = Field(
        max_length=6000,
        description=(
            "The section's prose. Cite with bracketed numbers taken from the "
            "claims you were given -- 'Order is preserved per partition [3].' "
            "Every substantive statement carries one, and a number you were not "
            "given is deleted before anyone reads the report."
        ),
    )
    claim_ids: list[str] = Field(
        default_factory=list,
        max_length=30,
        description="Ids of the claims this section states.",
    )


class DraftReport(BaseModel):
    """What the model returns: the six written sections.

    The assembled sections are absent by construction rather than by filtering.
    A field the model could fill is a field it will eventually fill.
    """

    model_config = {"extra": "forbid"}

    title: str = Field(min_length=5, max_length=200)
    sections: list[DraftSection] = Field(default_factory=list, max_length=6)

    @field_validator("sections")
    @classmethod
    def _only_written_sections(cls, sections: list[DraftSection]) -> list[DraftSection]:
        for section in sections:
            if section.kind.is_assembled:
                raise ValueError(
                    f"{section.kind.value} is rendered from the run's record and cannot be written"
                )
        return sections


class ReportSection(BaseModel):
    """A finished section, with its citations resolved."""

    model_config = {"extra": "forbid"}

    kind: SectionKind
    body: str
    claim_ids: list[str] = Field(default_factory=list)
    citation_numbers: list[int] = Field(default_factory=list)

    @property
    def heading(self) -> str:
        return self.kind.heading


class Report(BaseModel):
    """A finished report, and what assembling it had to remove."""

    model_config = {"extra": "forbid"}

    title: str
    question: str
    sections: list[ReportSection] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)

    unresolved_markers: list[str] = Field(
        default_factory=list,
        description="Citation numbers the model used that point at nothing.",
    )
    unsupported_claim_ids: list[str] = Field(
        default_factory=list,
        description="Claim ids a section referenced that are not publishable.",
    )

    def section(self, kind: SectionKind) -> ReportSection | None:
        return next((section for section in self.sections if section.kind is kind), None)

    def citation(self, number: int) -> Citation | None:
        return next((item for item in self.citations if item.number == number), None)

    @property
    def is_fully_cited(self) -> bool:
        """Whether every citation in the prose resolves to a real passage."""
        return not self.unresolved_markers

    def summary(self) -> str:
        problems = []
        if self.unresolved_markers:
            problems.append(f"{len(self.unresolved_markers)} unresolved citations removed")
        if self.unsupported_claim_ids:
            problems.append(f"{len(self.unsupported_claim_ids)} unpublishable claims dropped")
        tail = f", {', '.join(problems)}" if problems else ""
        return f"{len(self.sections)} sections, {len(self.citations)} citations{tail}"


def build_citations(
    claims: list[Claim], evidence: list[Evidence], sources: list[Source]
) -> list[Citation]:
    """Number every passage the publishable claims rest on.

    Numbering is ours and assigned before the model writes, so the prose is
    written against a fixed table rather than the table being reverse-engineered
    from the prose. The second order is how a citation ends up pointing at
    whichever passage happened to land in that position.

    Ordered by claim, so a reader following the report top to bottom meets
    citations in roughly ascending order rather than scattered.
    """
    evidence_by_id = {item.id: item for item in evidence}
    source_by_id = {item.id: item for item in sources}

    citations: list[Citation] = []
    numbered: dict[str, Citation] = {}

    for claim in claims:
        for evidence_id in claim.evidence_ids:
            item = evidence_by_id.get(evidence_id)
            if item is None:
                continue

            existing = numbered.get(evidence_id)
            if existing is not None:
                # One passage, one number, however many claims lean on it.
                # Numbering it twice would make one source look like two.
                existing.claim_ids.append(claim.id)
                continue

            source = source_by_id.get(item.source_id)
            citation = Citation(
                number=len(citations) + 1,
                evidence_id=item.id,
                source_id=item.source_id,
                url=source.url if source else "",
                title=source.title if source else "",
                domain=source.domain if source else "",
                location=item.location,
                quote=item.supporting_text,
                quote_status=item.verification.status.value if item.verification else "unchecked",
                claim_ids=[claim.id],
            )
            citations.append(citation)
            numbered[evidence_id] = citation

    return citations


def _validate_prose(body: str, citations: dict[int, Citation]) -> tuple[str, list[str], list[int]]:
    """Strip citation markers that point at nothing.

    Removed rather than left in place, because a number in brackets reads as
    provenance whether or not it has any. A reader cannot tell a broken citation
    from a working one without clicking, and most will not click.

    Reported rather than silently dropped, for the same reason every other
    discard in this pipeline is reported.
    """
    unresolved: list[str] = []
    used: list[int] = []

    def replace(match: re.Match[str]) -> str:
        number = int(match.group(1))
        if number in citations:
            if number not in used:
                used.append(number)
            return match.group(0)
        unresolved.append(match.group(0))
        return ""

    cleaned = CITATION_MARKER.sub(replace, body)
    # Collapse the space a removed marker leaves behind, so the sentence reads
    # as though the citation was never written rather than visibly damaged.
    cleaned = re.sub(r" +([.,;:])", r"\1", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip(), unresolved, used


def assemble(
    draft: DraftReport,
    *,
    question: str,
    claims: ClaimSet,
    citations: list[Citation],
    method: str,
    interpretation: str,
) -> Report:
    """Turn a draft into a report whose every citation resolves.

    Two things are enforced here rather than requested in the prompt:

    *A citation that points at nothing is removed.* The model writes numbers; it
    does not decide which numbers exist.

    *A section cannot rest on a claim that is not publishable.* An unsupported
    claim is filtered out of what the model is shown, so a reference to one means
    the id was invented -- and an invented claim id in a report is a sentence
    with no support at all behind it.
    """
    table = {citation.number: citation for citation in citations}
    publishable = {claim.id for claim in claims.publishable}

    sections: list[ReportSection] = []
    unresolved: list[str] = []
    unsupported: list[str] = []

    sections.append(ReportSection(kind=SectionKind.QUESTION, body=interpretation, claim_ids=[]))

    for draft_section in sorted(draft.sections, key=lambda item: _ORDER[item.kind]):
        body, broken, used = _validate_prose(draft_section.body, table)
        unresolved.extend(broken)

        kept = [claim_id for claim_id in draft_section.claim_ids if claim_id in publishable]
        unsupported.extend(
            claim_id for claim_id in draft_section.claim_ids if claim_id not in publishable
        )
        if not body:
            continue

        sections.append(
            ReportSection(
                kind=draft_section.kind,
                body=body,
                claim_ids=kept,
                citation_numbers=used,
            )
        )

    sections.append(ReportSection(kind=SectionKind.METHOD, body=method, claim_ids=[]))
    sections.append(
        ReportSection(
            kind=SectionKind.SOURCES,
            body="",
            citation_numbers=[citation.number for citation in citations],
        )
    )

    return Report(
        title=draft.title,
        question=question,
        sections=sections,
        citations=citations,
        unresolved_markers=unresolved,
        unsupported_claim_ids=unsupported,
    )


_ORDER = {kind: index for index, kind in enumerate(SectionKind)}


def render_markdown(report: Report) -> str:
    """The report as a document.

    The sources section is rendered here rather than written by the model, so
    every entry is a row of the citation table: number, quotation, page. A
    bibliography a model produced could list a page that was never retrieved.
    """
    lines = [f"# {report.title}", ""]

    for section in report.sections:
        if section.kind is SectionKind.SOURCES:
            continue
        if not section.body:
            continue
        lines.extend([f"## {section.heading}", "", section.body, ""])

    if report.citations:
        lines.extend([f"## {SectionKind.SOURCES.heading}", ""])
        for citation in report.citations:
            marker = "" if citation.is_quoted else " (paraphrased)"
            where = f", {citation.location}" if citation.location else ""
            lines.append(
                f"{citation.number}. [{citation.title or citation.domain}]({citation.url}){where}"
            )
            lines.append(f'   > "{citation.quote}"{marker}')
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def claims_for_report(claims: ClaimSet) -> list[Claim]:
    """The claims a report may be written from, in reading order.

    Unsupported claims are absent -- not marked, absent. The report generator is
    never shown one, which is why it cannot state one however it is prompted.

    Ordered by kind and then strength, so the model meets findings before
    trade-offs before positions, which is the order the sections need them in.
    """
    order = {
        ClaimKind.FINDING: 0,
        ClaimKind.TRADEOFF: 1,
        ClaimKind.POSITION: 2,
        ClaimKind.RECOMMENDATION: 3,
    }
    return sorted(
        claims.publishable,
        key=lambda claim: (order.get(claim.kind, 4), -claim.strength),
    )


def confidence_note(claim: Claim) -> str:
    """How a claim must be stated, given what stands behind it.

    Handed to the model per claim rather than left to its judgement of tone. A
    partially supported claim written as a plain assertion is the single easiest
    way to undo everything verification established, and "hedge when the
    evidence is weak" is exactly the kind of instruction a fluent model follows
    unevenly.
    """
    if claim.status is ClaimStatus.CONFLICTING:
        return "sources disagree -- present both positions, do not pick one"
    if claim.status is ClaimStatus.PARTIALLY_SUPPORTED:
        return "state with its limitation; do not assert it flatly"
    if claim.status is ClaimStatus.PROPOSED:
        return "could not be checked -- attribute it to the source, do not assert it"
    if claim.corroborating_publishers > 1:
        return "supported by more than one publisher; may be stated plainly"
    return "supported by a single publisher; state plainly but attribute"
