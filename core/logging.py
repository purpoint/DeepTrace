"""Structured logging configured once for the whole application.

DeepTrace logs events, not sentences. Every record is a dict with named fields,
so a research run can be reconstructed by filtering on ``research_id`` rather
than by grepping prose. This is the foundation the trace and observability
requirements build on.

Two properties matter more than formatting:

*Context propagates automatically.* Binding ``research_id`` once at the start of
a run attaches it to every subsequent log line on that task, including lines
emitted deep inside an agent or tool that was never passed the id.

*Secrets never reach a log sink.* Retrieved content and configuration both flow
through logs, so redaction happens in the processor chain rather than relying on
every call site to remember.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog
from structlog.types import EventDict, Processor

from core.config import Settings, get_settings

REDACTED = "[REDACTED]"

# Field names whose values must never be logged, matched case-insensitively as
# substrings so `openai_api_key` and `X-Api-Key` are both caught.
_SENSITIVE_KEY = re.compile(
    r"api[_-]?key|secret|token|password|passwd|authorization|credential|bearer|jwt|cookie",
    re.IGNORECASE,
)

# Fields that contain the substring "token" but are usage metrics, not secrets.
# Without this exemption the pattern above redacts token counts, which would
# silently destroy the cost tracking that observability depends on. Checked
# before _SENSITIVE_KEY, so the allowlist wins.
_METRIC_KEYS = frozenset(
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

# Value shapes that are secrets regardless of the field they appear under. These
# catch credentials embedded in error messages, URLs, and retrieved content,
# which is where accidental leaks actually happen.
_SECRET_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),  # OpenAI
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),  # Anthropic
    re.compile(r"tvly-[A-Za-z0-9_\-]{16,}"),  # Tavily
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),  # GitHub
    re.compile(r"AIza[A-Za-z0-9_\-]{20,}"),  # Google
    re.compile(r"(?i)\b(?:postgres(?:ql)?|redis|amqp)://[^:\s]+:[^@\s]+@"),  # URL credentials
)


def _redact_value(value: Any) -> Any:
    """Recursively strip secret-shaped substrings from a logged value."""
    if isinstance(value, str):
        for pattern in _SECRET_VALUE_PATTERNS:
            value = pattern.sub(REDACTED, value)
        return value
    if isinstance(value, dict):
        return _redact_mapping(value)
    if isinstance(value, (list, tuple)):
        return type(value)(_redact_value(item) for item in value)
    return value


def _redact_mapping(mapping: dict[Any, Any]) -> dict[Any, Any]:
    """Redact by key name first, then scan surviving values for secret shapes."""
    result: dict[Any, Any] = {}
    for key, value in mapping.items():
        is_metric = isinstance(key, str) and key.lower() in _METRIC_KEYS
        if not is_metric and isinstance(key, str) and _SENSITIVE_KEY.search(key):
            result[key] = REDACTED
        else:
            result[key] = _redact_value(value)
    return result


def redact_secrets(_logger: Any, _method: str, event_dict: EventDict) -> EventDict:
    """structlog processor that removes credentials from every record.

    Placed in the chain rather than left to call sites, because the leak that
    matters is the one nobody remembered to guard -- an exception string
    containing a request URL, or a config dump during debugging.
    """
    return _redact_mapping(dict(event_dict))


def _drop_color_message(_logger: Any, _method: str, event_dict: EventDict) -> EventDict:
    """Remove uvicorn's duplicate pre-coloured message field."""
    event_dict.pop("color_message", None)
    return event_dict


def build_processors(*, json_logs: bool) -> list[Processor]:
    """Assemble the processor chain.

    Order is significant. Context is merged first so bound fields participate in
    redaction, and rendering happens last.
    """
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _drop_color_message,
        redact_secrets,
    ]

    if json_logs:
        # Machine-readable for log aggregation in deployed environments.
        shared.append(structlog.processors.EventRenamer("message"))
        shared.append(structlog.processors.JSONRenderer())
    else:
        # Human-readable during development. Same fields, easier to scan.
        shared.append(structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()))

    return shared


def configure_logging(settings: Settings | None = None) -> None:
    """Configure structlog and the stdlib root logger.

    Safe to call more than once; later calls replace the configuration rather
    than stacking handlers.
    """
    settings = settings or get_settings()

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=settings.log_level,
        force=True,
    )

    structlog.configure(
        processors=build_processors(json_logs=settings.json_logs),
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[settings.log_level]
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Third-party libraries are noisy at INFO and drown out research events.
    for noisy in ("httpx", "httpcore", "urllib3", "openai", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a logger. Prefer ``get_logger(__name__)`` so records carry origin."""
    return structlog.stdlib.get_logger(name)


def bind_research_context(**fields: Any) -> None:
    """Bind fields to every subsequent log record in this execution context.

    Called once when a research run starts. Because it uses context variables,
    the bound ``research_id`` reaches agents and tools that were never handed
    it, and concurrent research runs on the same worker stay isolated.
    """
    structlog.contextvars.bind_contextvars(**fields)


def clear_research_context() -> None:
    """Clear bound context. Called when a run ends so ids never leak across runs."""
    structlog.contextvars.clear_contextvars()
