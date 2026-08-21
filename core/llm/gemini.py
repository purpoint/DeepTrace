"""Google Gemini adapter.

A translation layer and nothing more. It converts a :class:`CompletionRequest`
into a Gemini API call, converts the response back into a
:class:`CompletionResult`, and maps Gemini's exceptions onto DeepTrace's typed
errors. It does not retry, log, price, or validate schemas -- those live above
the provider interface so every vendor gets identical behaviour.

Gemini is the default provider because its free tier supports native
JSON-schema-constrained output. Every DeepTrace agent returns structured data,
so a provider that can only be *asked* for JSON rather than constrained to it
means a repair loop on nearly every call.
"""

from __future__ import annotations

import time
from typing import Any

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from core.llm.base import (
    CompletionRequest,
    CompletionResult,
    EmbeddingResult,
    Message,
    Role,
    TokenUsage,
)
from core.llm.errors import (
    LLMAuthenticationError,
    LLMBadRequestError,
    LLMConnectionError,
    LLMContentFilterError,
    LLMError,
    LLMRateLimitError,
    LLMServerError,
    LLMTimeoutError,
)

PROVIDER_NAME = "google"

# Structured output is supported by the Gemini generative models generally, so
# capability is derived from the model family rather than an allowlist of
# version prefixes. A version allowlist silently rots: when the provider
# releases a new generation, every new model reports "unsupported" and the
# client quietly degrades to prompt-level JSON instruction plus repair loops,
# which costs money and is hard to notice.
_NON_GENERATIVE_MARKERS = ("embedding", "-tts", "-live", "veo-", "lyria-", "imagen")

# Gemini finish reasons that mean the model declined rather than completed.
_REFUSAL_REASONS = {"SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "RECITATION"}


# Gemini accepts a subset of OpenAPI 3.0 schema, not full JSON Schema. Anything
# outside this set is rejected outright rather than ignored.
_GEMINI_SCHEMA_KEYS = frozenset(
    {
        "type",
        "format",
        "description",
        "nullable",
        "enum",
        "items",
        "properties",
        "required",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "pattern",
        "anyOf",
    }
)


def to_gemini_schema(schema: dict[str, Any], definitions: dict[str, Any] | None = None) -> Any:
    """Translate a Pydantic JSON Schema into Gemini's schema dialect.

    Pydantic emits standard JSON Schema. Gemini accepts a restricted OpenAPI 3.0
    subset and rejects the request outright when it sees anything else, so two
    transformations are required:

    ``$ref``/``$defs`` are inlined. Pydantic hoists nested models and enums into
    a definitions block and references them; Gemini has no concept of either.

    Unsupported keywords are dropped. ``additionalProperties`` is the one that
    bites first, because ``extra="forbid"`` -- which is exactly what stops a
    model from inventing fields -- is what emits it.

    ``anyOf: [X, null]``, which is how Pydantic expresses an optional field,
    becomes Gemini's ``nullable`` flag rather than a two-branch union.

    This lives in the provider because it is a vendor quirk. The schema handed
    in is unchanged, and no caller needs to know Gemini is fussy.
    """
    if definitions is None:
        definitions = schema.get("$defs", {})

    if "$ref" in schema:
        name = str(schema["$ref"]).rsplit("/", 1)[-1]
        target = definitions.get(name, {})
        overrides = {key: value for key, value in schema.items() if key != "$ref"}
        return to_gemini_schema({**target, **overrides}, definitions)

    # Optional fields arrive as anyOf[X, null]; Gemini spells that `nullable`.
    variants = schema.get("anyOf")
    if isinstance(variants, list):
        non_null = [v for v in variants if v.get("type") != "null"]
        if len(non_null) == 1 and len(non_null) < len(variants):
            inner = to_gemini_schema(non_null[0], definitions)
            if isinstance(inner, dict):
                inner["nullable"] = True
            return inner

    result: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _GEMINI_SCHEMA_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            result[key] = {name: to_gemini_schema(sub, definitions) for name, sub in value.items()}
        elif key == "items" and isinstance(value, dict):
            result[key] = to_gemini_schema(value, definitions)
        elif key == "anyOf" and isinstance(value, list):
            result[key] = [to_gemini_schema(v, definitions) for v in value]
        else:
            result[key] = value

    return result


def _split_system(messages: tuple[Message, ...]) -> tuple[str | None, list[Any]]:
    """Separate the system instruction from conversation turns.

    Gemini takes the system prompt as a separate ``system_instruction`` rather
    than as a message with a system role, and names the assistant role
    ``model``. Both are vendor quirks that stop at this boundary.
    """
    system_parts: list[str] = []
    contents: list[Any] = []

    for message in messages:
        if message.role is Role.SYSTEM:
            system_parts.append(message.content)
            continue
        role = "model" if message.role is Role.ASSISTANT else "user"
        contents.append(
            genai_types.Content(role=role, parts=[genai_types.Part(text=message.content)])
        )

    return ("\n\n".join(system_parts) if system_parts else None, contents)


def _usage_from(response: Any) -> TokenUsage:
    """Read token counts off the response.

    Falls back to zeros rather than raising: a missing usage field should not
    fail a completion that otherwise succeeded, though it will show up as a
    suspiciously free call in the run log.
    """
    meta = getattr(response, "usage_metadata", None)
    if meta is None:
        return TokenUsage()
    return TokenUsage(
        input_tokens=getattr(meta, "prompt_token_count", 0) or 0,
        output_tokens=getattr(meta, "candidates_token_count", 0) or 0,
        cached_tokens=getattr(meta, "cached_content_token_count", 0) or 0,
        reasoning_tokens=getattr(meta, "thoughts_token_count", 0) or 0,
    )


def _finish_reason_of(response: Any) -> str:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return "unknown"
    reason = getattr(candidates[0], "finish_reason", None)
    if reason is None:
        return "stop"
    return getattr(reason, "name", str(reason))


def _text_of(response: Any) -> str:
    """Extract text, tolerating responses whose ``.text`` accessor raises.

    A blocked or empty candidate can make the convenience accessor throw, and a
    provider adapter must surface that as a typed DeepTrace error rather than an
    SDK-specific exception leaking upward.
    """
    try:
        text = response.text
    except Exception:
        text = None
    if text:
        return str(text)

    collected: list[str] = []
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            part_text = getattr(part, "text", None)
            if part_text:
                collected.append(str(part_text))
    return "".join(collected)


def _translate_error(exc: Exception, model: str) -> LLMError:
    """Map an SDK exception onto a typed DeepTrace error.

    The mapping decides whether the retry policy will try again, so
    classification matters more than the message. Anything unrecognised is
    treated as non-retryable: retrying an unknown failure risks repeating a
    request that already had a side effect or already cost money.
    """
    kwargs: dict[str, Any] = {"provider": PROVIDER_NAME, "model": model}

    if isinstance(exc, TimeoutError):
        return LLMTimeoutError("Gemini request timed out", **kwargs)

    if isinstance(exc, genai_errors.APIError):
        status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        message = getattr(exc, "message", None) or str(exc)

        if status == 429:
            return LLMRateLimitError(f"Gemini rate limit: {message}", **kwargs)
        if status in (401, 403):
            return LLMAuthenticationError(f"Gemini rejected the API key: {message}", **kwargs)
        if status == 400:
            return LLMBadRequestError(f"Gemini rejected the request: {message}", **kwargs)
        if status is not None and 500 <= int(status) < 600:
            return LLMServerError(f"Gemini server error {status}: {message}", **kwargs)
        return LLMError(f"Gemini error: {message}", **kwargs)

    if isinstance(exc, (ConnectionError, OSError)):
        return LLMConnectionError(f"Could not reach Gemini: {exc}", **kwargs)

    return LLMError(f"Unexpected Gemini failure: {exc}", **kwargs)


class GeminiProvider:
    """Implements :class:`~core.llm.base.LLMProvider` for Google Gemini."""

    def __init__(self, api_key: str, *, client: Any | None = None) -> None:
        """Args:
        api_key: Google AI Studio key.
        client: Injected client, used by tests to avoid network access.
        """
        self._client = client or genai.Client(api_key=api_key)

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    def supports_structured_output(self, model: str) -> bool:
        lowered = model.lower()
        if not lowered.startswith(("gemini-", "models/gemini-")):
            return False
        return not any(marker in lowered for marker in _NON_GENERATIVE_MARKERS)

    def _build_config(self, request: CompletionRequest, system: str | None) -> Any:
        config: dict[str, Any] = {
            "temperature": request.temperature,
            "http_options": genai_types.HttpOptions(
                timeout=int(request.timeout_seconds * 1000)  # SDK expects milliseconds
            ),
        }
        if system:
            config["system_instruction"] = system
        if request.max_tokens is not None:
            config["max_output_tokens"] = request.max_tokens
        if request.stop:
            config["stop_sequences"] = list(request.stop)
        if request.seed is not None:
            config["seed"] = request.seed
        if request.response_schema is not None and self.supports_structured_output(request.model):
            config["response_mime_type"] = "application/json"
            config["response_schema"] = to_gemini_schema(request.response_schema)

        return genai_types.GenerateContentConfig(**config)

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        system, contents = _split_system(request.messages)
        config = self._build_config(request, system)

        started = time.perf_counter()
        try:
            response = await self._client.aio.models.generate_content(
                model=request.model, contents=contents, config=config
            )
        except Exception as exc:
            raise _translate_error(exc, request.model) from exc
        latency_ms = (time.perf_counter() - started) * 1000

        finish_reason = _finish_reason_of(response)
        if finish_reason in _REFUSAL_REASONS:
            raise LLMContentFilterError(
                f"Gemini declined to answer (finish_reason={finish_reason})",
                provider=PROVIDER_NAME,
                model=request.model,
            )

        text = _text_of(response)
        if not text and finish_reason not in ("MAX_TOKENS", "length"):
            raise LLMError(
                f"Gemini returned no text (finish_reason={finish_reason})",
                provider=PROVIDER_NAME,
                model=request.model,
            )

        return CompletionResult(
            text=text,
            model=request.model,
            provider=PROVIDER_NAME,
            usage=_usage_from(response),
            latency_ms=latency_ms,
            finish_reason=finish_reason.lower(),
            response_id=getattr(response, "response_id", None),
            metadata={"structured": request.response_schema is not None},
        )

    async def embed(
        self, texts: tuple[str, ...], *, model: str, timeout_seconds: float = 60.0
    ) -> EmbeddingResult:
        started = time.perf_counter()
        try:
            response = await self._client.aio.models.embed_content(
                model=model,
                contents=list(texts),
                config=genai_types.EmbedContentConfig(
                    http_options=genai_types.HttpOptions(timeout=int(timeout_seconds * 1000))
                ),
            )
        except Exception as exc:
            raise _translate_error(exc, model) from exc
        latency_ms = (time.perf_counter() - started) * 1000

        vectors = tuple(
            tuple(float(value) for value in (embedding.values or ()))
            for embedding in (getattr(response, "embeddings", None) or ())
        )
        return EmbeddingResult(
            vectors=vectors,
            model=model,
            provider=PROVIDER_NAME,
            usage=_usage_from(response),
            latency_ms=latency_ms,
        )
