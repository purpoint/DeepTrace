"""One definition of what a secret looks like, for everything that records.

Redaction started in the logging module, where it belongs -- a processor in the
chain rather than a call site that has to remember. But logs are not the only
place a run is written down. The trace is persisted to PostgreSQL, and it
carries the two fields where credentials actually turn up: the arguments a tool
was called with, and the message an exception produced.

Those are written by five recorder implementations. Adding redaction to each is
precisely the pattern the logging module rejected, so the patterns live here and
are applied in one place per concern: the structlog processor for logs, and the
trace record itself for the trace.

**Two layers, and the second is the one that matters.** Sensitive *field names*
catch what a caller deliberately passed. Secret *value shapes* catch credentials
wherever they appear -- inside a URL, inside an error message, inside a nested
dict nobody inspected. Leaks happen in the second kind, because the first kind
is the one people remember.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

SENSITIVE_KEY = re.compile(
    r"api[_-]?key|secret|token|password|passwd|authorization|credential|bearer|jwt|cookie",
    re.IGNORECASE,
)
"""Field names whose values must never be recorded.

Matched as substrings and case-insensitively, so ``openai_api_key`` and
``X-Api-Key`` are both caught.
"""

METRIC_KEYS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "prompt_tokens",
        "completion_tokens",
        "cached_tokens",
        "reasoning_tokens",
        "max_tokens",
        "max_output_tokens",
        "token_count",
        "token_limit",
        "tokens",
        "tokens_per_second",
    }
)
"""Fields containing "token" that are usage metrics, not credentials.

This allowlist exists because of a real bug: the pattern above matched
``input_tokens`` and redacted it, silently destroying the cost tracking sitting
next to it. A security control that quietly breaks an unrelated feature is worse
than one that fails loudly, and it is why the guard is tested in both
directions.
"""

SECRET_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),  # OpenAI
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),  # Anthropic
    re.compile(r"tvly-[A-Za-z0-9_\-]{16,}"),  # Tavily
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),  # GitHub
    re.compile(r"AIza[A-Za-z0-9_\-]{20,}"),  # Google
    re.compile(r"(?i)\b(?:postgres(?:ql)?|redis|amqp)://[^:\s]+:[^@\s]+@"),  # URL credentials
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{20,}"),  # a presented access token
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]+"),  # a JWT
)
"""Value shapes that are secrets regardless of the field they appear under.

The last two arrived with authentication. A rejected token reaches an exception
message as often as a rejected password does, and a JWT is recognisable on
sight: three base64url segments after an ``eyJ`` header. Recognising it by shape
is what catches the one that was logged by accident.
"""


def redact_value(value: Any) -> Any:
    """Recursively strip secret-shaped substrings from a value."""
    if isinstance(value, str):
        for pattern in SECRET_VALUE_PATTERNS:
            value = pattern.sub(REDACTED, value)
        return value
    if isinstance(value, dict):
        return redact_mapping(value)
    if isinstance(value, (list, tuple)):
        return type(value)(redact_value(item) for item in value)
    return value


def redact_mapping(mapping: dict[Any, Any]) -> dict[Any, Any]:
    """Redact by key name first, then scan surviving values for secret shapes."""
    result: dict[Any, Any] = {}
    for key, value in mapping.items():
        is_metric = isinstance(key, str) and key.lower() in METRIC_KEYS
        if not is_metric and isinstance(key, str) and SENSITIVE_KEY.search(key):
            result[key] = REDACTED
        else:
            result[key] = redact_value(value)
    return result


def redact_text(value: str | None) -> str | None:
    """Redact a single free-text field, preserving ``None``.

    ``None`` and ``"[REDACTED]"`` mean different things -- "there was no error"
    against "there was an error we will not show you" -- and collapsing them
    would make a successful call indistinguishable from a censored one.
    """
    if value is None:
        return None
    return str(redact_value(value))


__all__ = [
    "METRIC_KEYS",
    "REDACTED",
    "SECRET_VALUE_PATTERNS",
    "SENSITIVE_KEY",
    "redact_mapping",
    "redact_text",
    "redact_value",
]
