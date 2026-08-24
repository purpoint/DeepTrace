"""Durable records of every model call and tool invocation.

This is the seam that keeps observability from becoming a retrofit. Call sites
depend on the :class:`RunRecorder` protocol, never on a concrete backend. Today
records land in JSONL files; once PostgreSQL exists they land in the
``agent_runs`` and ``tool_calls`` tables, and not one caller changes.

Recording is deliberately best-effort: a failure to write a record must never
fail the research run it is describing. Observability that can take down the
thing it observes is worse than no observability.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from core.logging import get_logger
from core.redaction import redact_mapping, redact_text

log = get_logger(__name__)


def new_run_id(prefix: str = "run") -> str:
    """Generate a short, sortable-enough identifier for one recorded unit."""
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class AgentRun:
    """One model call, with everything needed to explain and cost it later.

    The field set is dictated by a single requirement: for any failed research
    run, it must be possible to say where it failed, why, how long it took,
    which model and prompt version were used, how many times it retried, and
    what it cost. Every field below exists to answer part of that.

    Credentials are stripped from ``error_message`` here, at construction,
    rather than by the thing that writes it down. There are five recorders --
    null, in-memory, JSONL, multi, and PostgreSQL -- and redacting in each is
    exactly the "every call site remembers" pattern that the logging processor
    exists to avoid. Doing it once, in the record itself, means a recorder
    added tomorrow inherits it and cannot opt out.
    """

    agent: str
    provider: str
    model: str
    prompt_name: str
    prompt_version: str

    run_id: str = field(default_factory=lambda: new_run_id("run"))
    research_id: str | None = None
    task_id: str | None = None
    node: str | None = None
    tier: str | None = None

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    latency_ms: float = 0.0
    cost_usd: Decimal | None = None

    status: str = "success"
    error_type: str | None = None
    error_message: str | None = None
    retry_count: int = 0
    finish_reason: str | None = None

    parent_run_id: str | None = None
    """Links a repair attempt to the call it is repairing, so a run reads as a
    tree rather than a flat list of unrelated attempts."""

    started_at: datetime = field(default_factory=_utc_now)
    completed_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Strip credentials from the free-text field.

        Provider errors quote the request that failed, and a request carries an
        Authorization header. This is the field where a key actually leaks --
        not the ones anybody thought to guard.
        """
        object.__setattr__(self, "error_message", redact_text(self.error_message))
        object.__setattr__(self, "metadata", redact_mapping(self.metadata))

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def succeeded(self) -> bool:
        return self.status == "success"


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool invocation.

    Tools are the system's only contact with the outside world, so every call is
    recorded whether it succeeded or not. A source that could not be fetched is
    part of the research trace: it explains a gap in the evidence instead of
    leaving one unaccounted for.

    Being the outside contact is also why this record is the riskiest one to
    store. ``arguments`` holds a URL or a search query, and both can carry a
    credential -- a signed URL, or a key someone pasted into a question. So both
    it and ``error_message`` are redacted at construction.
    """

    tool: str
    call_id: str = field(default_factory=lambda: new_run_id("tool"))
    research_id: str | None = None
    task_id: str | None = None
    agent: str | None = None

    arguments: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    status: str = "success"
    error_type: str | None = None
    error_message: str | None = None
    retry_count: int = 0

    result_count: int | None = None
    """How many items came back, e.g. search results or extracted passages."""
    result_bytes: int | None = None
    cache_hit: bool = False

    parent_run_id: str | None = None
    started_at: datetime = field(default_factory=_utc_now)
    completed_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", redact_mapping(self.arguments))
        object.__setattr__(self, "metadata", redact_mapping(self.metadata))
        object.__setattr__(self, "error_message", redact_text(self.error_message))

    @property
    def succeeded(self) -> bool:
        return self.status == "success"


@runtime_checkable
class RunRecorder(Protocol):
    """Where run records go.

    Implementations must not raise. A recorder that throws would turn an
    observability problem into a research failure, which inverts the point.
    """

    def record_agent_run(self, run: AgentRun) -> None: ...

    def record_tool_call(self, call: ToolCall) -> None: ...


def _encode(value: Any) -> Any:
    """Serialise types the JSON encoder does not handle natively.

    ``Decimal`` becomes a string rather than a float so exact cost values
    survive a round trip; converting to float here would reintroduce the
    rounding error the pricing module avoids.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


def _to_json_dict(record: AgentRun | ToolCall) -> dict[str, Any]:
    return {key: _encode(value) for key, value in asdict(record).items()}


class NullRunRecorder:
    """Discards everything. The default when no recorder is configured.

    Lets library code call the recorder unconditionally instead of guarding
    every call with a None check.
    """

    # Arguments are intentionally unused: this is the null object of the
    # protocol, and its whole purpose is to accept and discard.
    def record_agent_run(self, run: AgentRun) -> None:  # noqa: ARG002
        return None

    def record_tool_call(self, call: ToolCall) -> None:  # noqa: ARG002
        return None


class InMemoryRunRecorder:
    """Keeps records in memory. For tests and for per-run cost aggregation."""

    def __init__(self) -> None:
        self.agent_runs: list[AgentRun] = []
        self.tool_calls: list[ToolCall] = []

    def record_agent_run(self, run: AgentRun) -> None:
        self.agent_runs.append(run)

    def record_tool_call(self, call: ToolCall) -> None:
        self.tool_calls.append(call)

    def total_cost(self) -> Decimal | None:
        """Sum recorded cost, or ``None`` if any call had unknown pricing.

        A total that silently omits unpriced calls would understate spend. When
        even one price is missing the honest answer is that the total is not
        known, which matches how estimate_cost reports a single call.
        """
        known = [run.cost_usd for run in self.agent_runs if run.cost_usd is not None]
        if len(known) != len(self.agent_runs):
            return None
        return sum(known, Decimal(0))

    def total_tokens(self) -> int:
        return sum(run.total_tokens for run in self.agent_runs)

    def clear(self) -> None:
        self.agent_runs.clear()
        self.tool_calls.clear()


class JsonlRunRecorder:
    """Appends records to newline-delimited JSON, one file per UTC day.

    JSONL is chosen because appends are atomic enough for this purpose, the
    files are greppable during development, and each line survives independently
    if the process dies mid-write. It is explicitly a stand-in for the database
    tables that arrive with the persistence milestone.

    Writes are serialised with a lock because a worker runs research tasks
    concurrently and interleaved partial lines would corrupt the file.
    """

    def __init__(self, directory: Path, *, filename_prefix: str = "runs") -> None:
        self.directory = Path(directory)
        self.filename_prefix = filename_prefix
        self._lock = threading.Lock()
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path_for_today(self) -> Path:
        return self.directory / f"{self.filename_prefix}-{_utc_now():%Y-%m-%d}.jsonl"

    def _append(self, kind: str, record: AgentRun | ToolCall) -> None:
        payload = {"kind": kind, **_to_json_dict(record)}
        line = json.dumps(payload, default=str, ensure_ascii=False)
        try:
            with self._lock, self._path_for_today().open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            # Never propagate. Losing a record is bad; failing the research run
            # that produced it is worse.
            log.warning("recorder.write_failed", kind=kind, error=str(exc))

    def record_agent_run(self, run: AgentRun) -> None:
        self._append("agent_run", run)

    def record_tool_call(self, call: ToolCall) -> None:
        self._append("tool_call", call)

    def read_all(self) -> list[dict[str, Any]]:
        """Read back every record written today. Used by tests and the CLI."""
        path = self._path_for_today()
        if not path.exists():
            return []
        with path.open(encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]


class MultiRunRecorder:
    """Fans records out to several recorders.

    Used to keep an in-memory tally for the current run while also writing the
    durable record, and later to write to PostgreSQL and a trace exporter at
    once. A failure in one recorder must not prevent the others from receiving
    the record.
    """

    def __init__(self, *recorders: RunRecorder) -> None:
        self.recorders = recorders

    def record_agent_run(self, run: AgentRun) -> None:
        for recorder in self.recorders:
            try:
                recorder.record_agent_run(run)
            except Exception as exc:
                log.warning("recorder.failed", recorder=type(recorder).__name__, error=str(exc))

    def record_tool_call(self, call: ToolCall) -> None:
        for recorder in self.recorders:
            try:
                recorder.record_tool_call(call)
            except Exception as exc:
                log.warning("recorder.failed", recorder=type(recorder).__name__, error=str(exc))


def default_recorder(directory: Path | str | None = None) -> RunRecorder:
    """Build the recorder used when nothing more specific is configured.

    Returns a no-op recorder when ``DEEPTRACE_DISABLE_RUN_RECORDING`` is set, so
    a test suite or a throwaway script does not litter the run log.
    """
    if os.getenv("DEEPTRACE_DISABLE_RUN_RECORDING"):
        return NullRunRecorder()
    from core.config import get_settings

    target = Path(directory) if directory is not None else get_settings().run_log_path
    return JsonlRunRecorder(target)
