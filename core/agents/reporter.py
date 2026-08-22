"""The reporter: the last stage, and the one with the most to lose.

Everything upstream is a guarantee about provenance. This is where those
guarantees either reach the reader or quietly stop being true, because a report
is prose and prose can overstate a checked claim without any individual word
being false.

The design is therefore about what the model is *not* given. It receives claims
that survived verification, each with an explicit instruction about how far it
may be stated, and the citation numbers available to it. It does not receive
sources, page text, search results, or the analyst's reasoning. A generator that
cannot see a rejected page cannot cite one, and that is a stronger property than
a prompt asking it not to.

Three of the nine sections never reach the model at all. What was asked, how it
was researched, and what it cites are rendered from the run's own record --
because a method section a model wrote could describe searches that never
happened, and a bibliography a model wrote could list a page that was never
retrieved.
"""

from __future__ import annotations

from core.llm.client import LLMClient
from core.logging import get_logger
from core.models.claim import Claim, ClaimSet
from core.models.evidence import Evidence
from core.models.query import QuerySpec
from core.models.report import (
    DraftReport,
    Report,
    assemble,
    build_citations,
    claims_for_report,
    confidence_note,
)
from core.models.research import TaskResult
from core.models.source import Source
from core.models.verification import VerificationReport
from core.prompts.registry import Prompt
from core.prompts.reporter import REPORTER_V1

log = get_logger(__name__)

AGENT_NAME = "reporter"


def _render_claims(claims: list[Claim], citations_for: dict[str, list[int]]) -> str:
    """Lay out the claims with their citations and their stating instruction.

    The instruction is attached per claim rather than stated once as a rule,
    because "hedge where the evidence is weak" is exactly the kind of guidance a
    fluent model applies unevenly across a long document.
    """
    lines = []
    for claim in claims:
        numbers = ", ".join(f"[{number}]" for number in citations_for.get(claim.id, []))
        lines.append(
            f"{claim.id} ({claim.kind.value})\n"
            f"   {claim.text}\n"
            f"   how to state it: {confidence_note(claim)}\n"
            f"   citations: {numbers or '(none)'}"
        )
        if claim.condition:
            lines.append(f"   holds when: {claim.condition}")
    return "\n".join(lines) if lines else "(no claim survived verification)"


def _interpretation(question: str, spec: QuerySpec | None) -> str:
    """The question section: what was asked, and what was assumed.

    Assembled from the specification rather than written, because the
    assumptions the analyzer made are the ones the research actually followed. A
    model asked to describe them would describe plausible ones.
    """
    if spec is None:
        return question

    lines = [spec.normalized_question]
    if spec.scope:
        lines.append("")
        lines.append("Covered: " + "; ".join(spec.scope))
    if spec.out_of_scope:
        lines.append("Not covered: " + "; ".join(spec.out_of_scope))
    if spec.ambiguities:
        lines.append("")
        lines.append("The question was ambiguous in places, and this research assumed:")
        lines.extend(f"- {item.aspect}: {item.assumption}" for item in spec.ambiguities)
    return "\n".join(lines)


def _method(
    task_results: list[TaskResult],
    sources: list[Source],
    evidence: list[Evidence],
    verification: VerificationReport | None,
    research_loops: int,
) -> str:
    """The method section, counted from the record rather than described.

    Every number here is something the run did, not something a model recalled
    about it. This is the section a sceptical reader checks first, so nothing in
    it may be generated.
    """
    verbatim = sum(1 for item in evidence if item.is_verified)
    lines = [
        f"The question was decomposed into {len(task_results)} research "
        f"{'task' if len(task_results) == 1 else 'tasks'}, which retrieved "
        f"{len(sources)} sources.",
        "",
        f"From those, {len(evidence)} passages were extracted and each was checked "
        f"against the page it came from; {verbatim} matched word for word. A passage "
        f"that could not be found in its source was discarded along with the "
        f"statement it was offered to support.",
    ]

    if verification is not None and verification.verdicts:
        lines.extend(
            [
                "",
                f"Each resulting claim was then checked against the evidence, including "
                f"evidence it did not cite: {verification.summary()}.",
            ]
        )

    if research_loops:
        lines.extend(
            [
                "",
                f"Verification could not settle every claim on the first pass, so "
                f"{research_loops} further round"
                f"{'s' if research_loops > 1 else ''} of research "
                f"{'were' if research_loops > 1 else 'was'} carried out.",
            ]
        )

    thin = [result.task_id for result in task_results if not result.usable_sources]
    if thin:
        lines.extend(["", f"These aspects returned nothing usable: {', '.join(thin)}."])

    return "\n".join(lines)


def _gaps(
    claims: ClaimSet, verification: VerificationReport | None, open_questions: list[str]
) -> str:
    """What the report must admit, gathered before the model writes.

    Handed over explicitly rather than left for the model to notice, because a
    model writing a limitations section from a set of successful claims writes
    a reassuring one. The gaps are known: the analyst named them, verification
    named more, and claims were rejected outright.
    """
    lines = list(open_questions)
    if verification is not None:
        lines.extend(verification.follow_up_questions)
        lines.extend(f"could not be checked: {claim_id}" for claim_id, _ in verification.failed)
    lines.extend(f"stated without support and dropped: {text}" for text, _ in claims.rejected)
    unsupported = [claim.text for claim in claims.claims if claim.status.value == "unsupported"]
    lines.extend(f"the evidence did not support: {text}" for text in unsupported)

    return "\n".join(f"- {line}" for line in lines) if lines else "(none identified)"


class Reporter:
    """Writes the report from verified claims, and assembles the rest."""

    def __init__(self, client: LLMClient, *, prompt: Prompt = REPORTER_V1) -> None:
        self.client = client
        self.prompt = prompt

    async def write(
        self,
        claims: ClaimSet,
        *,
        question: str,
        evidence: list[Evidence],
        sources: list[Source],
        spec: QuerySpec | None = None,
        task_results: list[TaskResult] | None = None,
        verification: VerificationReport | None = None,
        open_questions: list[str] | None = None,
        research_loops: int = 0,
        research_id: str | None = None,
    ) -> Report:
        """Produce the report for one run.

        A run with nothing publishable still produces a report. It says the
        research established nothing and shows what was attempted, which is a
        real answer -- and more useful than an empty file, because the reader
        learns that the question was researched and came back empty rather than
        that something broke.
        """
        publishable = claims_for_report(claims)
        citations = build_citations(publishable, evidence, sources)
        interpretation = _interpretation(question, spec)
        method = _method(task_results or [], sources, evidence, verification, research_loops)

        if not publishable:
            log.warning(
                "report.no_publishable_claims",
                research_id=research_id,
                claims=len(claims.claims),
            )
            return assemble(
                DraftReport(
                    title=f"No verified answer: {question[:120]}",
                    sections=[],
                ),
                question=question,
                claims=claims,
                citations=citations,
                method=method,
                interpretation=interpretation,
            )

        citations_for: dict[str, list[int]] = {}
        for citation in citations:
            for claim_id in citation.claim_ids:
                citations_for.setdefault(claim_id, []).append(citation.number)

        draft = await self.client.complete_structured(
            self.prompt,
            DraftReport,
            {
                "question": question,
                "interpretation": interpretation,
                "claims": _render_claims(publishable, citations_for),
                "gaps": _gaps(claims, verification, open_questions or []),
            },
            agent=AGENT_NAME,
            research_id=research_id,
        )

        report = assemble(
            draft,
            question=question,
            claims=claims,
            citations=citations,
            method=method,
            interpretation=interpretation,
        )

        log.info(
            "report.written",
            research_id=research_id,
            sections=len(report.sections),
            citations=len(report.citations),
            claims_used=len({cid for section in report.sections for cid in section.claim_ids}),
            claims_available=len(publishable),
            unresolved_citations=len(report.unresolved_markers),
            unsupported_claims=len(report.unsupported_claim_ids),
            prompt_version=self.prompt.version,
        )
        return report
