"""The provider interface every LLM vendor implements.

This module is the boundary that makes DeepTrace vendor-independent. Agents
depend on :class:`LLMProvider` and on the plain dataclasses in this file; they
never import a vendor SDK, never see a vendor's response object, and never
branch on which company is serving a request.

Two consequences follow, and both are requirements rather than nice-to-haves:

*Adding a provider is additive.* A new vendor means a new module implementing
this protocol. No agent changes, because no agent knew the old vendor's name.

*Routing can cross vendors mid-run.* Because a tier resolves to a provider at
call time, evidence extraction can run on one company's cheap model while fact
checking runs on another's strongest model, in the same research run.

The interface is deliberately small. A provider translates DeepTrace's request
into its vendor's API, translates the response back, and maps vendor exceptions
onto :mod:`core.llm.errors`. Everything else -- retries, cost accounting,
schema validation, run recording -- lives above it so it is written once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class ModelTier(StrEnum):
    """Capability tier a call requires, resolved to a concrete model by routing.

    Agents request a tier, not a model name. This keeps the cost/capability
    trade-off in configuration where it can be retuned, and means an agent's
    code does not change when a better or cheaper model appears.
    """

    CHEAP = "cheap"
    """Classification, query generation, extraction. High volume, low reasoning."""

    STRONG = "strong"
    """Analysis, fact checking, synthesis. Lower volume, high reasoning."""

    EMBED = "embed"
    """Embeddings for evidence retrieval and deduplication."""


class Role(StrEnum):
    """Message author. Deliberately closed -- retrieved web content is never a
    role, it is user-turn data clearly marked as untrusted."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class Message:
    """A single conversation turn.

    Frozen because a request that is retried must be byte-identical to the one
    that failed; a mutable message could be altered between attempts and make a
    failure impossible to reproduce.
    """

    role: Role
    content: str

    @classmethod
    def system(cls, content: str) -> Message:
        return cls(role=Role.SYSTEM, content=content)

    @classmethod
    def user(cls, content: str) -> Message:
        return cls(role=Role.USER, content=content)

    @classmethod
    def assistant(cls, content: str) -> Message:
        return cls(role=Role.ASSISTANT, content=content)

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role.value, "content": self.content}


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Token counts for one call.

    ``cached_tokens`` is tracked separately because cached input is billed at a
    reduced rate by several providers. Folding it into ``input_tokens`` would
    silently overstate cost.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def billable_input_tokens(self) -> int:
        """Input tokens charged at full rate."""
        return max(self.input_tokens - self.cached_tokens, 0)

    def __add__(self, other: TokenUsage) -> TokenUsage:
        """Accumulate usage across the calls in a research run."""
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
        )


@dataclass(frozen=True, slots=True)
class CompletionRequest:
    """Everything needed to make one completion call, vendor-neutral.

    Passed as a single object so a retry replays exactly the original request,
    and so a recorded run can be reproduced from what was stored.
    """

    messages: tuple[Message, ...]
    model: str
    temperature: float = 0.0
    max_tokens: int | None = None
    timeout_seconds: float = 60.0
    response_schema: dict[str, Any] | None = None
    """JSON Schema for structured output. The provider translates this into its
    vendor's mechanism; the caller above validates the result against Pydantic."""
    schema_name: str = "response"
    stop: tuple[str, ...] = ()
    seed: int | None = None
    """Requested determinism, where the provider supports it. Best effort."""


@dataclass(frozen=True, slots=True)
class CompletionResult:
    """A completion plus the metadata observability and cost tracking need.

    Providers must populate ``usage`` and ``latency_ms``. Without them the run
    recorder has nothing to record and cost cannot be computed, which is why
    they are required fields rather than optional extras.
    """

    text: str
    model: str
    provider: str
    usage: TokenUsage
    latency_ms: float
    finish_reason: str = "stop"
    response_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def was_truncated(self) -> bool:
        """True when the model stopped at the token limit rather than finishing.

        Truncated output is a common cause of malformed structured output, so it
        is worth distinguishing from a schema the model got wrong.
        """
        return self.finish_reason in ("length", "max_tokens")


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    """Embedding vectors plus usage, for cost accounting parity with completions."""

    vectors: tuple[tuple[float, ...], ...]
    model: str
    provider: str
    usage: TokenUsage
    latency_ms: float

    @property
    def dimensions(self) -> int:
        return len(self.vectors[0]) if self.vectors else 0


@runtime_checkable
class LLMProvider(Protocol):
    """What every vendor adapter must implement.

    A Protocol rather than an abstract base class: adapters do not inherit from
    DeepTrace types, which keeps them thin translation layers and makes test
    doubles trivial to write without importing production machinery.

    Implementations are responsible for exactly three things:

    1. Translate :class:`CompletionRequest` into the vendor's API call.
    2. Translate the vendor's response into :class:`CompletionResult`, including
       accurate token usage and measured latency.
    3. Map vendor exceptions onto :mod:`core.llm.errors`, setting ``retryable``
       correctly.

    They must not retry, log, compute cost, or validate schemas. Those belong
    above the interface so every provider gets identical behaviour.
    """

    @property
    def name(self) -> str:
        """Stable provider id, e.g. ``"openai"``. Recorded with every run."""
        ...

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        """Execute one completion. Raises subclasses of ``LLMError`` on failure."""
        ...

    async def embed(
        self, texts: tuple[str, ...], *, model: str, timeout_seconds: float = 60.0
    ) -> EmbeddingResult:
        """Embed one or more texts."""
        ...

    def supports_structured_output(self, model: str) -> bool:
        """Whether the model can be constrained to a JSON Schema natively.

        When False, the layer above falls back to prompt-level instruction plus
        validation and repair, which is less reliable and worth knowing about.
        """
        ...
