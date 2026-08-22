"""Typed errors for the LLM layer.

Every provider raises these rather than its own SDK's exceptions. That is what
lets retry policy, routing, and error handling be written once instead of once
per vendor.

The important attribute is :attr:`LLMError.retryable`. Retrying a rate limit is
correct; retrying an authentication failure burns time to arrive at the same
answer, and retrying a content-filter refusal will never succeed. Encoding this
on the error itself keeps the decision next to the knowledge, instead of in a
growing list of exception types at the call site.

:attr:`LLMError.transient` answers a different question, and the two are not the
same. ``retryable`` asks whether the client should try again *now*, inside its
own loop. ``transient`` asks whether the request could not be served at all, so
that trying again *later* is worth doing -- which is what decides whether a
workflow step is failed or merely owed.

A structured-output failure separates them. It is retryable, because the repair
loop can plausibly fix a malformed response on the next attempt; it is not
transient, because the service answered and waiting an hour will not make the
model's output fit a schema it does not fit. Treating it as transient would turn
a broken schema into a run that invites resuming forever.
"""

from __future__ import annotations


class LLMError(Exception):
    """Base class for every failure in the LLM layer.

    Args:
        message: Human-readable description, safe to show in logs.
        provider: Which provider produced the failure, if known.
        model: Which model was being called, if known.
        retryable: Whether retrying the same request could plausibly succeed.
    """

    retryable: bool = False

    transient: bool = False
    """Whether the request could not be served, rather than being served badly.

    Read by the workflow to tell an interrupted step from a failed one: a step
    stopped by an unavailable provider is still owed and resumes, while a step
    that produced an unusable result has failed and is recorded as such."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.model = model
        if retryable is not None:
            self.retryable = retryable

    def __str__(self) -> str:
        context = [part for part in (self.provider, self.model) if part]
        return f"{self.message} ({', '.join(context)})" if context else self.message


class LLMTimeoutError(LLMError):
    """The request exceeded its deadline.

    Retryable: a timeout is usually transient load, and DeepTrace bounds total
    attempts anyway.
    """

    retryable = True
    transient = True


class LLMRateLimitError(LLMError):
    """The provider rejected the request for rate or quota reasons.

    Carries ``retry_after`` when the provider supplies it, so backoff can honour
    the server's own guidance instead of guessing.
    """

    retryable = True
    transient = True

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, provider=provider, model=model)
        self.retry_after = retry_after


class LLMConnectionError(LLMError):
    """The provider could not be reached. Retryable."""

    retryable = True
    transient = True


class LLMServerError(LLMError):
    """The provider returned a 5xx response. Retryable."""

    retryable = True
    transient = True


class LLMAuthenticationError(LLMError):
    """The API key is missing, malformed, or rejected.

    Not retryable. The same key will be rejected again, so failing immediately
    produces a clearer message faster.
    """

    retryable = False


class LLMBadRequestError(LLMError):
    """The request itself is invalid: bad parameters, oversized context, or an
    unsupported schema. Not retryable without changing the request."""

    retryable = False


class LLMContentFilterError(LLMError):
    """The provider refused to generate a completion.

    Not retryable. In DeepTrace this is a signal to record the refusal and
    degrade gracefully, never to retry until something slips through.
    """

    retryable = False


class StructuredOutputError(LLMError):
    """The model returned output that does not satisfy the requested schema.

    Retryable, but only through the repair path: the raw output is fed back with
    the validation error so the model can correct it. Retrying the original
    request unchanged tends to reproduce the same malformed shape.
    """

    retryable = True

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        raw_output: str | None = None,
        validation_error: str | None = None,
    ) -> None:
        super().__init__(message, provider=provider, model=model)
        self.raw_output = raw_output
        self.validation_error = validation_error


class LLMBudgetExceededError(LLMError):
    """A research run reached its token or cost ceiling.

    Not retryable. This is the budget working as designed, not a fault, and it
    must stop the run rather than trigger another attempt.
    """

    retryable = False


class ProviderNotConfiguredError(LLMError):
    """A provider was requested but has no credentials configured."""

    retryable = False

    def __init__(self, provider: str) -> None:
        super().__init__(
            f"Provider '{provider}' is not configured. "
            f"Add {provider.upper()}_API_KEY to your .env file (see .env.example).",
            provider=provider,
        )


class UnknownProviderError(LLMError):
    """A provider id was requested that has no registered implementation."""

    retryable = False

    def __init__(self, provider: str, available: tuple[str, ...]) -> None:
        super().__init__(
            f"Unknown provider '{provider}'. Available providers: {', '.join(available)}.",
            provider=provider,
        )
