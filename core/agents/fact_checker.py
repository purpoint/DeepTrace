"""The fact checker: does the evidence actually support the claim.

Everything upstream establishes that citations are *present*. The quote verifier
proves a passage appears in its source; grounding proves a citation resolves to a
passage that exists. Neither asks whether the passage says what the claim says it
says, and a claim can be perfectly cited and still wrong.

Two things this agent does that the analyst structurally could not:

*It reads evidence the claim did not cite.* The analyst cited what supported its
conclusions. A passage from another task that contradicts one was never brought
into contact with it, because research is decomposed and each task searches
alone. Retrieval closes that gap, and a contradicting passage is the single most
valuable thing this stage can find.

*It is adversarial by construction.* The analyst was asked what the evidence
supports; this agent is asked whether the evidence supports a specific
statement. Asking the second question of the model that produced the first would
be asking it to find fault with its own reasoning, so the claim arrives here
stripped of the reasoning that produced it -- just the statement and the
passages.

Two checks are kept out of the model entirely. Overgeneralization has a
deterministic component -- a universal quantifier that appears in no supporting
passage -- because a model assessing whether a confident sentence is too
confident tends to agree with the sentence. And a verdict cannot exceed what the
pipeline established: a claim resting only on paraphrase cannot be "supported",
whatever the model decides.
"""

from __future__ import annotations

import asyncio

from core.llm.client import LLMClient
from core.logging import get_logger
from core.models.claim import Claim, ClaimSet, ClaimStatus
from core.models.evidence import Evidence
from core.models.verification import (
    ClaimVerification,
    Disposition,
    VerificationReport,
    apply,
    is_narrower,
    overgeneralization,
)
from core.prompts.fact_checker import FACT_CHECKER_V1
from core.prompts.registry import Prompt
from core.retrieval import EvidenceRetriever, LexicalRetriever

log = get_logger(__name__)

AGENT_NAME = "fact_checker"

MAX_RELATED = 6
"""Uncited passages compared against each claim.

Bounded because verification runs per claim and every passage added is prompt
paid for once per claim rather than once per run.
"""

MAX_PASSAGE_CHARS = 500


def _render(labels: dict[str, Evidence], *, empty: str) -> str:
    if not labels:
        return empty
    lines = []
    for label, item in labels.items():
        status = item.verification.status.value if item.verification else "unchecked"
        lines.append(
            f"{label}. [{status} | weight {item.weight}]\n"
            f'   "{item.supporting_text[:MAX_PASSAGE_CHARS]}"'
        )
    return "\n".join(lines)


class FactChecker:
    """Checks claims against the evidence available to the whole run."""

    def __init__(
        self,
        client: LLMClient,
        *,
        prompt: Prompt = FACT_CHECKER_V1,
        retriever: EvidenceRetriever | None = None,
        max_related: int = MAX_RELATED,
        max_concurrency: int = 4,
    ) -> None:
        self.client = client
        self.prompt = prompt
        self.retriever = retriever or LexicalRetriever()
        self.max_related = max_related
        self.max_concurrency = max_concurrency

    async def check(
        self,
        claims: ClaimSet,
        evidence: list[Evidence],
        *,
        question: str,
        research_id: str | None = None,
    ) -> tuple[ClaimSet, VerificationReport]:
        """Check every claim, returning the claims with their verdicts applied.

        Claims are checked concurrently under a bound, like extraction: one call
        per claim, and a run can hold a dozen.

        A claim that could not be checked keeps its proposed status rather than
        being marked unsupported. Failing to check something is not evidence
        against it, and quietly converting one into the other would let a
        provider outage look like a claim being refuted.
        """
        if not claims.claims:
            return claims, VerificationReport()

        by_id = {item.id: item for item in evidence}
        semaphore = asyncio.Semaphore(self.max_concurrency)
        compared: set[str] = set()

        async def worker(claim: Claim) -> tuple[Claim, ClaimVerification | None, str | None]:
            async with semaphore:
                cited = [by_id[eid] for eid in claim.evidence_ids if eid in by_id]
                related = [
                    item
                    for item in self.retriever.related(
                        claim.text, evidence, limit=self.max_related + len(cited)
                    )
                    if item.id not in set(claim.evidence_ids)
                ][: self.max_related]

                compared.update(item.id for item in (*cited, *related))
                try:
                    verification = await self._check_one(
                        claim,
                        cited=cited,
                        related=related,
                        question=question,
                        research_id=research_id,
                    )
                except Exception as exc:
                    return claim, None, f"{type(exc).__name__}: {exc}"

                return claim, self._enforce(claim, verification, cited, question), None

        outcomes = await asyncio.gather(*(worker(claim) for claim in claims.claims))

        report = VerificationReport(evidence_compared=len(compared))
        checked: list[Claim] = []
        for claim, verification, failure in outcomes:
            if verification is None:
                report.failed.append((claim.id, failure or "unknown"))
                checked.append(claim)
                continue
            report.verdicts[claim.id] = verification
            checked.append(apply(claim, verification))

        verified = claims.model_copy(update={"claims": checked})
        log.info(
            "verification.completed",
            research_id=research_id,
            claims=len(checked),
            evidence_compared=report.evidence_compared,
            failed=len(report.failed),
            follow_ups=len(report.follow_up_questions),
            **report.counts(),
        )
        return verified, report

    async def _check_one(
        self,
        claim: Claim,
        *,
        cited: list[Evidence],
        related: list[Evidence],
        question: str,
        research_id: str | None,
    ) -> ClaimVerification:
        """One claim, one model call.

        Labels are scoped to this call, and the two groups are labelled apart
        (C for cited, R for related) so the model's answer says which pool a
        passage came from -- which is how "a related passage contradicts this"
        becomes visible rather than being lost among the citations.
        """
        cited_labels = {f"C{index}": item for index, item in enumerate(cited, start=1)}
        related_labels = {f"R{index}": item for index, item in enumerate(related, start=1)}

        verification = await self.client.complete_structured(
            self.prompt,
            ClaimVerification,
            {
                "question": question,
                "claim": claim.text,
                "condition": f"Stated to hold: {claim.condition}" if claim.condition else "",
                "cited": _render(cited_labels, empty="(none)"),
                "related": _render(related_labels, empty="(none found)"),
            },
            agent=AGENT_NAME,
            research_id=research_id,
        )

        table = {**cited_labels, **related_labels}
        return verification.model_copy(
            update={
                "supporting_evidence_ids": _resolve(verification.supporting_evidence_ids, table),
                "contradicting_evidence_ids": _resolve(
                    verification.contradicting_evidence_ids, table
                ),
            }
        )

    def _enforce(
        self,
        claim: Claim,
        verification: ClaimVerification,
        cited: list[Evidence],
        question: str,
    ) -> ClaimVerification:
        """Apply what the model does not get to decide.

        The deterministic overgeneralization check wins where it fires: a
        quantifier absent from every supporting passage is a fact about the
        text, not a judgement, and a claim carrying one is at best partially
        supported however the model rated it.

        A follow-up that merely restates the research question is dropped, and
        the disposition falls back to revising. Re-running the search that
        already failed to settle a claim spends the same money for the same
        result, so a research_more with nothing narrower to ask is not a plan.
        """
        reaching = overgeneralization(claim.text, cited)
        updates: dict[str, object] = {}

        if reaching is not None:
            updates["overgeneralization"] = verification.overgeneralization or reaching
            if verification.verdict is ClaimStatus.SUPPORTED:
                updates["verdict"] = ClaimStatus.PARTIALLY_SUPPORTED

        follow_up = verification.follow_up_question
        if follow_up and not is_narrower(follow_up, question):
            log.info(
                "verification.follow_up_rejected",
                claim_id=claim.id,
                follow_up=follow_up[:120],
            )
            updates["follow_up_question"] = None
            if verification.disposition is Disposition.RESEARCH_MORE:
                updates["disposition"] = Disposition.REVISE

        return verification.model_copy(update=updates) if updates else verification


def _resolve(labels: list[str], table: dict[str, Evidence]) -> list[str]:
    """Map the labels this call showed the model back to evidence ids.

    Same discipline as grounding an analysis: a label the model invented has
    nothing to resolve to and is dropped, so a verdict cannot cite a passage
    that was never compared.
    """
    resolved: list[str] = []
    for label in labels:
        item = table.get(label.strip().upper())
        if item is not None and item.id not in resolved:
            resolved.append(item.id)
    return resolved
