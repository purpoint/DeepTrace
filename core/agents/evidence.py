"""The evidence agent: turning retrieved pages into attributed passages.

This is the step that makes the project's central promise real. Before it, the
system has documents. After it, it has statements that each point at a specific
passage in a specific document.

The agent does one thing the model cannot be trusted to do for itself: it checks
that every extracted passage actually appears in the source. A model asked for a
supporting quote will sometimes produce a sentence that reads exactly like
something the page would say but does not appear on it, and by eye that is
indistinguishable from a real quotation. Verification is deterministic string
matching against the retrieved text, so a fabricated passage cannot enter the
evidence pool no matter how plausible it sounds.
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from core.llm.client import LLMClient
from core.logging import get_logger
from core.models.evidence import (
    Evidence,
    EvidenceExtractionReport,
    QuoteStatus,
    SupportStrength,
    verify_quotation,
)
from core.models.source import Source
from core.observability.recorder import new_run_id
from core.prompts.evidence import EVIDENCE_EXTRACTOR_V1
from core.prompts.registry import Prompt, wrap_untrusted

log = get_logger(__name__)

AGENT_NAME = "evidence"

MAX_ITEMS_PER_SOURCE = 5
MAX_DOCUMENT_CHARS = 12_000


class ExtractedItem(BaseModel):
    """One passage as the model produced it, before verification."""

    model_config = {"extra": "forbid"}

    claim: str = Field(min_length=10, max_length=400)
    supporting_text: str = Field(min_length=10, max_length=2000)
    location: str = Field(default="", max_length=200)
    support_strength: SupportStrength = SupportStrength.MODERATE


class ExtractionResult(BaseModel):
    """Everything extracted from one source.

    ``injection_observed`` exists because the extraction prompt tells the model
    to report instructions embedded in a document rather than follow them. That
    turns a prompt-injection attempt into a source-quality signal instead of
    something that is merely ignored.
    """

    model_config = {"extra": "forbid"}

    evidence: list[ExtractedItem] = Field(default_factory=list, max_length=10)
    injection_observed: bool = Field(
        default=False,
        description="Whether the document contained text addressed to the reader.",
    )


class EvidenceAgent:
    """Extracts attributed evidence from sources."""

    def __init__(
        self,
        client: LLMClient,
        *,
        prompt: Prompt = EVIDENCE_EXTRACTOR_V1,
        max_items_per_source: int = MAX_ITEMS_PER_SOURCE,
        max_concurrency: int = 4,
    ) -> None:
        self.client = client
        self.prompt = prompt
        self.max_items_per_source = max_items_per_source
        self.max_concurrency = max_concurrency

    async def extract(
        self,
        sources: list[Source],
        *,
        question: str,
        task_id: str | None = None,
        research_id: str | None = None,
    ) -> EvidenceExtractionReport:
        """Extract evidence from every usable source.

        Sources are processed concurrently under a bound. Extraction is one call
        per source and a research run can hold fifty of them, so doing it
        serially would dominate latency -- while doing it unbounded would hit
        provider rate limits and spend the token budget in a burst.
        """
        usable = [source for source in sources if source.has_content]
        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def worker(source: Source) -> tuple[Source, ExtractionResult | None]:
            async with semaphore:
                return source, await self._extract_one(
                    source, question=question, task_id=task_id, research_id=research_id
                )

        outcomes = await asyncio.gather(*(worker(source) for source in usable))

        report = EvidenceExtractionReport(sources_processed=len(usable))
        for source, extraction in outcomes:
            if extraction is None:
                report.sources_failed += 1
                continue
            if extraction.injection_observed:
                report.injection_attempts.append(source.domain)
                log.warning(
                    "evidence.injection_observed",
                    research_id=research_id,
                    source_id=source.id,
                    domain=source.domain,
                )
            self._verify_into(extraction, source=source, task_id=task_id, report=report)

        log.info(
            "evidence.extracted",
            research_id=research_id,
            task_id=task_id,
            sources=report.sources_processed,
            failed_sources=report.sources_failed,
            evidence=len(report.evidence),
            verbatim=len(report.verified_evidence),
            rejected=len(report.rejected),
            rejection_rate=report.rejection_rate,
            injection_attempts=len(report.injection_attempts),
        )
        return report

    async def _extract_one(
        self,
        source: Source,
        *,
        question: str,
        task_id: str | None,
        research_id: str | None,
    ) -> ExtractionResult | None:
        """Extract from one source, returning None if the call failed.

        A source that could not be processed must not fail the others, so the
        failure is counted and the remaining sources continue.
        """
        document = wrap_untrusted(
            source.content, source=source.domain or source.url, max_chars=MAX_DOCUMENT_CHARS
        )
        try:
            return await self.client.complete_structured(
                self.prompt,
                ExtractionResult,
                {
                    "question": question,
                    "source_title": source.title or "(untitled)",
                    "source_domain": source.domain or source.url,
                    "max_items": self.max_items_per_source,
                    "document": document,
                },
                agent=AGENT_NAME,
                research_id=research_id,
                task_id=task_id,
            )
        except Exception as exc:
            log.warning(
                "evidence.extraction_failed",
                research_id=research_id,
                source_id=source.id,
                domain=source.domain,
                error=str(exc),
            )
            return None

    def _verify_into(
        self,
        extraction: ExtractionResult,
        *,
        source: Source,
        task_id: str | None,
        report: EvidenceExtractionReport,
    ) -> None:
        """Verify each passage against the source and keep only what survives.

        Verification runs against ``source.content`` -- the text actually
        retrieved -- rather than against anything the model returned. Checking a
        model's output against the same model's output would prove nothing.
        """
        for item in extraction.evidence:
            verification = verify_quotation(item.supporting_text, source.content)

            if verification.status is QuoteStatus.NOT_FOUND:
                report.rejected.append(
                    (
                        item.claim,
                        f"passage not found in {source.domain or source.url} "
                        f"(similarity {verification.similarity})",
                    )
                )
                log.warning(
                    "evidence.passage_not_in_source",
                    source_id=source.id,
                    domain=source.domain,
                    similarity=verification.similarity,
                    claim=item.claim[:120],
                )
                continue

            report.evidence.append(
                Evidence(
                    id=new_run_id("ev"),
                    source_id=source.id,
                    task_id=task_id,
                    claim=item.claim,
                    supporting_text=item.supporting_text,
                    location=item.location,
                    support_strength=item.support_strength,
                    verification=verification,
                    source_quality=source.quality_score,
                )
            )
