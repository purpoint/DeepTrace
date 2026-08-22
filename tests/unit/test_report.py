"""Tests for the report.

Every guarantee the pipeline built is worth what this document's fidelity to it
turns out to be. A citation number pointing at the wrong passage, or a claim the
fact checker refused appearing as a plain assertion, undoes the quote verifier,
the grounding pass and the verifier in one sentence.

So the tests are the acceptance criteria, stated as failures: a citation that
resolves to nothing, a sentence with no verified claim behind it, and a weakly
supported claim written as though it were settled.
"""

from __future__ import annotations

import json

import pytest

from core.agents.reporter import Reporter
from core.llm.client import LLMClient, ModelRouter
from core.models.claim import Claim, ClaimKind, ClaimSet, ClaimStatus, EvidenceLink
from core.models.evidence import (
    Evidence,
    QuoteStatus,
    QuoteVerification,
    SupportStrength,
)
from core.models.report import (
    DraftReport,
    SectionKind,
    build_citations,
    claims_for_report,
    confidence_note,
    render_markdown,
)
from core.models.source import Source, SourceType
from tests.fakes import FakeProvider

pytestmark = pytest.mark.unit

ROUTER = ModelRouter("fake", "cheap-model", "strong-model", "embed-model")
QUESTION = "How does Kafka guarantee message ordering within a partition?"


def evidence(evidence_id: str = "ev_1", source_id: str = "src_1") -> Evidence:
    return Evidence(
        id=evidence_id,
        source_id=source_id,
        task_id="ordering",
        claim="Kafka preserves order within a partition.",
        supporting_text="Records are appended to a partition in the order they are sent.",
        location="Ordering guarantees",
        support_strength=SupportStrength.STRONG,
        verification=QuoteVerification(status=QuoteStatus.VERBATIM, similarity=1.0),
        source_quality=0.97,
    )


def source(source_id: str = "src_1", domain: str = "kafka.apache.org") -> Source:
    return Source(
        id=source_id,
        url=f"https://{domain}/documentation",
        title="Kafka Documentation",
        domain=domain,
        source_type=SourceType.OFFICIAL_DOCS,
        quality_score=0.97,
        task_id="ordering",
        content="Records are appended in the order they are sent." * 12,
        word_count=90,
    )


def claim(
    text: str = "Kafka preserves record order within a partition.",
    *,
    claim_id: str = "res_1:find_1",
    status: ClaimStatus = ClaimStatus.SUPPORTED,
    evidence_ids: tuple[str, ...] = ("ev_1",),
    kind: ClaimKind = ClaimKind.FINDING,
) -> Claim:
    return Claim(
        id=claim_id,
        text=text,
        kind=kind,
        status=status,
        evidence=[
            EvidenceLink(evidence_id=eid, source_id="src_1", weight=0.9, verbatim=True)
            for eid in evidence_ids
        ],
    )


def draft_json(body: str, *, kind: str = "summary", claim_ids: list[str] | None = None) -> str:
    return json.dumps(
        {
            "title": "Ordering guarantees in Kafka",
            "sections": [
                {
                    "kind": kind,
                    "body": body,
                    "claim_ids": claim_ids if claim_ids is not None else ["res_1:find_1"],
                }
            ],
        }
    )


def reporter(*responses: object) -> Reporter:
    return Reporter(LLMClient(FakeProvider(responses), router=ROUTER, max_repair_attempts=0))


async def write(agent: Reporter, claims: ClaimSet, **kwargs: object):  # type: ignore[no-untyped-def]
    return await agent.write(
        claims,
        question=QUESTION,
        evidence=kwargs.pop("evidence", [evidence()]),  # type: ignore[arg-type]
        sources=kwargs.pop("sources", [source()]),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


class TestEveryCitationResolves:
    """The roadmap's first acceptance criterion, and the easiest to fail
    silently: a number in brackets reads as provenance whether or not it has
    any, and a reader cannot tell without clicking."""

    async def test_a_citation_pointing_at_nothing_is_removed(self) -> None:
        agent = reporter(draft_json("Ordering is preserved [1]. Something else [9]."))

        report = await write(agent, ClaimSet(claims=[claim()]))

        body = report.section(SectionKind.SUMMARY).body  # type: ignore[union-attr]
        assert "[1]" in body
        assert "[9]" not in body
        assert report.unresolved_markers == ["[9]"]
        assert report.is_fully_cited is False

    async def test_removing_a_citation_does_not_leave_a_damaged_sentence(self) -> None:
        agent = reporter(draft_json("Ordering is preserved [9]."))

        report = await write(agent, ClaimSet(claims=[claim()]))

        assert report.section(SectionKind.SUMMARY).body == "Ordering is preserved."  # type: ignore[union-attr]

    async def test_every_citation_resolves_to_a_real_source(self) -> None:
        agent = reporter(draft_json("Ordering is preserved [1]."))

        report = await write(agent, ClaimSet(claims=[claim()]))

        citation = report.citation(1)
        assert citation is not None
        assert citation.url == "https://kafka.apache.org/documentation"
        assert citation.quote.startswith("Records are appended")
        assert citation.evidence_id == "ev_1"

    def test_one_passage_gets_one_number_however_many_claims_use_it(self) -> None:
        """Numbering it twice would make one source look like two, which is the
        same false corroboration the evidence layer guards against."""
        citations = build_citations(
            [claim(claim_id="res_1:find_1"), claim(claim_id="res_1:find_2")],
            [evidence()],
            [source()],
        )

        assert len(citations) == 1
        assert citations[0].claim_ids == ["res_1:find_1", "res_1:find_2"]


class TestNothingUnverifiedReachesTheReport:
    """The roadmap's second criterion. The generator is never shown a rejected
    claim, so this is a structural property rather than an instruction."""

    def test_an_unsupported_claim_is_not_offered_to_the_generator(self) -> None:
        claims = ClaimSet(
            claims=[
                claim(claim_id="res_1:find_1"),
                claim(
                    "A claim the checker refused.",
                    claim_id="res_1:find_2",
                    status=ClaimStatus.UNSUPPORTED,
                ),
            ]
        )

        offered = claims_for_report(claims)

        assert [item.id for item in offered] == ["res_1:find_1"]

    async def test_a_section_citing_an_unpublishable_claim_has_it_stripped(self) -> None:
        """An invented claim id in a report is a sentence with nothing behind
        it, and it would still read like every other sentence."""
        agent = reporter(
            draft_json("Ordering is preserved [1].", claim_ids=["res_1:find_1", "res_1:ghost"])
        )

        report = await write(agent, ClaimSet(claims=[claim()]))

        assert report.section(SectionKind.SUMMARY).claim_ids == ["res_1:find_1"]  # type: ignore[union-attr]
        assert report.unsupported_claim_ids == ["res_1:ghost"]

    async def test_a_run_with_nothing_publishable_still_produces_a_report(self) -> None:
        """A real answer -- the research established nothing -- and more useful
        than an empty file, which reads as a crash."""
        agent = reporter()
        claims = ClaimSet(claims=[claim(status=ClaimStatus.UNSUPPORTED)])

        report = await write(agent, claims)

        assert "No verified answer" in report.title
        assert report.section(SectionKind.METHOD) is not None
        assert agent.client.provider.calls == 0, "a model was asked to write from nothing"

    async def test_the_generator_never_sees_a_source(self) -> None:
        """It cannot cite a page it was not shown, which is stronger than asking
        it not to."""
        agent = reporter(draft_json("Ordering is preserved [1]."))

        await write(agent, ClaimSet(claims=[claim()]))

        sent = agent.client.provider.requests[0].messages[1].content  # type: ignore[attr-defined]
        assert "kafka.apache.org/documentation" not in sent
        assert "res_1:find_1" in sent


class TestWeakEvidenceIsStatedWeakly:
    """The roadmap's third criterion. Enforced by instructing per claim rather
    than once as a rule, because "hedge where the evidence is weak" is exactly
    what a fluent model applies unevenly."""

    def test_a_partially_supported_claim_must_carry_its_limitation(self) -> None:
        note = confidence_note(claim(status=ClaimStatus.PARTIALLY_SUPPORTED))

        assert "limitation" in note
        assert "flatly" in note

    def test_a_conflicting_claim_must_be_presented_as_a_disagreement(self) -> None:
        note = confidence_note(claim(status=ClaimStatus.CONFLICTING))

        assert "disagree" in note
        assert "both" in note

    def test_an_unchecked_claim_must_be_attributed_not_asserted(self) -> None:
        note = confidence_note(claim(status=ClaimStatus.PROPOSED))

        assert "not assert" in note

    def test_a_single_publisher_claim_is_not_described_as_corroborated(self) -> None:
        single = claim()
        corroborated = claim().model_copy(update={"corroborating_publishers": 3})

        assert "single publisher" in confidence_note(single)
        assert "more than one publisher" in confidence_note(corroborated)

    async def test_the_instruction_reaches_the_generator_per_claim(self) -> None:
        agent = reporter(draft_json("Ordering is preserved in some setups [1]."))
        claims = ClaimSet(claims=[claim(status=ClaimStatus.PARTIALLY_SUPPORTED)])

        await write(agent, claims)

        sent = agent.client.provider.requests[0].messages[1].content  # type: ignore[attr-defined]
        assert "how to state it: state with its limitation" in sent


class TestTheAssembledSections:
    """Three sections contain no model output. A reader who trusts nothing the
    model wrote can still check the question, the work, and the sources."""

    async def test_the_model_cannot_write_the_method_section(self) -> None:
        with pytest.raises(ValueError, match="cannot be written"):
            DraftReport(
                title="A report",
                sections=[{"kind": "method", "body": "I searched very carefully."}],  # type: ignore[list-item]
            )

    async def test_the_method_section_counts_what_the_run_actually_did(self) -> None:
        from core.models.research import SufficiencyVerdict, TaskResult

        agent = reporter(draft_json("Ordering is preserved [1]."))
        results = [
            TaskResult(
                task_id="ordering",
                question="How are records ordered?",
                sources=[source()],
                verdict=SufficiencyVerdict.SUFFICIENT,
                stop_reason="evidence is sufficient",
                rounds=1,
            )
        ]

        report = await write(agent, ClaimSet(claims=[claim()]), task_results=results)

        method = report.section(SectionKind.METHOD).body  # type: ignore[union-attr]
        assert "1 research task" in method
        assert "1 sources" in method
        assert "matched word for word" in method

    async def test_the_question_section_states_the_assumptions_that_were_made(self) -> None:
        from core.models.query import Ambiguity, QuerySpec, ResearchType, TimeSensitivity

        spec = QuerySpec(
            normalized_question="How does Kafka guarantee ordering within a partition?",
            research_type=ResearchType.EXPLANATION,
            scope=["partition ordering"],
            success_criteria=["ordering guarantees are described"],
            time_sensitivity=TimeSensitivity.STATIC,
            requires_current_information=False,
            ambiguities=[
                Ambiguity(
                    aspect="version",
                    assumption="the research assumed a recent Kafka release",
                    why_it_matters="ordering guarantees changed across major versions",
                )
            ],
        )
        agent = reporter(draft_json("Ordering is preserved [1]."))

        report = await write(agent, ClaimSet(claims=[claim()]), spec=spec)

        question = report.section(SectionKind.QUESTION).body  # type: ignore[union-attr]
        assert "assumed" in question
        assert "recent Kafka release" in question

    async def test_the_sources_section_is_rendered_from_the_citation_table(self) -> None:
        agent = reporter(draft_json("Ordering is preserved [1]."))

        report = await write(agent, ClaimSet(claims=[claim()]))
        document = render_markdown(report)

        assert "## Sources" in document
        assert "https://kafka.apache.org/documentation" in document
        assert "Records are appended" in document


class TestRendering:
    async def test_sections_appear_in_reading_order(self) -> None:
        draft = json.dumps(
            {
                "title": "Ordering in Kafka",
                "sections": [
                    {"kind": "limitations", "body": "Retries were not examined.", "claim_ids": []},
                    {"kind": "summary", "body": "Order holds per partition [1].", "claim_ids": []},
                ],
            }
        )
        agent = reporter(draft)

        report = await write(agent, ClaimSet(claims=[claim()]))
        document = render_markdown(report)

        assert document.index("## Summary") < document.index("## Limitations")
        assert document.index("## Question") < document.index("## Summary")
        assert document.index("## Limitations") < document.index("## How this was researched")

    async def test_a_paraphrased_citation_is_marked_as_one(self) -> None:
        """A paraphrase and a quotation are different kinds of support, and
        rendering them identically makes the weaker look like the stronger."""
        paraphrased = evidence().model_copy(
            update={
                "verification": QuoteVerification(status=QuoteStatus.PARAPHRASED, similarity=0.8)
            }
        )
        agent = reporter(draft_json("Ordering is preserved [1]."))

        report = await write(agent, ClaimSet(claims=[claim()]), evidence=[paraphrased])

        assert "(paraphrased)" in render_markdown(report)


class TestClaimOrdering:
    def test_findings_come_before_recommendations(self) -> None:
        claims = ClaimSet(
            claims=[
                claim(
                    "Enable idempotence.",
                    claim_id="res_1:reco_1",
                    kind=ClaimKind.RECOMMENDATION,
                ),
                claim(claim_id="res_1:find_1"),
            ]
        )

        ordered = claims_for_report(claims)

        assert [item.kind for item in ordered] == [ClaimKind.FINDING, ClaimKind.RECOMMENDATION]
