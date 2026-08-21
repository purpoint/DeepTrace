"""Tests for the query analyzer.

The analyzer's defining rule is that it must not answer the question. That is
enforced structurally -- QuerySpec has no field an answer could occupy -- and the
tests here assert the structural guarantee rather than inspecting generated prose
for signs of an answer.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from core.agents.query_analyzer import QueryAnalyzer
from core.config import ResearchDepth
from core.llm.client import LLMClient, ModelRouter
from core.llm.retry import RetryPolicy
from core.models.query import Ambiguity, QuerySpec, ResearchType, TimeSensitivity
from core.observability.recorder import InMemoryRunRecorder
from core.prompts.analyzer import QUERY_ANALYZER_V1
from tests.fakes import FakeProvider

pytestmark = pytest.mark.unit


ROUTER = ModelRouter(
    provider_id="fake",
    cheap_model="gemini-3.5-flash-lite",
    strong_model="gemini-3.7-flash",
    embed_model="gemini-embedding-001",
)


def spec_json(**overrides: object) -> str:
    payload: dict[str, object] = {
        "normalized_question": (
            "How do Kafka and RabbitMQ differ for a high-scale event-driven "
            "microservices architecture?"
        ),
        "research_type": "comparison",
        "scope": ["architecture", "delivery semantics", "operational complexity"],
        "out_of_scope": ["pricing of managed offerings"],
        "constraints": ["high-scale event-driven microservices"],
        "ambiguities": [],
        "success_criteria": ["Each scope item is covered for both systems"],
        "time_sensitivity": "evolving",
        "requires_current_information": True,
    }
    payload.update(overrides)
    return json.dumps(payload)


def make_analyzer(*responses: object) -> tuple[QueryAnalyzer, InMemoryRunRecorder]:
    recorder = InMemoryRunRecorder()
    client = LLMClient(
        FakeProvider(responses),
        router=ROUTER,
        recorder=recorder,
        retry_policy=RetryPolicy(max_attempts=2, initial_delay_seconds=0.001, jitter=0.0),
    )
    return QueryAnalyzer(client), recorder


# ---------------------------------------------------------------------------
# The central guarantee
# ---------------------------------------------------------------------------


class TestCannotAnswerTheQuestion:
    """The rule "do not answer" is enforced by the output shape, not by trust."""

    def test_schema_has_no_field_an_answer_could_occupy(self) -> None:
        fields = set(QuerySpec.model_fields)
        forbidden = {"answer", "conclusion", "finding", "findings", "result", "recommendation"}

        assert not (fields & forbidden)

    def test_extra_fields_are_rejected(self) -> None:
        """A model that invents an `answer` field fails validation rather than
        having it silently ignored."""
        with pytest.raises(ValidationError):
            QuerySpec.model_validate_json(spec_json(answer="Use Kafka."))

    @pytest.mark.parametrize(
        "answerish",
        [
            "The answer is that Kafka scales better than RabbitMQ for streaming.",
            "In conclusion, Kafka is the stronger choice for this architecture.",
            "You should use Kafka for high-throughput event streaming workloads.",
            "I recommend Kafka because it handles higher sustained throughput.",
        ],
    )
    def test_a_question_restated_as_a_verdict_is_rejected(self, answerish: str) -> None:
        """Catches the specific failure where the model resolves the question in
        the field meant to restate it, which would bias every later stage."""
        with pytest.raises(ValidationError, match="reads as an answer"):
            QuerySpec.model_validate_json(spec_json(normalized_question=answerish))

    def test_a_genuine_question_passes(self) -> None:
        spec = QuerySpec.model_validate_json(spec_json())
        assert "differ" in spec.normalized_question


# ---------------------------------------------------------------------------
# Schema behaviour
# ---------------------------------------------------------------------------


class TestSchemaValidation:
    def test_scope_cannot_be_empty(self) -> None:
        """An empty scope means nothing defines when research is complete."""
        with pytest.raises(ValidationError):
            QuerySpec.model_validate_json(spec_json(scope=[]))

    def test_success_criteria_cannot_be_empty(self) -> None:
        with pytest.raises(ValidationError):
            QuerySpec.model_validate_json(spec_json(success_criteria=[]))

    def test_blank_padding_entries_are_rejected(self) -> None:
        """Models sometimes emit [""] to satisfy a minimum-length constraint.
        Accepting it would let an empty scope pass as a populated one."""
        with pytest.raises(ValidationError, match="only blank entries"):
            QuerySpec.model_validate_json(spec_json(scope=["", "   "]))

    def test_blank_entries_are_stripped_when_others_survive(self) -> None:
        spec = QuerySpec.model_validate_json(spec_json(scope=["architecture", "  ", ""]))
        assert spec.scope == ["architecture"]

    def test_unknown_research_type_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            QuerySpec.model_validate_json(spec_json(research_type="vibes"))

    def test_scope_is_bounded(self) -> None:
        """An unbounded scope would produce an unbounded plan."""
        with pytest.raises(ValidationError):
            QuerySpec.model_validate_json(spec_json(scope=[f"item {n}" for n in range(50)]))

    def test_optional_lists_default_to_empty(self) -> None:
        spec = QuerySpec.model_validate_json(
            json.dumps(
                {
                    "normalized_question": "How does Kafka guarantee message ordering?",
                    "research_type": "explanation",
                    "scope": ["partition ordering"],
                    "success_criteria": ["Ordering guarantees are described"],
                    "time_sensitivity": "static",
                    "requires_current_information": False,
                }
            )
        )
        assert spec.ambiguities == []
        assert spec.constraints == []


class TestAmbiguity:
    def test_ambiguity_carries_the_assumption_being_made(self) -> None:
        """Research proceeds on a stated assumption rather than blocking, so the
        report can say what was assumed."""
        spec = QuerySpec.model_validate_json(
            spec_json(
                ambiguities=[
                    {
                        "aspect": "scale",
                        "why_it_matters": "Throughput needs change the recommendation entirely",
                        "assumption": "tens of thousands of events per second",
                    }
                ]
            )
        )

        assert spec.is_ambiguous
        assert spec.ambiguities[0].assumption

    def test_ambiguity_without_an_assumption_is_rejected(self) -> None:
        """An ambiguity with no way forward would stall research."""
        with pytest.raises(ValidationError):
            Ambiguity(aspect="scale", why_it_matters="It changes everything", assumption="")

    def test_a_clear_question_has_no_ambiguities(self) -> None:
        assert QuerySpec.model_validate_json(spec_json()).is_ambiguous is False


class TestFreshness:
    @pytest.mark.parametrize(
        ("sensitivity", "current", "expected"),
        [
            ("volatile", False, True),
            ("static", True, True),
            ("evolving", True, True),
            ("static", False, False),
            ("evolving", False, False),
        ],
    )
    def test_freshness_requirement(self, sensitivity: str, current: bool, expected: bool) -> None:
        """Determines whether stale sources count as weak evidence later."""
        spec = QuerySpec.model_validate_json(
            spec_json(time_sensitivity=sensitivity, requires_current_information=current)
        )
        assert spec.freshness_required is expected

    def test_time_sensitivity_is_a_closed_set(self) -> None:
        assert {t.value for t in TimeSensitivity} == {"static", "evolving", "volatile"}


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------


class TestQueryAnalyzer:
    async def test_produces_a_valid_specification(self) -> None:
        analyzer, _ = make_analyzer(spec_json())
        spec = await analyzer.analyze("Compare Kafka and RabbitMQ")

        assert isinstance(spec, QuerySpec)
        assert spec.research_type is ResearchType.COMPARISON
        assert len(spec.scope) == 3

    async def test_empty_question_is_rejected_before_any_model_call(self) -> None:
        """Spending a call to discover the input was blank is wasteful."""
        analyzer, recorder = make_analyzer(spec_json())

        with pytest.raises(ValueError, match="must not be empty"):
            await analyzer.analyze("   ")

        assert recorder.agent_runs == []

    async def test_question_is_whitespace_normalised(self) -> None:
        analyzer, _ = make_analyzer(spec_json())
        await analyzer.analyze("  Compare Kafka and RabbitMQ  ")

    async def test_depth_reaches_the_prompt(self) -> None:
        analyzer, _ = make_analyzer(spec_json())
        await analyzer.analyze("Compare Kafka and RabbitMQ", depth=ResearchDepth.DEEP)

        rendered = analyzer.client.provider.requests[0].messages[1].content  # type: ignore[attr-defined]
        assert "deep" in rendered

    async def test_run_is_recorded_with_the_prompt_version(self) -> None:
        """Reproducibility depends on knowing which prompt actually ran."""
        analyzer, recorder = make_analyzer(spec_json())
        await analyzer.analyze("Compare Kafka and RabbitMQ", research_id="res_1")

        run = recorder.agent_runs[0]
        assert run.agent == "query_analyzer"
        assert run.research_id == "res_1"
        assert run.prompt_name == "query_analyzer"
        assert run.prompt_version == "v1"

    async def test_runs_on_the_cheap_tier(self) -> None:
        """Classification and extraction do not need a strong model, and this
        prompt runs on every research request."""
        analyzer, recorder = make_analyzer(spec_json())
        await analyzer.analyze("Compare Kafka and RabbitMQ")

        assert recorder.agent_runs[0].model == "gemini-3.5-flash-lite"

    async def test_malformed_output_is_repaired(self) -> None:
        analyzer, recorder = make_analyzer(spec_json(scope=[]), spec_json())
        spec = await analyzer.analyze("Compare Kafka and RabbitMQ")

        assert spec.scope
        assert [r.agent for r in recorder.agent_runs] == [
            "query_analyzer",
            "query_analyzer.repair",
        ]


class TestPromptContract:
    def test_prompt_declares_the_variables_it_uses(self) -> None:
        assert QUERY_ANALYZER_V1.variables == {"question", "depth"}

    def test_prompt_instructs_the_model_not_to_answer(self) -> None:
        assert "do not answer" in QUERY_ANALYZER_V1.system.lower()

    def test_prompt_permits_empty_lists(self) -> None:
        """Without this the model invents ambiguities in clear questions to
        avoid returning an empty list."""
        assert "empty list" in QUERY_ANALYZER_V1.system.lower()

    def test_prompt_is_registered_under_a_stable_id(self) -> None:
        assert QUERY_ANALYZER_V1.id == "query_analyzer.v1"


class TestSummary:
    def test_summary_is_loggable(self) -> None:
        summary = QuerySpec.model_validate_json(spec_json()).summary()
        assert "comparison" in summary
        assert "3 scope items" in summary
