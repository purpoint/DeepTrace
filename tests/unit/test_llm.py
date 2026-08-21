"""Tests for the LLM layer.

Everything here runs against a fake provider. No network, no API key, no cost.
That is deliberate: the behaviour worth testing -- retry classification, repair
loops, cost recording, tier routing -- is the layer *above* the vendor, and
making it depend on a live API would make the suite slow, flaky, and expensive.

The fake provider also serves a second purpose. It is a complete implementation
of LLMProvider written without importing any vendor SDK, which demonstrates the
interface is genuinely implementable by more than one thing.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import BaseModel, Field

from core.llm.base import (
    CompletionRequest,
    CompletionResult,
    LLMProvider,
    Message,
    ModelTier,
    Role,
    TokenUsage,
)
from core.llm.client import LLMClient, ModelRouter, _strip_code_fence, build_provider
from core.llm.errors import (
    LLMAuthenticationError,
    LLMBadRequestError,
    LLMContentFilterError,
    LLMError,
    LLMRateLimitError,
    LLMServerError,
    LLMTimeoutError,
    ProviderNotConfiguredError,
    StructuredOutputError,
    UnknownProviderError,
)
from core.llm.pricing import estimate_cost
from core.llm.retry import RetryPolicy, with_retries
from core.observability.recorder import InMemoryRunRecorder
from core.prompts.registry import Prompt
from tests.fakes import FakeProvider

pytestmark = pytest.mark.unit


class ResearchPlan(BaseModel):
    objective: str
    tasks: list[str] = Field(min_length=1)


PLANNER = Prompt(
    name="planner",
    version="v1",
    system="You plan research.",
    user_template="Plan research for: $question",
    variables=frozenset({"question"}),
    tier=ModelTier.CHEAP,
)

ROUTER = ModelRouter(
    provider_id="fake",
    cheap_model="gemini-3.5-flash-lite",
    strong_model="gemini-3.7-flash",
    embed_model="gemini-embedding-001",
)

VALID_PLAN = '{"objective": "Compare Kafka and RabbitMQ", "tasks": ["architecture"]}'


def make_client(*responses: object, **kwargs: object) -> tuple[LLMClient, InMemoryRunRecorder]:
    recorder = InMemoryRunRecorder()
    client = LLMClient(
        FakeProvider(responses),
        router=ROUTER,
        recorder=recorder,
        retry_policy=RetryPolicy(max_attempts=3, initial_delay_seconds=0.001, jitter=0.0),
        **kwargs,  # type: ignore[arg-type]
    )
    return client, recorder


# ---------------------------------------------------------------------------
# Interface contracts
# ---------------------------------------------------------------------------


class TestProviderInterface:
    def test_fake_provider_satisfies_the_protocol(self) -> None:
        """A provider written without any vendor SDK still fits the interface,
        which is what makes the abstraction more than a claim."""
        assert isinstance(FakeProvider([VALID_PLAN]), LLMProvider)

    def test_messages_are_immutable(self) -> None:
        """A retried request must be byte-identical to the one that failed."""
        message = Message.user("hello")
        with pytest.raises(AttributeError):
            message.content = "changed"  # type: ignore[misc]

    def test_role_has_no_role_for_retrieved_content(self) -> None:
        """Web content cannot arrive as a system message because no such path
        exists in the type."""
        assert {r.value for r in Role} == {"system", "user", "assistant"}


class TestTokenUsage:
    def test_cached_tokens_reduce_billable_input(self) -> None:
        usage = TokenUsage(input_tokens=1000, output_tokens=200, cached_tokens=400)
        assert usage.billable_input_tokens == 600
        assert usage.total_tokens == 1200

    def test_cached_tokens_cannot_make_billable_negative(self) -> None:
        usage = TokenUsage(input_tokens=100, cached_tokens=500)
        assert usage.billable_input_tokens == 0

    def test_usage_accumulates(self) -> None:
        combined = TokenUsage(input_tokens=10, output_tokens=5) + TokenUsage(
            input_tokens=20, output_tokens=7
        )
        assert (combined.input_tokens, combined.output_tokens) == (30, 12)


class TestTruncationDetection:
    @pytest.mark.parametrize("reason", ["length", "max_tokens"])
    def test_truncated_finish_reasons(self, reason: str) -> None:
        """Truncation is a distinct cause of malformed structured output and is
        worth distinguishing from a schema the model simply got wrong."""
        result = CompletionResult(
            text="",
            model="m",
            provider="fake",
            usage=TokenUsage(),
            latency_ms=1.0,
            finish_reason=reason,
        )
        assert result.was_truncated is True

    def test_normal_stop_is_not_truncated(self) -> None:
        result = CompletionResult(
            text="ok", model="m", provider="fake", usage=TokenUsage(), latency_ms=1.0
        )
        assert result.was_truncated is False


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


class TestRetryClassification:
    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (LLMTimeoutError("t"), True),
            (LLMRateLimitError("r"), True),
            (LLMServerError("s"), True),
            (LLMAuthenticationError("a"), False),
            (LLMBadRequestError("b"), False),
            (LLMContentFilterError("c"), False),
        ],
    )
    def test_retryable_flag(self, error: LLMError, expected: bool) -> None:
        assert error.retryable is expected

    async def test_non_retryable_error_fails_immediately(self) -> None:
        """Retrying a rejected API key wastes time reaching the same answer."""
        provider = FakeProvider([LLMAuthenticationError("bad key")])
        with pytest.raises(LLMAuthenticationError):
            await with_retries(
                lambda: provider.complete(_request()),
                policy=RetryPolicy(max_attempts=5, initial_delay_seconds=0.001),
            )
        assert provider.calls == 1

    async def test_retryable_error_is_retried_then_succeeds(self) -> None:
        provider = FakeProvider([LLMRateLimitError("429"), VALID_PLAN])
        result = await with_retries(
            lambda: provider.complete(_request()),
            policy=RetryPolicy(max_attempts=3, initial_delay_seconds=0.001, jitter=0.0),
        )
        assert provider.calls == 2
        assert result.text == VALID_PLAN

    async def test_attempts_are_bounded(self) -> None:
        """An unbounded retry loop against a paid API spends money for nothing."""
        provider = FakeProvider([LLMServerError("500")] * 10)
        with pytest.raises(LLMServerError):
            await with_retries(
                lambda: provider.complete(_request()),
                policy=RetryPolicy(max_attempts=3, initial_delay_seconds=0.001, jitter=0.0),
            )
        assert provider.calls == 3

    async def test_original_error_is_raised_not_a_wrapper(self) -> None:
        """The caller must see the real failure, not "retries exhausted"."""
        provider = FakeProvider([LLMServerError("upstream exploded")] * 5)
        with pytest.raises(LLMServerError, match="upstream exploded"):
            await with_retries(
                lambda: provider.complete(_request()),
                policy=RetryPolicy(max_attempts=2, initial_delay_seconds=0.001),
            )


class TestBackoff:
    def test_delay_grows_exponentially(self) -> None:
        policy = RetryPolicy(initial_delay_seconds=1.0, backoff_multiplier=2.0, jitter=0.0)
        assert [policy.delay_for(n) for n in (1, 2, 3)] == [1.0, 2.0, 4.0]

    def test_delay_is_capped(self) -> None:
        policy = RetryPolicy(
            initial_delay_seconds=1.0, backoff_multiplier=10.0, max_delay_seconds=5.0, jitter=0.0
        )
        assert policy.delay_for(6) == 5.0

    def test_provider_retry_after_overrides_backoff(self) -> None:
        """The server knows its own limits better than the client does."""
        policy = RetryPolicy(initial_delay_seconds=1.0, jitter=0.0)
        assert policy.delay_for(3, retry_after=0.5) == 0.5

    def test_retry_after_is_still_clamped(self) -> None:
        """A provider asking for a very long wait must fail fast, not stall a run."""
        policy = RetryPolicy(max_delay_seconds=30.0, jitter=0.0)
        assert policy.delay_for(1, retry_after=600.0) == 30.0

    def test_jitter_spreads_concurrent_retries(self) -> None:
        """Without jitter, parallel tasks that hit a rate limit together retry
        together and re-trigger it."""
        policy = RetryPolicy(initial_delay_seconds=1.0, jitter=0.5)
        delays = {policy.delay_for(1) for _ in range(30)}
        assert len(delays) > 1
        assert all(0.5 <= d <= 1.5 for d in delays)

    def test_jitter_never_produces_a_negative_delay(self) -> None:
        policy = RetryPolicy(initial_delay_seconds=0.01, jitter=1.0)
        assert all(policy.delay_for(1) >= 0 for _ in range(50))

    async def test_total_delay_budget_stops_retrying(self) -> None:
        provider = FakeProvider([LLMServerError("500")] * 10)
        with pytest.raises(LLMServerError):
            await with_retries(
                lambda: provider.complete(_request()),
                policy=RetryPolicy(
                    max_attempts=10,
                    initial_delay_seconds=0.01,
                    max_total_delay_seconds=0.015,
                    jitter=0.0,
                ),
            )
        assert provider.calls < 10


# ---------------------------------------------------------------------------
# Structured output
# ---------------------------------------------------------------------------


class TestStructuredOutput:
    async def test_valid_response_parses_into_the_schema(self) -> None:
        client, _ = make_client(VALID_PLAN)
        plan = await client.complete_structured(PLANNER, ResearchPlan, {"question": "q"})
        assert isinstance(plan, ResearchPlan)
        assert plan.tasks == ["architecture"]

    async def test_invalid_output_is_repaired_not_resent(self) -> None:
        """Resending the same request reproduces the same malformed shape, so
        the invalid output and the validation error are fed back instead."""
        client, recorder = make_client('{"objective": "x"}', VALID_PLAN)
        plan = await client.complete_structured(PLANNER, ResearchPlan, {"question": "q"})

        assert plan.tasks == ["architecture"]
        assert [run.agent for run in recorder.agent_runs] == ["unknown", "unknown.repair"]

    async def test_repair_is_linked_to_the_run_it_repairs(self) -> None:
        """Repair calls cost money. Linking them is what makes the cost of
        malformed output measurable instead of an unexplained row."""
        client, recorder = make_client('{"objective": "x"}', VALID_PLAN)
        await client.complete_structured(PLANNER, ResearchPlan, {"question": "q"})

        original, repair = recorder.agent_runs
        assert original.parent_run_id is None
        assert repair.parent_run_id == original.run_id

    async def test_repair_attempts_are_bounded(self) -> None:
        client, _ = make_client(*['{"objective": "x"}'] * 6, max_repair_attempts=2)
        with pytest.raises(StructuredOutputError) as exc:
            await client.complete_structured(PLANNER, ResearchPlan, {"question": "q"})
        assert exc.value.validation_error

    async def test_failure_carries_raw_output_for_diagnosis(self) -> None:
        client, _ = make_client(*["not json at all"] * 6, max_repair_attempts=1)
        with pytest.raises(StructuredOutputError) as exc:
            await client.complete_structured(PLANNER, ResearchPlan, {"question": "q"})
        assert "not json" in (exc.value.raw_output or "")

    @pytest.mark.parametrize(
        "wrapped",
        [
            f"```json\n{VALID_PLAN}\n```",
            f"```\n{VALID_PLAN}\n```",
            f"  {VALID_PLAN}  ",
        ],
    )
    async def test_code_fences_do_not_cost_a_repair_call(self, wrapped: str) -> None:
        """Rejecting valid JSON over three backticks would spend a paid call to
        remove them."""
        client, recorder = make_client(wrapped)
        plan = await client.complete_structured(PLANNER, ResearchPlan, {"question": "q"})
        assert plan.objective
        assert len(recorder.agent_runs) == 1

    def test_strip_fence_leaves_ordinary_json_alone(self) -> None:
        assert _strip_code_fence(VALID_PLAN) == VALID_PLAN


# ---------------------------------------------------------------------------
# Routing, recording, and cost
# ---------------------------------------------------------------------------


class TestModelRouting:
    @pytest.mark.parametrize(
        ("tier", "expected"),
        [
            (ModelTier.CHEAP, "gemini-3.5-flash-lite"),
            (ModelTier.STRONG, "gemini-3.7-flash"),
            (ModelTier.EMBED, "gemini-embedding-001"),
        ],
    )
    def test_tier_resolves_to_configured_model(self, tier: ModelTier, expected: str) -> None:
        assert ROUTER.model_for(tier) == expected

    async def test_prompt_tier_is_the_default(self) -> None:
        """Routing is a property of the prompt, so call sites cannot drift."""
        client, recorder = make_client(VALID_PLAN)
        await client.complete_structured(PLANNER, ResearchPlan, {"question": "q"})
        assert recorder.agent_runs[0].model == "gemini-3.5-flash-lite"

    async def test_explicit_tier_overrides_the_prompt(self) -> None:
        client, recorder = make_client(VALID_PLAN)
        await client.complete_structured(
            PLANNER, ResearchPlan, {"question": "q"}, tier=ModelTier.STRONG
        )
        assert recorder.agent_runs[0].model == "gemini-3.7-flash"


class TestRunRecording:
    async def test_successful_call_records_everything_needed_to_explain_it(self) -> None:
        client, recorder = make_client(VALID_PLAN)
        await client.complete_structured(
            PLANNER, ResearchPlan, {"question": "q"}, agent="planner", research_id="res_1"
        )

        run = recorder.agent_runs[0]
        assert run.agent == "planner"
        assert run.research_id == "res_1"
        assert run.prompt_name == "planner"
        assert run.prompt_version == "v1"
        assert run.model == "gemini-3.5-flash-lite"
        assert run.tier == "cheap"
        assert run.input_tokens > 0
        assert run.latency_ms > 0
        assert run.status == "success"

    async def test_failed_call_is_recorded_with_its_error(self) -> None:
        """A failure that leaves no record cannot be diagnosed later."""
        client, recorder = make_client(LLMAuthenticationError("bad key"))
        with pytest.raises(LLMAuthenticationError):
            await client.complete(PLANNER, {"question": "q"}, agent="planner")

        run = recorder.agent_runs[0]
        assert run.status == "error"
        assert run.error_type == "LLMAuthenticationError"
        assert run.succeeded is False

    async def test_retry_count_is_recorded(self) -> None:
        client, recorder = make_client(LLMRateLimitError("429"), VALID_PLAN)
        await client.complete(PLANNER, {"question": "q"})
        assert recorder.agent_runs[0].retry_count == 1

    async def test_cost_is_computed_from_usage(self) -> None:
        """Uses a model that has a recorded price, since the point of this test
        is the arithmetic rather than the contents of the pricing table."""
        priced = "gpt-4o-mini"
        router = ModelRouter("fake", priced, priced, priced)
        recorder = InMemoryRunRecorder()
        client = LLMClient(FakeProvider([VALID_PLAN]), router=router, recorder=recorder)
        await client.complete(PLANNER, {"question": "q"})

        run = recorder.agent_runs[0]
        expected = estimate_cost(
            priced,
            TokenUsage(input_tokens=run.input_tokens, output_tokens=run.output_tokens),
        )
        assert run.cost_usd == expected
        assert isinstance(run.cost_usd, Decimal)

    async def test_unpriced_model_records_unknown_cost_not_zero(self) -> None:
        """A run showing $0.00 looks free; one showing unknown looks unmeasured."""
        router = ModelRouter("fake", "no-such-model", "no-such-model", "no-such-model")
        recorder = InMemoryRunRecorder()
        client = LLMClient(FakeProvider([VALID_PLAN]), router=router, recorder=recorder)
        await client.complete(PLANNER, {"question": "q"})

        assert recorder.agent_runs[0].cost_usd is None
        assert recorder.total_cost() is None


class TestPromptVariables:
    async def test_variables_do_not_collide_with_method_arguments(self) -> None:
        """A prompt variable named `agent` must not hijack the agent parameter.

        Regression test: prompt variables were once passed as **kwargs, sharing
        a keyword namespace with the method's own parameters.
        """
        prompt = Prompt(
            name="collide",
            version="v1",
            system="s",
            user_template="$agent and $task_id",
            variables=frozenset({"agent", "task_id"}),
        )
        client, recorder = make_client(VALID_PLAN)
        await client.complete(
            prompt, {"agent": "IN_TEMPLATE", "task_id": "ALSO_IN_TEMPLATE"}, agent="real_agent"
        )

        assert recorder.agent_runs[0].agent == "real_agent"


class TestProviderConstruction:
    def test_unknown_provider_lists_the_available_ones(self) -> None:
        from core.config import Settings

        with pytest.raises(UnknownProviderError, match="google"):
            build_provider(Settings(_env_file=None), provider_id="nonexistent")

    def test_missing_credentials_name_the_variable(self) -> None:
        from core.config import Settings

        with pytest.raises(ProviderNotConfiguredError, match="GOOGLE_API_KEY"):
            build_provider(Settings(_env_file=None), provider_id="google")


def _request() -> CompletionRequest:
    return CompletionRequest(messages=(Message.user("hi"),), model="gemini-3.5-flash-lite")
