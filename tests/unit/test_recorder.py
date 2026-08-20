"""Tests for run recording.

The governing rule is that recording must never break the thing it records, so
failure handling is tested as carefully as the happy path.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from core.observability.recorder import (
    AgentRun,
    InMemoryRunRecorder,
    JsonlRunRecorder,
    MultiRunRecorder,
    NullRunRecorder,
    RunRecorder,
    ToolCall,
    new_run_id,
)

pytestmark = pytest.mark.unit


def make_run(**kwargs: object) -> AgentRun:
    defaults: dict[str, object] = {
        "agent": "planner",
        "provider": "google",
        "model": "gemini-2.0-flash",
        "prompt_name": "planner",
        "prompt_version": "v1",
        "input_tokens": 900,
        "output_tokens": 120,
        "latency_ms": 42.0,
        "cost_usd": Decimal("0.000138"),
    }
    defaults.update(kwargs)
    return AgentRun(**defaults)  # type: ignore[arg-type]


class TestIdentifiers:
    def test_ids_are_unique(self) -> None:
        assert len({new_run_id() for _ in range(500)}) == 500

    def test_prefix_identifies_the_kind(self) -> None:
        assert new_run_id("tool").startswith("tool_")


class TestRecorderProtocol:
    @pytest.mark.parametrize(
        "recorder",
        [NullRunRecorder(), InMemoryRunRecorder(), MultiRunRecorder()],
    )
    def test_implementations_satisfy_the_protocol(self, recorder: object) -> None:
        assert isinstance(recorder, RunRecorder)

    def test_null_recorder_accepts_and_discards(self) -> None:
        """Lets library code call the recorder unconditionally instead of
        guarding every call site with a None check."""
        recorder = NullRunRecorder()
        recorder.record_agent_run(make_run())
        recorder.record_tool_call(ToolCall(tool="web_search"))


class TestInMemoryAggregation:
    def test_totals_accumulate(self) -> None:
        recorder = InMemoryRunRecorder()
        recorder.record_agent_run(make_run())
        recorder.record_agent_run(make_run())

        assert recorder.total_tokens() == 2040
        assert recorder.total_cost() == Decimal("0.000276")

    def test_one_unpriced_call_makes_the_total_unknown(self) -> None:
        """Summing only the priced calls would understate spend while looking
        authoritative."""
        recorder = InMemoryRunRecorder()
        recorder.record_agent_run(make_run())
        recorder.record_agent_run(make_run(cost_usd=None))

        assert recorder.total_cost() is None

    def test_totals_stay_exact(self) -> None:
        """Float accumulation would drift away from what the provider bills."""
        recorder = InMemoryRunRecorder()
        for _ in range(1000):
            recorder.record_agent_run(make_run(cost_usd=Decimal("0.0001")))

        assert recorder.total_cost() == Decimal("0.1000")

    def test_clear_resets(self) -> None:
        recorder = InMemoryRunRecorder()
        recorder.record_agent_run(make_run())
        recorder.record_tool_call(ToolCall(tool="web_search"))
        recorder.clear()

        assert recorder.agent_runs == []
        assert recorder.tool_calls == []


class TestJsonlPersistence:
    def test_records_round_trip(self, tmp_path: Path) -> None:
        recorder = JsonlRunRecorder(tmp_path)
        recorder.record_agent_run(make_run(research_id="res_1"))
        recorder.record_tool_call(ToolCall(tool="web_search", result_count=8))

        records = recorder.read_all()
        assert [r["kind"] for r in records] == ["agent_run", "tool_call"]
        assert records[0]["research_id"] == "res_1"
        assert records[1]["result_count"] == 8

    def test_cost_is_stored_as_a_string_not_a_float(self, tmp_path: Path) -> None:
        """Encoding Decimal as float would reintroduce the rounding error the
        pricing module exists to avoid."""
        recorder = JsonlRunRecorder(tmp_path)
        recorder.record_agent_run(make_run(cost_usd=Decimal("0.000138")))

        raw = recorder.read_all()[0]
        assert raw["cost_usd"] == "0.000138"
        assert Decimal(raw["cost_usd"]) == Decimal("0.000138")

    def test_each_record_is_an_independent_line(self, tmp_path: Path) -> None:
        """One line per record means a crash mid-write loses at most one."""
        recorder = JsonlRunRecorder(tmp_path)
        for _ in range(5):
            recorder.record_agent_run(make_run())

        path = next(tmp_path.glob("*.jsonl"))
        lines = path.read_text().strip().split("\n")
        assert len(lines) == 5
        assert all(json.loads(line)["kind"] == "agent_run" for line in lines)

    def test_directory_is_created_if_absent(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "runs"
        JsonlRunRecorder(target).record_agent_run(make_run())
        assert target.exists()

    def test_write_failure_does_not_raise(self, tmp_path: Path) -> None:
        """A recorder that throws would turn an observability problem into a
        research failure, which inverts the point."""
        recorder = JsonlRunRecorder(tmp_path)
        recorder.directory = tmp_path / "deleted"  # never created
        recorder._path_for_today().parent.mkdir(exist_ok=True)
        recorder.directory = Path("/proc/nonexistent-readonly")

        recorder.record_agent_run(make_run())  # must not raise


class TestMultiRecorder:
    def test_fans_out_to_every_backend(self, tmp_path: Path) -> None:
        memory, jsonl = InMemoryRunRecorder(), JsonlRunRecorder(tmp_path)
        MultiRunRecorder(memory, jsonl).record_agent_run(make_run())

        assert len(memory.agent_runs) == 1
        assert len(jsonl.read_all()) == 1

    def test_one_failing_backend_does_not_block_the_others(self) -> None:
        class Exploding:
            def record_agent_run(self, run: AgentRun) -> None:
                raise RuntimeError("backend down")

            def record_tool_call(self, call: ToolCall) -> None:
                raise RuntimeError("backend down")

        memory = InMemoryRunRecorder()
        recorder = MultiRunRecorder(Exploding(), memory)

        recorder.record_agent_run(make_run())
        recorder.record_tool_call(ToolCall(tool="web_search"))

        assert len(memory.agent_runs) == 1
        assert len(memory.tool_calls) == 1


class TestRecordShape:
    def test_failed_run_carries_its_error(self) -> None:
        """A failure that leaves no record cannot be diagnosed later."""
        run = make_run(status="error", error_type="LLMTimeoutError", retry_count=2)

        assert run.succeeded is False
        assert run.error_type == "LLMTimeoutError"
        assert run.retry_count == 2

    def test_failed_tool_calls_are_representable(self) -> None:
        """A source that could not be fetched explains a gap in the evidence
        rather than leaving one unaccounted for."""
        call = ToolCall(tool="fetch_url", status="error", error_type="SourceFetchError")
        assert call.succeeded is False

    def test_parent_link_makes_a_run_a_tree(self) -> None:
        original = make_run()
        repair = make_run(agent="planner.repair", parent_run_id=original.run_id)

        assert repair.parent_run_id == original.run_id
