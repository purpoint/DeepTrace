"""Distributed tracing, off by default.

The run recorder already answers *what a run did*: every model call and tool
call, with its prompt version, tokens, latency and cost, in a table that
outlives the process. What it cannot answer is *what was happening at the same
time*. A research run fans out to a dozen concurrent tasks across two processes,
and a flat list of records sorted by start time cannot show that the analyst
waited four minutes because nine searches were queued behind one rate limiter.
Spans nest, so they can.

**Three properties this has to have.**

*It must cost nothing when nobody is collecting.* Tracing is for the days it is
needed, and this project runs on a laptop the rest of the time. With no exporter
configured the OpenTelemetry API is left with its default no-op implementation,
where starting a span is a function call that returns an object and does
nothing. No collector, no dependency on one running, no background thread.

*It must never break a run.* Observability that can fail the thing it observes
is worse than none, which is the same rule the run recorder follows -- recording
appends to a list that cannot block and cannot raise. A misconfigured endpoint
here degrades to no traces, never to a failed research run.

*It must cross the process boundary.* This is the part worth building. A
question is submitted by the API, queued in Redis, and executed minutes later by
a worker that may be on another machine. Traced naively that is two unrelated
traces, and the interesting question -- why did the run this user submitted take
nine minutes -- spans both. So the submitting span's context travels on the job,
and the worker continues the trace rather than starting one.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.propagate import extract, inject

from core.config import Settings, get_settings
from core.logging import get_logger

log = get_logger(__name__)

SERVICE_NAME = "deeptrace"

_configured = False


def configure_tracing(settings: Settings | None = None) -> bool:
    """Install a tracer provider if one is configured. Returns whether it did.

    Idempotent, because three entry points -- the API, the worker, and the CLI --
    each start a process that may call it, and installing two providers means
    every span is exported twice.

    A failure here is logged and swallowed. An operator who mistypes a collector
    endpoint should lose their traces, not their research.
    """
    global _configured
    if _configured:
        return True

    settings = settings or get_settings()
    exporter_kind = settings.otel_exporter.lower()
    if exporter_kind == "none":
        # The API's default provider is a no-op. Leaving it in place is what
        # makes an untraced run cost nothing rather than a little.
        return False

    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

        resource = Resource.create(
            {
                "service.name": settings.otel_service_name,
                "deployment.environment": settings.app_env.value,
            }
        )
        provider = TracerProvider(resource=resource)

        if exporter_kind == "console":
            exporter: Any = ConsoleSpanExporter()
        elif exporter_kind == "otlp":
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            exporter = OTLPSpanExporter(endpoint=settings.require("otel_endpoint"))
        else:
            log.error("tracing.unknown_exporter", exporter=exporter_kind)
            return False

        # Batched rather than simple: a span export on the critical path of a
        # research run would put a network call between two model calls.
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _configured = True
        log.info("tracing.configured", exporter=exporter_kind)
        return True
    except Exception as exc:
        log.error("tracing.setup_failed", error_type=type(exc).__name__, error=str(exc))
        return False


def get_tracer(name: str = SERVICE_NAME) -> trace.Tracer:
    """A tracer. Returns a working no-op one when tracing is not configured."""
    return trace.get_tracer(name)


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[trace.Span]:
    """Run a block inside a span, recording an exception if one escapes.

    Attributes with a value of ``None`` are dropped rather than stringified.
    OpenTelemetry rejects a null attribute value, and the alternative -- writing
    the string "None" -- puts a value in the trace that looks like data.
    """
    tracer = get_tracer()
    clean = {key: value for key, value in attributes.items() if value is not None}
    with tracer.start_as_current_span(name, attributes=clean) as current:
        try:
            yield current
        except Exception as exc:
            # Recorded on the span and re-raised. Swallowing here would make
            # tracing change behaviour, which is the one thing it must not do.
            current.record_exception(exc)
            current.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
            raise


def current_trace_id() -> str | None:
    """The active trace id as hex, for putting in a log line.

    This is what connects the two halves of an investigation: a log search finds
    the failing run, and its trace id opens the waterfall that explains it.
    Returns None when nothing is being traced, so a log field is absent rather
    than filled with zeros.
    """
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None
    return format(context.trace_id, "032x")


def carrier_for_current_span() -> dict[str, str]:
    """Serialise the active span's context, to be carried across a boundary.

    W3C ``traceparent``, which is a header format rather than anything specific
    to HTTP -- the two ends here are a queue producer and a queue consumer, and
    the format does not care.
    """
    carrier: dict[str, str] = {}
    inject(carrier)
    return carrier


@contextmanager
def continue_trace(
    carrier: Mapping[str, str] | None, name: str, **attributes: Any
) -> Iterator[trace.Span]:
    """Resume a trace that started in another process.

    Given the carrier a producer attached, the span opened here is a child of
    the one that submitted the work rather than the root of a second, unrelated
    trace. Without this, "why did this user's research take nine minutes" is a
    question that spans two traces and can be answered by neither.

    An absent or unparseable carrier is not an error -- a job queued before this
    existed has none -- so it simply starts a new trace.
    """
    token = None
    if carrier:
        try:
            token = otel_context.attach(extract(dict(carrier)))
        except Exception:  # pragma: no cover - a malformed carrier
            token = None

    try:
        with span(name, **attributes) as current:
            yield current
    finally:
        if token is not None:
            otel_context.detach(token)


def inject_into(carrier: MutableMapping[str, str]) -> None:
    """Attach the current span context to an outgoing carrier in place."""
    inject(carrier)


__all__ = [
    "SERVICE_NAME",
    "carrier_for_current_span",
    "configure_tracing",
    "continue_trace",
    "current_trace_id",
    "get_tracer",
    "inject_into",
    "span",
]
