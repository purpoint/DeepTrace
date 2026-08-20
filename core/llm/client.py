"""The client agents actually call.

Everything the provider interface deliberately excludes lives here, written
once and applied to every vendor: model routing, retries, structured-output
validation and repair, cost estimation, and run recording.

An agent's entire interaction with a language model is one of two calls:

    result = await client.complete(prompt, {"question": q}, agent="planner")
    plan   = await client.complete_structured(prompt, ResearchPlan, {"question": q})

Neither mentions a vendor, a model name, a retry count, or a price.

Prompt variables are passed as an explicit dict rather than **kwargs. Sharing a
keyword namespace with the method's own parameters would mean a prompt variable
named ``agent`` or ``task_id`` silently hijacked one of them -- a collision that
would be very hard to diagnose from the resulting behaviour.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from core.config import Settings, get_settings
from core.llm.base import (
    CompletionRequest,
    CompletionResult,
    LLMProvider,
    Message,
    ModelTier,
)
from core.llm.errors import (
    LLMError,
    ProviderNotConfiguredError,
    StructuredOutputError,
    UnknownProviderError,
)
from core.llm.pricing import estimate_cost
from core.llm.retry import DEFAULT_POLICY, RetryPolicy, with_retries
from core.logging import get_logger
from core.observability.recorder import AgentRun, NullRunRecorder, RunRecorder, new_run_id
from core.prompts.registry import Prompt

log = get_logger(__name__)

TModel = TypeVar("TModel", bound=BaseModel)

_JSON_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


@dataclass(frozen=True, slots=True)
class ModelRouter:
    """Resolves a capability tier to a concrete provider and model.

    This is where "no agent knows which vendor serves it" is implemented. An
    agent asks for CHEAP or STRONG; the mapping to a vendor and a model name is
    configuration, so routing can change without touching agent code -- and
    different tiers may resolve to different vendors in the same run.
    """

    provider_id: str
    cheap_model: str
    strong_model: str
    embed_model: str

    @classmethod
    def from_settings(cls, settings: Settings) -> ModelRouter:
        return cls(
            provider_id=settings.llm_provider,
            cheap_model=settings.llm_model_cheap,
            strong_model=settings.llm_model_strong,
            embed_model=settings.llm_model_embed,
        )

    def model_for(self, tier: ModelTier) -> str:
        return {
            ModelTier.CHEAP: self.cheap_model,
            ModelTier.STRONG: self.strong_model,
            ModelTier.EMBED: self.embed_model,
        }[tier]


def build_provider(settings: Settings | None = None, provider_id: str | None = None) -> LLMProvider:
    """Construct a provider from configuration.

    Adding a vendor means adding a branch here and a module implementing the
    protocol. Nothing else in the codebase changes, which is the concrete test
    of whether the abstraction is real.
    """
    settings = settings or get_settings()
    provider_id = provider_id or settings.llm_provider
    available = ("google",)

    if provider_id == "google":
        from core.llm.gemini import GeminiProvider

        if not settings.google_api_key:
            raise ProviderNotConfiguredError("google")
        return GeminiProvider(api_key=settings.google_api_key)

    # Additional vendors slot in here as `if provider_id == "openai": ...`
    # paired with a module implementing LLMProvider. Nothing else in the
    # codebase changes, which is the concrete test of the abstraction. Only
    # providers with a tested adapter are listed; an untested branch would
    # promise a capability that does not exist.
    raise UnknownProviderError(provider_id, available)


def _strip_code_fence(text: str) -> str:
    """Remove a markdown code fence some models wrap JSON in.

    Not a correctness fix so much as a cheap one: rejecting an otherwise valid
    response over three backticks would spend a repair call to remove them.
    """
    match = _JSON_FENCE.match(text)
    return match.group(1) if match else text.strip()


class LLMClient:
    """Vendor-neutral entry point for every model call in DeepTrace."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        router: ModelRouter,
        recorder: RunRecorder | None = None,
        retry_policy: RetryPolicy = DEFAULT_POLICY,
        default_timeout: float = 60.0,
        max_repair_attempts: int = 2,
    ) -> None:
        self.provider = provider
        self.router = router
        self.recorder = recorder or NullRunRecorder()
        self.retry_policy = retry_policy
        self.default_timeout = default_timeout
        self.max_repair_attempts = max_repair_attempts

    @classmethod
    def from_settings(
        cls, settings: Settings | None = None, *, recorder: RunRecorder | None = None
    ) -> LLMClient:
        settings = settings or get_settings()
        return cls(
            provider=build_provider(settings),
            router=ModelRouter.from_settings(settings),
            recorder=recorder,
            retry_policy=RetryPolicy(max_attempts=settings.llm_max_retries),
            default_timeout=settings.llm_timeout_seconds,
        )

    # -- recording ---------------------------------------------------------

    def _record(
        self,
        *,
        prompt: Prompt,
        tier: ModelTier,
        model: str,
        agent: str,
        result: CompletionResult | None,
        started_at: datetime,
        status: str,
        error: Exception | None = None,
        retry_count: int = 0,
        research_id: str | None = None,
        task_id: str | None = None,
        parent_run_id: str | None = None,
        run_id: str | None = None,
    ) -> str:
        """Write one AgentRun. Returns its id so a repair can be linked to it."""
        usage = result.usage if result else None
        cost = estimate_cost(model, usage) if usage else None
        record = AgentRun(
            run_id=run_id or new_run_id("run"),
            agent=agent,
            provider=self.provider.name,
            model=model,
            tier=tier.value,
            prompt_name=prompt.name,
            prompt_version=prompt.version,
            research_id=research_id,
            task_id=task_id,
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            cached_tokens=usage.cached_tokens if usage else 0,
            latency_ms=result.latency_ms if result else 0.0,
            cost_usd=cost,
            status=status,
            error_type=type(error).__name__ if error else None,
            error_message=str(error) if error else None,
            retry_count=retry_count,
            finish_reason=result.finish_reason if result else None,
            parent_run_id=parent_run_id,
            started_at=started_at,
            completed_at=datetime.now(UTC),
        )
        self.recorder.record_agent_run(record)
        return record.run_id

    # -- completion --------------------------------------------------------

    async def complete(
        self,
        prompt: Prompt,
        variables: dict[str, object] | None = None,
        *,
        agent: str = "unknown",
        tier: ModelTier | None = None,
        research_id: str | None = None,
        task_id: str | None = None,
        response_schema: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.0,
        parent_run_id: str | None = None,
    ) -> CompletionResult:
        """Render a prompt, call the model, record the run, return the result.

        The tier defaults to the prompt's declared tier, so routing is a property
        of the prompt rather than something each call site repeats and
        occasionally gets wrong.
        """
        tier = tier or prompt.tier
        model = self.router.model_for(tier)
        messages = prompt.render(**(variables or {}))
        started_at = datetime.now(UTC)
        retries = 0

        request = CompletionRequest(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_seconds=self.default_timeout,
            response_schema=response_schema,
            schema_name=prompt.name,
        )

        def count_retry(attempt: int, _error: LLMError, _delay: float) -> None:
            nonlocal retries
            retries = attempt

        try:
            result = await with_retries(
                lambda: self.provider.complete(request),
                policy=self.retry_policy,
                operation_name=f"llm.{prompt.name}",
                on_retry=count_retry,
            )
        except LLMError as exc:
            self._record(
                prompt=prompt,
                tier=tier,
                model=model,
                agent=agent,
                result=None,
                started_at=started_at,
                status="error",
                error=exc,
                retry_count=retries,
                research_id=research_id,
                task_id=task_id,
                parent_run_id=parent_run_id,
            )
            raise

        run_id = self._record(
            prompt=prompt,
            tier=tier,
            model=model,
            agent=agent,
            result=result,
            started_at=started_at,
            status="success",
            retry_count=retries,
            research_id=research_id,
            task_id=task_id,
            parent_run_id=parent_run_id,
        )
        # Carry the run id on the result so a follow-up call -- a structured
        # output repair -- can record itself as a child of this run.
        result = replace(result, metadata={**result.metadata, "run_id": run_id})

        log.info(
            "llm.completed",
            agent=agent,
            prompt=prompt.id,
            model=model,
            tier=tier.value,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            latency_ms=round(result.latency_ms, 1),
            retry_count=retries,
            run_id=run_id,
        )
        return result

    async def complete_structured(
        self,
        prompt: Prompt,
        schema: type[TModel],
        variables: dict[str, object] | None = None,
        *,
        agent: str = "unknown",
        tier: ModelTier | None = None,
        research_id: str | None = None,
        task_id: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.0,
    ) -> TModel:
        """Call the model and validate the response against a Pydantic schema.

        On a validation failure the raw output and the specific validation error
        are fed back so the model can correct its own mistake. That is materially
        more effective than retrying the identical request, which tends to
        reproduce the identical malformed shape.

        Repair attempts are recorded as separate runs linked by
        ``parent_run_id``, so the cost of malformed output is visible rather than
        hidden inside a successful call.
        """
        tier = tier or prompt.tier
        json_schema = schema.model_json_schema()
        native = self.provider.supports_structured_output(self.router.model_for(tier))

        result = await self.complete(
            prompt,
            variables,
            agent=agent,
            tier=tier,
            research_id=research_id,
            task_id=task_id,
            response_schema=json_schema if native else None,
            max_tokens=max_tokens,
            temperature=temperature,
        )

        parent_run_id = str(result.metadata.get("run_id") or "") or None
        last_error: str = ""
        raw = result.text
        for attempt in range(self.max_repair_attempts + 1):
            try:
                return schema.model_validate_json(_strip_code_fence(raw))
            except (ValidationError, json.JSONDecodeError) as exc:
                last_error = str(exc)
                if attempt == self.max_repair_attempts:
                    break
                log.warning(
                    "llm.structured_output_invalid",
                    agent=agent,
                    prompt=prompt.id,
                    schema=schema.__name__,
                    repair_attempt=attempt + 1,
                    truncated=result.was_truncated,
                )
                raw = await self._repair(
                    prompt=prompt,
                    schema=schema,
                    json_schema=json_schema,
                    invalid_output=raw,
                    validation_error=last_error,
                    agent=agent,
                    tier=tier,
                    research_id=research_id,
                    task_id=task_id,
                    parent_run_id=parent_run_id,
                )

        raise StructuredOutputError(
            f"Model output failed validation against {schema.__name__} "
            f"after {self.max_repair_attempts} repair attempts",
            provider=self.provider.name,
            model=self.router.model_for(tier),
            raw_output=raw[:2000],
            validation_error=last_error,
        )

    async def _repair(
        self,
        *,
        prompt: Prompt,
        schema: type[TModel],
        json_schema: dict[str, Any],
        invalid_output: str,
        validation_error: str,
        agent: str,
        tier: ModelTier,
        research_id: str | None,
        task_id: str | None,
        parent_run_id: str | None,
    ) -> str:
        """Ask the model to correct its own malformed output.

        Uses a minimal repair conversation rather than the original prompt: the
        task is now "fix this JSON", not "do the research again", and resending
        the full original context would cost far more input tokens for a
        mechanical correction.
        """
        model = self.router.model_for(tier)
        started_at = datetime.now(UTC)
        messages = (
            Message.system(
                "You correct malformed JSON so that it satisfies a schema. "
                "Return only the corrected JSON object, with no commentary and "
                "no code fences. Preserve the original content wherever it is "
                "valid; do not invent new values to satisfy required fields."
            ),
            Message.user(
                f"Target schema:\n{json.dumps(json_schema, indent=2)}\n\n"
                f"Invalid output:\n{invalid_output}\n\n"
                f"Validation error:\n{validation_error}"
            ),
        )
        request = CompletionRequest(
            messages=messages,
            model=model,
            temperature=0.0,
            timeout_seconds=self.default_timeout,
            response_schema=json_schema
            if self.provider.supports_structured_output(model)
            else None,
            schema_name=schema.__name__,
        )

        try:
            result = await with_retries(
                lambda: self.provider.complete(request),
                policy=self.retry_policy,
                operation_name=f"llm.{prompt.name}.repair",
            )
        except LLMError as exc:
            self._record(
                prompt=prompt,
                tier=tier,
                model=model,
                agent=agent,
                result=None,
                started_at=started_at,
                status="error",
                error=exc,
                research_id=research_id,
                task_id=task_id,
                parent_run_id=parent_run_id,
            )
            raise

        self._record(
            prompt=prompt,
            tier=tier,
            model=model,
            agent=f"{agent}.repair",
            result=result,
            started_at=started_at,
            status="success",
            research_id=research_id,
            task_id=task_id,
            parent_run_id=parent_run_id,
        )
        return result.text
