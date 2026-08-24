"""Tests for distributed tracing.

Three properties, and the first two are about tracing staying out of the way.

It must cost nothing when nobody is collecting, because that is the state this
project runs in almost always. It must never change behaviour, because
observability that can break the thing it observes is worse than none. And it
must cross a process boundary, because the run being observed spans two.

The third is the one that needed a bug fixed: instrumentation that emits spans
is not the same as instrumentation that produces a *trace*, and the difference
is invisible until you count the roots.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from core.config import Settings
from core.observability.tracing import (
    carrier_for_current_span,
    configure_tracing,
    continue_trace,
    current_trace_id,
    span,
)

# One provider for the whole module, installed once.
#
# OpenTelemetry deliberately refuses to replace a tracer provider -- a global
# that can be swapped mid-process is a global that silently loses spans. So the
# provider is installed once and the *exporter* is cleared between tests, which
# is also closer to how the application behaves: configure_tracing is
# idempotent for exactly this reason.
_EXPORTER = InMemorySpanExporter()
_PROVIDER = TracerProvider(resource=Resource.create({"service.name": "test"}))
_PROVIDER.add_span_processor(SimpleSpanProcessor(_EXPORTER))
trace.set_tracer_provider(_PROVIDER)


@pytest.fixture
def spans() -> InMemorySpanExporter:
    """The recorded spans, empty at the start of each test.

    The real SDK rather than a mock, because what is under test is whether
    spans *nest* -- and a mock would record which calls were made while saying
    nothing about the tree they produce.
    """
    _EXPORTER.clear()
    return _EXPORTER


@pytest.fixture(autouse=True)
def _drain_spans() -> Iterator[None]:
    """Discard spans after every test in this module.

    Installing a provider is process-wide, so the rest of the suite emits into
    the same exporter. Left alone it would accumulate every span from every
    test for the whole session -- harmless in size, but it would mean a tracing
    test could see another module's spans if a clear were ever missed.
    """
    yield
    _EXPORTER.clear()


class TestCostingNothingWhenUnconfigured:
    def test_the_default_is_no_exporter(self) -> None:
        """A laptop runs this project most of the time, and should not need a
        collector for it to start."""
        assert Settings(_env_file=None).otel_exporter == "none"  # type: ignore[call-arg]

    def test_configuring_none_installs_nothing(self) -> None:
        assert configure_tracing(Settings(_env_file=None, otel_exporter="none")) is False  # type: ignore[call-arg]

    def test_an_unknown_exporter_is_refused_rather_than_guessed(self) -> None:
        assert configure_tracing(Settings(_env_file=None, otel_exporter="nonsense")) is False  # type: ignore[call-arg]

    def test_a_span_still_works_with_nothing_collecting(self) -> None:
        """The API's no-op implementation. Starting a span has to be safe
        whether or not anyone installed a provider, or every call site needs a
        conditional."""
        with span("probe", key="value"):
            pass  # must not raise


class TestNeverChangingBehaviour:
    def test_an_exception_escapes_the_span(self, spans: InMemorySpanExporter) -> None:
        """Recorded and re-raised. Swallowing here would make adding tracing
        change what the program does, which is the one thing it must not do."""
        with pytest.raises(ValueError, match="boom"), span("failing"):
            raise ValueError("boom")

        recorded = spans.get_finished_spans()
        assert recorded[0].status.status_code is trace.StatusCode.ERROR

    def test_null_attributes_are_dropped_not_stringified(self, spans: InMemorySpanExporter) -> None:
        """OpenTelemetry rejects a null attribute value, and writing the string
        "None" puts something in the trace that looks like data."""
        with span("probe", present="yes", absent=None):
            pass

        attributes = spans.get_finished_spans()[0].attributes or {}
        assert attributes.get("present") == "yes"
        assert "absent" not in attributes


class TestNesting:
    def test_spans_nest(self, spans: InMemorySpanExporter) -> None:
        """The property the whole feature rests on. A flat list of records
        sorted by start time cannot show that nine searches queued behind one
        rate limiter; a tree can."""
        with span("outer"), span("inner"):
            pass

        recorded = {s.name: s for s in spans.get_finished_spans()}
        assert recorded["inner"].parent is not None
        assert recorded["inner"].parent.span_id == recorded["outer"].context.span_id

    def test_one_run_produces_one_trace(self, spans: InMemorySpanExporter) -> None:
        """This is the bug that was actually shipped and caught by counting.

        Every node was instrumented and every span was emitted, so the feature
        looked complete -- but with no run-level span each node was the root of
        its own trace, and ten unrelated traces answer none of the questions a
        waterfall exists for.
        """
        with span("graph.run"):
            for name in ("graph.analyze", "graph.plan", "graph.report"):
                with span(name):
                    pass

        recorded = spans.get_finished_spans()
        assert len({s.context.trace_id for s in recorded}) == 1
        assert len([s for s in recorded if s.parent is None]) == 1


class TestCrossingAProcessBoundary:
    def test_a_carrier_resumes_the_same_trace(self, spans: InMemorySpanExporter) -> None:
        """A question is submitted by the API and executed minutes later by a
        worker that may be on another machine. Traced naively that is two
        unrelated traces, and "why did this take nine minutes" spans both."""
        with span("api.submit"):
            submitted = current_trace_id()
            carrier = carrier_for_current_span()

        assert "traceparent" in carrier

        with continue_trace(carrier, "worker.execute"):
            resumed = current_trace_id()

        assert resumed == submitted

    def test_an_absent_carrier_starts_a_new_trace(self, spans: InMemorySpanExporter) -> None:
        """A job queued by the CLI, or by a version of the API that predates
        this, has no carrier. That is not an error."""
        with continue_trace(None, "worker.execute"):
            assert current_trace_id() is not None

    def test_a_malformed_carrier_does_not_raise(self, spans: InMemorySpanExporter) -> None:
        with continue_trace({"traceparent": "not-a-traceparent"}, "worker.execute"):
            pass  # must not raise

    def test_there_is_no_trace_id_outside_a_span(self) -> None:
        """Absent rather than zeroed.

        A log field carrying all-zeros looks like a trace id that could be
        looked up, and cannot be. This is also what every log line looks like
        when no exporter is configured, which is the normal case.
        """
        assert current_trace_id() is None
