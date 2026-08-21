"""Tests for evidence extraction and quotation verification.

Verification is the anti-fabrication mechanism, so it is tested in both
directions: a fabricated passage must be rejected, and a genuine one must not
be. A verifier that rejects everything would look secure and destroy the
system's ability to cite anything.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from core.agents.evidence import EvidenceAgent
from core.llm.client import LLMClient, ModelRouter
from core.llm.retry import RetryPolicy
from core.models.evidence import (
    PARAPHRASE_THRESHOLD,
    Evidence,
    QuoteStatus,
    QuoteVerification,
    SupportStrength,
    normalise,
    verify_quotation,
)
from core.models.source import Source, SourceType
from core.observability.recorder import InMemoryRunRecorder
from core.prompts.evidence import EVIDENCE_EXTRACTOR_V1
from tests.fakes import FakeProvider

pytestmark = pytest.mark.unit


ROUTER = ModelRouter(
    provider_id="fake",
    cheap_model="gemini-3.5-flash-lite",
    strong_model="gemini-3.7-flash",
    embed_model="gemini-embedding-001",
)

DOCUMENT = (
    "Kafka provides ordering guarantees at the partition level. Records sent by a "
    "producer to a particular partition are appended in the order they are sent, and "
    "a consumer sees records in the order they are stored in the log. There is no "
    "global ordering guarantee across partitions. Applications that need total "
    "ordering must use a single partition, which limits throughput to one consumer "
    "per consumer group."
)

REAL_QUOTE = (
    "Records sent by a producer to a particular partition are appended in the order they are sent"
)


def source(content: str = DOCUMENT, **kwargs: object) -> Source:
    defaults: dict[str, object] = {
        "id": "src_1",
        "url": "https://kafka.apache.org/documentation/",
        "title": "Kafka Documentation",
        "domain": "kafka.apache.org",
        "source_type": SourceType.OFFICIAL_DOCS,
        "quality_score": 0.97,
        "content": content,
        "word_count": len(content.split()),
    }
    defaults.update(kwargs)
    return Source(**defaults)  # type: ignore[arg-type]


def extraction_json(items: list[dict[str, object]] | None = None, injection: bool = False) -> str:
    return json.dumps(
        {
            "evidence": items
            if items is not None
            else [
                {
                    "claim": "Kafka preserves record order within a single partition.",
                    "supporting_text": REAL_QUOTE,
                    "location": "Ordering guarantees section",
                    "support_strength": "strong",
                }
            ],
            "injection_observed": injection,
        }
    )


def make_agent(*responses: object) -> tuple[EvidenceAgent, InMemoryRunRecorder]:
    recorder = InMemoryRunRecorder()
    client = LLMClient(
        FakeProvider(responses),
        router=ROUTER,
        recorder=recorder,
        retry_policy=RetryPolicy(max_attempts=1, initial_delay_seconds=0.001, jitter=0.0),
    )
    return EvidenceAgent(client), recorder


# ---------------------------------------------------------------------------
# Quotation verification
# ---------------------------------------------------------------------------


class TestFabricationIsRejected:
    """The failure this module exists to catch: a passage that reads exactly
    like something the page would say, but does not appear on it."""

    def test_an_invented_benchmark_is_rejected(self) -> None:
        result = verify_quotation(
            "Kafka achieves 2 million messages per second on commodity hardware", DOCUMENT
        )
        assert result.status is QuoteStatus.NOT_FOUND
        assert result.is_usable is False

    def test_a_passage_contradicting_the_source_is_rejected(self) -> None:
        result = verify_quotation(
            "Kafka guarantees strict global ordering across all partitions", DOCUMENT
        )
        assert result.status is QuoteStatus.NOT_FOUND

    def test_a_plausible_but_absent_sentence_is_rejected(self) -> None:
        """Reads like documentation. Is not in the documentation."""
        result = verify_quotation(
            "Consumers may replay records from any offset within the retention window",
            DOCUMENT,
        )
        assert result.status is QuoteStatus.NOT_FOUND

    def test_words_scattered_across_the_document_do_not_count(self) -> None:
        """A long page contains most short passages' words somewhere. Requiring
        them to appear together is the point of the sliding window."""
        scattered = "global partition consumer throughput ordering single group log"
        assert verify_quotation(scattered, DOCUMENT).status is QuoteStatus.NOT_FOUND

    def test_empty_passage_is_rejected(self) -> None:
        assert verify_quotation("   ", DOCUMENT).status is QuoteStatus.NOT_FOUND

    def test_a_source_with_no_text_supports_nothing(self) -> None:
        assert verify_quotation(REAL_QUOTE, "").status is QuoteStatus.NOT_FOUND


class TestGenuineQuotesSurvive:
    """A verifier that rejects everything would look secure while destroying
    the system's ability to cite anything."""

    def test_an_exact_quote_is_verbatim(self) -> None:
        result = verify_quotation(REAL_QUOTE, DOCUMENT)
        assert result.status is QuoteStatus.VERBATIM
        assert result.similarity == 1.0

    def test_whitespace_differences_still_match(self) -> None:
        """Extracted text routinely differs in line breaks and spacing."""
        reflowed = REAL_QUOTE.replace(" ", "\n  ", 3)
        assert verify_quotation(reflowed, DOCUMENT).status is QuoteStatus.NORMALISED

    def test_typographic_variants_still_match(self) -> None:
        """Models silently convert curly quotes and em dashes to ASCII.
        Rejecting a quote over an apostrophe would discard real evidence."""
        typographic = (
            "Kafka\u2019s ordering guarantees apply at the partition level "
            "\u2014 records are appended in order."
        )
        ascii_version = (
            "Kafka's ordering guarantees apply at the partition level "
            "- records are appended in order."
        )
        assert verify_quotation(ascii_version, typographic).status is QuoteStatus.NORMALISED

    def test_a_quotable_status_is_reported(self) -> None:
        assert verify_quotation(REAL_QUOTE, DOCUMENT).status.is_quotable is True


class TestParaphraseHandling:
    def test_a_near_verbatim_passage_is_marked_not_rejected(self) -> None:
        near = (
            "Records sent by a producer to a particular partition are appended in the "
            "order that they are sent"
        )
        result = verify_quotation(near, DOCUMENT)

        assert result.status is QuoteStatus.PARAPHRASED
        assert result.similarity >= PARAPHRASE_THRESHOLD

    def test_a_paraphrase_is_usable_but_not_quotable(self) -> None:
        """The source says it, but not in these words. Presenting it as a
        quotation would misrepresent the page."""
        assert QuoteStatus.PARAPHRASED.is_usable is True
        assert QuoteStatus.PARAPHRASED.is_quotable is False


class TestNormalisation:
    def test_case_is_preserved(self) -> None:
        """A quotation that changes case is not verbatim."""
        assert "Kafka" in normalise("Kafka provides ordering")

    def test_whitespace_collapses(self) -> None:
        assert normalise("a  \n\t b") == "a b"

    def test_zero_width_characters_are_removed(self) -> None:
        assert normalise("Kaf​ka") == "Kafka"


# ---------------------------------------------------------------------------
# The evidence model
# ---------------------------------------------------------------------------


class TestEvidenceModel:
    def test_provenance_is_required(self) -> None:
        """Without a source id, evidence cannot be traced back to a document,
        which is the entire promise."""
        with pytest.raises(ValidationError):
            Evidence(id="ev_1", claim="A claim about things", supporting_text="Some text here")

    def test_weight_combines_strength_quality_and_fidelity(self) -> None:
        strong = Evidence(
            id="ev_1",
            source_id="src_1",
            claim="Kafka preserves order within a partition.",
            supporting_text=REAL_QUOTE,
            support_strength=SupportStrength.STRONG,
            source_quality=1.0,
            verification=QuoteVerification(status=QuoteStatus.VERBATIM, similarity=1.0),
        )
        weak = strong.model_copy(update={"support_strength": SupportStrength.WEAK})

        assert strong.weight == 1.0
        assert weak.weight < strong.weight

    def test_a_paraphrase_weighs_less_than_a_quotation(self) -> None:
        """Even when the source genuinely says it: the wording that was checked
        is not the wording being relied on."""
        quoted = Evidence(
            id="ev_1",
            source_id="src_1",
            claim="Kafka preserves order within a partition.",
            supporting_text=REAL_QUOTE,
            source_quality=1.0,
            verification=QuoteVerification(status=QuoteStatus.VERBATIM, similarity=1.0),
        )
        paraphrased = quoted.model_copy(
            update={
                "verification": QuoteVerification(status=QuoteStatus.PARAPHRASED, similarity=0.8)
            }
        )
        assert paraphrased.weight < quoted.weight

    def test_low_quality_sources_weigh_less(self) -> None:
        base = {
            "id": "ev_1",
            "source_id": "src_1",
            "claim": "Kafka preserves order within a partition.",
            "supporting_text": REAL_QUOTE,
            "verification": QuoteVerification(status=QuoteStatus.VERBATIM, similarity=1.0),
        }
        assert (
            Evidence(**base, source_quality=0.97).weight
            > Evidence(  # type: ignore[arg-type]
                **base,
                source_quality=0.45,  # type: ignore[arg-type]
            ).weight
        )

    def test_unverified_evidence_is_not_marked_verified(self) -> None:
        item = Evidence(
            id="ev_1",
            source_id="src_1",
            claim="Kafka preserves order within a partition.",
            supporting_text=REAL_QUOTE,
        )
        assert item.is_verified is False


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------


class TestExtraction:
    async def test_evidence_is_extracted_with_provenance(self) -> None:
        """Claim -> Evidence -> Source -> URL, the chain the project promises."""
        agent, _ = make_agent(extraction_json())
        report = await agent.extract([source()], question="How does Kafka order records?")

        assert len(report.evidence) == 1
        item = report.evidence[0]
        assert item.source_id == "src_1"
        assert item.is_verified
        assert item.support_strength is SupportStrength.STRONG
        assert item.source_quality == 0.97

    async def test_a_fabricated_passage_never_becomes_evidence(self) -> None:
        """The central guarantee of this milestone."""
        agent, _ = make_agent(
            extraction_json(
                [
                    {
                        "claim": "Kafka sustains two million messages per second.",
                        "supporting_text": "Kafka achieves 2 million messages per second "
                        "on commodity hardware in production deployments.",
                        "location": "Performance",
                        "support_strength": "strong",
                    }
                ]
            )
        )
        report = await agent.extract([source()], question="How fast is Kafka?")

        assert report.evidence == []
        assert len(report.rejected) == 1
        assert "not found" in report.rejected[0][1]

    async def test_real_and_fabricated_passages_are_separated(self) -> None:
        agent, _ = make_agent(
            extraction_json(
                [
                    {
                        "claim": "Kafka preserves order within a partition.",
                        "supporting_text": REAL_QUOTE,
                        "location": "Ordering",
                        "support_strength": "strong",
                    },
                    {
                        "claim": "Kafka sustains two million messages per second.",
                        "supporting_text": "Kafka achieves 2 million messages per second.",
                        "location": "Performance",
                        "support_strength": "strong",
                    },
                ]
            )
        )
        report = await agent.extract([source()], question="How does Kafka order records?")

        assert len(report.evidence) == 1
        assert len(report.rejected) == 1
        assert report.rejection_rate == 0.5

    async def test_verification_uses_the_retrieved_text_not_the_model_output(self) -> None:
        """Checking a model's output against the same model's output would
        prove nothing. The comparison is against what was actually fetched."""
        agent, _ = make_agent(extraction_json())
        empty_page = source(content="")  # not usable, so never processed
        report = await agent.extract([empty_page], question="q")

        assert report.sources_processed == 0
        assert report.evidence == []

    async def test_thin_sources_are_skipped(self) -> None:
        agent, _ = make_agent(extraction_json())
        report = await agent.extract([source(content="Access denied.", word_count=2)], question="q")
        assert report.sources_processed == 0

    async def test_an_empty_extraction_is_a_valid_outcome(self) -> None:
        """A document that does not address the task should yield nothing.
        Forcing evidence out of it is how unsupported claims are born."""
        agent, _ = make_agent(extraction_json([]))
        report = await agent.extract([source()], question="What is the capital of France?")

        assert report.evidence == []
        assert report.rejected == []
        assert report.sources_processed == 1


class TestMultipleSources:
    async def test_sources_are_processed_concurrently(self) -> None:
        agent, recorder = make_agent(extraction_json())
        sources = [source(id=f"src_{n}", url=f"https://a{n}.example.com/") for n in range(4)]

        report = await agent.extract(sources, question="q")

        assert report.sources_processed == 4
        assert len(recorder.agent_runs) == 4

    async def test_one_failing_source_does_not_stop_the_others(self) -> None:
        """A single unrecoverable response must not discard the evidence the
        other sources produced.

        Concurrency is pinned to one and repair disabled, so the first source
        genuinely fails rather than being rescued by the client's repair loop
        or racing the second source for the queued response.
        """
        recorder = InMemoryRunRecorder()
        client = LLMClient(
            FakeProvider(["not json at all", extraction_json()]),
            router=ROUTER,
            recorder=recorder,
            retry_policy=RetryPolicy(max_attempts=1, initial_delay_seconds=0.001, jitter=0.0),
            max_repair_attempts=0,
        )
        agent = EvidenceAgent(client, max_concurrency=1)
        sources = [source(id="src_bad"), source(id="src_good")]

        report = await agent.extract(sources, question="q")

        assert report.sources_failed == 1
        assert len(report.evidence) == 1
        assert report.evidence[0].source_id == "src_good"

    async def test_evidence_records_which_source_it_came_from(self) -> None:
        agent, _ = make_agent(extraction_json())
        sources = [source(id="src_a"), source(id="src_b")]

        report = await agent.extract(sources, question="q")

        assert {item.source_id for item in report.evidence} == {"src_a", "src_b"}


class TestPromptInjection:
    async def test_document_content_is_wrapped(self) -> None:
        agent, _ = make_agent(extraction_json())
        await agent.extract([source()], question="q")

        sent = agent.client.provider.requests[0].messages[1].content  # type: ignore[attr-defined]
        assert "BEGIN UNTRUSTED CONTENT" in sent
        assert "must never be followed" in sent

    async def test_an_injection_attempt_is_recorded_as_a_source_signal(self) -> None:
        """The prompt asks the model to report embedded instructions rather than
        follow them, which turns an attack into a quality signal about the
        source instead of something merely ignored."""
        agent, _ = make_agent(extraction_json(injection=True))
        report = await agent.extract([source()], question="q")

        assert report.injection_attempts == ["kafka.apache.org"]


class TestObservability:
    async def test_runs_are_recorded_with_the_prompt_version(self) -> None:
        agent, recorder = make_agent(extraction_json())
        await agent.extract([source()], question="q", research_id="res_1", task_id="t1")

        run = recorder.agent_runs[0]
        assert run.agent == "evidence"
        assert run.prompt_name == "evidence_extractor"
        assert run.prompt_version == "v1"
        assert run.research_id == "res_1"
        assert run.task_id == "t1"

    async def test_extraction_runs_on_the_cheap_tier(self) -> None:
        """One call per source per task makes this the highest-volume model
        call in a research run."""
        agent, recorder = make_agent(extraction_json())
        await agent.extract([source()], question="q")

        assert recorder.agent_runs[0].tier == "cheap"

    async def test_the_report_summarises_what_happened(self) -> None:
        agent, _ = make_agent(extraction_json())
        summary = (await agent.extract([source()], question="q")).summary()

        assert "1 evidence" in summary
        assert "1 verbatim" in summary


class TestPromptContract:
    def test_prompt_demands_verbatim_copying(self) -> None:
        assert "exactly as it" in EVIDENCE_EXTRACTOR_V1.system.lower()

    def test_prompt_warns_that_passages_are_checked(self) -> None:
        """Telling the model the quote will be verified is cheaper than
        repairing the output afterwards."""
        assert "checked against the document" in EVIDENCE_EXTRACTOR_V1.system.lower()

    def test_prompt_permits_an_empty_result(self) -> None:
        """Without this the model manufactures evidence to avoid returning
        nothing, which is exactly the failure mode."""
        assert "empty list" in EVIDENCE_EXTRACTOR_V1.system.lower()

    def test_prompt_forbids_outside_knowledge(self) -> None:
        assert "not in this document" in EVIDENCE_EXTRACTOR_V1.system.lower()
