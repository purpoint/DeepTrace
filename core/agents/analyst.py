"""The analyst: turning verified evidence into stated conclusions.

This is where the system stops quoting and starts saying. Everything before it
is traceable by construction -- a source is a page that was retrieved, a piece of
evidence is a passage checked against that page. A finding is neither. It is a
statement about the evidence, and a fabricated one reads exactly like a sound
one.

So the agent does the same thing the evidence agent does, one level up, and for
the same reason. The evidence agent verifies quotations against source text with
deterministic matching. This agent verifies citations against the evidence pool
with a deterministic lookup, and discards whatever does not resolve. Neither
asks a model to check a model's work.

What the agent contributes beyond the model call is entirely enforcement:

*Only verified evidence is shown.* A passage the verifier rejected never reaches
analysis, so a conclusion cannot rest on one -- the filter is upstream of the
prompt rather than a rule inside it.

*Evidence is labelled, not identified.* The model cites E1 and E7, and the
mapping from label to evidence is ours. A label it invents resolves to nothing,
which is what makes an invented citation detectable rather than merely unlikely.

*Confidence is calibrated after the fact.* A model rates its own confidence
generously; grounding caps it at what the cited evidence can carry.
"""

from __future__ import annotations

from core.llm.client import LLMClient
from core.logging import get_logger
from core.models.analysis import (
    Analysis,
    AnalysisReport,
    evidence_labels,
    ground,
)
from core.models.evidence import Evidence
from core.models.query import QuerySpec
from core.models.research import TaskResult
from core.models.source import Source
from core.prompts.analyst import ANALYST_V1
from core.prompts.registry import Prompt

log = get_logger(__name__)

AGENT_NAME = "analyst"

MAX_EVIDENCE = 60
"""Most passages shown to the analyst in one call.

A ceiling on prompt size rather than a research limit: the source budget already
bounds how much is collected, and this guards the case where one generous source
yields many passages. Ordered by weight before truncation, so what is dropped is
the weakest evidence rather than whatever happened to be last.
"""

MAX_PASSAGE_CHARS = 400
"""Passages are trimmed for the prompt, never for the record.

The full text stays in the evidence the report cites. What the analyst needs is
enough to see what a passage establishes, and sending sixty complete passages
would spend the token budget on repetition.
"""


def _render_evidence(labels: dict[str, Evidence], domains: dict[str, str]) -> str:
    """Lay out the evidence pool for the prompt.

    The publisher is shown because independence is something the analyst is
    asked to weigh, and two passages from one domain are one publisher. The
    verification status is shown for the same reason: a paraphrase is weaker
    ground than a quotation, and the model should be able to see which it has.
    """
    lines = []
    for label, item in labels.items():
        publisher = domains.get(item.source_id, "unknown source")
        status = item.verification.status.value if item.verification else "unchecked"
        passage = item.supporting_text[:MAX_PASSAGE_CHARS]
        lines.append(
            f"{label}. [{publisher} | {status} | weight {item.weight}]\n"
            f"   claim: {item.claim}\n"
            f'   passage: "{passage}"'
        )
    return "\n".join(lines)


def _bullets(items: list[str], *, empty: str = "(none stated)") -> str:
    return "\n".join(f"- {item}" for item in items) if items else empty


class AnalystAgent:
    """Draws conclusions from verified evidence, and only from it."""

    def __init__(
        self,
        client: LLMClient,
        *,
        prompt: Prompt = ANALYST_V1,
        max_evidence: int = MAX_EVIDENCE,
    ) -> None:
        self.client = client
        self.prompt = prompt
        self.max_evidence = max_evidence

    async def analyse(
        self,
        evidence: list[Evidence],
        *,
        question: str,
        spec: QuerySpec | None = None,
        sources: list[Source] | None = None,
        task_results: list[TaskResult] | None = None,
        research_id: str | None = None,
    ) -> AnalysisReport:
        """Analyse the evidence a run collected.

        Args:
            sources: Used to attribute evidence to publishers, which is what
                decides whether two passages corroborate each other or merely
                repeat one site.
            task_results: Used to tell the analyst which aspects of the question
                produced nothing, so a gap is named rather than filled.

        Returns an empty analysis rather than raising when there is nothing to
        analyse. A run whose evidence was all rejected has a real answer -- that
        it established nothing -- and an exception would replace that answer
        with a stack trace.
        """
        usable = self._usable(evidence)
        if not usable:
            log.warning(
                "analysis.no_evidence",
                research_id=research_id,
                evidence_supplied=len(evidence),
            )
            return AnalysisReport(
                analysis=Analysis(
                    summary=(
                        "No verified evidence was available, so nothing can be "
                        "concluded about this question."
                    )
                ),
                evidence_considered=0,
            )

        domains = {source.id: source.domain for source in (sources or []) if source.domain}
        labels = evidence_labels(usable)

        analysis = await self.client.complete_structured(
            self.prompt,
            Analysis,
            {
                "question": question,
                "research_type": spec.research_type.value if spec else "unspecified",
                "scope": _bullets(spec.scope if spec else []),
                "gaps": _bullets(self._gaps(task_results), empty="(none)"),
                "evidence": _render_evidence(labels, domains),
                "evidence_count": len(usable),
            },
            agent=AGENT_NAME,
            research_id=research_id,
        )

        report = ground(analysis, usable, domains=domains)

        log.info(
            "analysis.completed",
            research_id=research_id,
            evidence_considered=report.evidence_considered,
            findings=len(report.analysis.findings),
            corroborated=len(report.analysis.corroborated_findings),
            tradeoffs=len(report.analysis.tradeoffs),
            contradictions=len(report.analysis.contradictions),
            recommendations=len(report.analysis.recommendations),
            open_questions=len(report.analysis.open_questions),
            dropped=len(report.dropped),
            drop_rate=report.drop_rate,
            prompt_version=self.prompt.version,
        )
        return report

    def _usable(self, evidence: list[Evidence]) -> list[Evidence]:
        """The evidence an analysis may rest on, strongest first.

        Filtering here rather than in the prompt is the point: a rejected
        passage is not shown to the model at all, so no instruction has to hold
        for a conclusion to be unable to cite one.
        """
        usable = [item for item in evidence if item.is_usable]
        usable.sort(key=lambda item: (-item.weight, item.id))
        return usable[: self.max_evidence]

    def _gaps(self, task_results: list[TaskResult] | None) -> list[str]:
        """Aspects of the question that produced no usable sources.

        Handed to the analyst so a gap can be named. Without it the model sees
        only what was found, and a question with a missing half looks like a
        question that was fully answered.
        """
        return [
            f"{result.task_id}: {result.stop_reason}"
            for result in (task_results or [])
            if not result.usable_sources
        ]
