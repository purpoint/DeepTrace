"""PostgreSQL-backed run recording.

This is the swap the RunRecorder protocol was designed for. Call sites written
in the LLM and tool layers depend on the protocol, so moving from JSONL files to
Postgres changes nothing above this module.

There is one real problem to solve. The protocol is synchronous -- ``record_agent_run``
returns None and is called from inside agent code -- while a database write is
asynchronous. Three options, and the reasons two of them are wrong:

*Make the protocol async.* Every call site becomes an await, and recording moves
onto the critical path of the research loop. A slow database would then slow
down research, which inverts the relationship: observability must not be able to
degrade the thing it observes.

*Write synchronously from the async context.* A blocking database call inside an
event loop stalls every concurrent research task sharing it, not just the one
that made the call.

*Buffer and flush.* Recording appends to an in-memory list, which cannot block
and cannot fail. Writes happen in batches at an await point the caller chooses.
This is what is implemented.

Batching is a second benefit rather than the motivation: a research run produces
dozens of records, and one insert of forty rows costs far less than forty
inserts of one.
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.logging import get_logger
from core.observability.recorder import AgentRun, ToolCall
from infrastructure.db.models import AgentRunRow, ToolCallRow

log = get_logger(__name__)

DEFAULT_FLUSH_THRESHOLD = 50


class PostgresRunRecorder:
    """Buffers run records and writes them to PostgreSQL in batches.

    Implements :class:`~core.observability.recorder.RunRecorder`. Nothing that
    records through it knows a database exists.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        research_id: str | None = None,
        flush_threshold: int = DEFAULT_FLUSH_THRESHOLD,
    ) -> None:
        """Args:
        session: The session batches are written through.
        research_id: Applied to records that did not carry one. Agents deep in
            a call stack sometimes do not have it, and a record with no run id
            cannot be found in the trace.
        flush_threshold: Buffer size at which a flush is due. Bounds memory on
            a long run without making every record a database round trip.
        """
        self.session = session
        self.research_id = research_id
        self.flush_threshold = flush_threshold
        self._agent_runs: list[AgentRun] = []
        self._tool_calls: list[ToolCall] = []

    # -- RunRecorder protocol (synchronous, never blocks, never raises) -----

    def record_agent_run(self, run: AgentRun) -> None:
        self._agent_runs.append(run)

    def record_tool_call(self, call: ToolCall) -> None:
        self._tool_calls.append(call)

    @property
    def pending(self) -> int:
        return len(self._agent_runs) + len(self._tool_calls)

    @property
    def flush_due(self) -> bool:
        return self.pending >= self.flush_threshold

    # -- persistence -------------------------------------------------------

    async def flush(self) -> int:
        """Write buffered records. Returns how many rows were written.

        Never raises. Losing observability records is bad; failing a completed
        research run because its telemetry could not be stored is worse. A
        failed flush clears the buffer rather than retrying, since retaining
        records that already failed to write would grow the buffer without
        bound on a persistently broken database.
        """
        runs, calls = self._agent_runs, self._tool_calls
        self._agent_runs, self._tool_calls = [], []

        if not runs and not calls:
            return 0

        try:
            written = 0
            if runs:
                written += await self._insert(
                    AgentRunRow, [self._agent_run_values(r) for r in runs], "run_id"
                )
            if calls:
                written += await self._insert(
                    ToolCallRow, [self._tool_call_values(c) for c in calls], "call_id"
                )
            await self.session.flush()
        except Exception as exc:
            log.warning(
                "recorder.flush_failed",
                error_type=type(exc).__name__,
                error=str(exc),
                dropped_records=len(runs) + len(calls),
            )
            return 0
        return written

    async def _insert(
        self, table: type[AgentRunRow] | type[ToolCallRow], rows: list[dict[str, object]], key: str
    ) -> int:
        """Insert rows, ignoring any whose id is already present.

        A retried job can replay records it already wrote. Ignoring the conflict
        makes recording idempotent, so a retry does not fail on a duplicate key
        and does not double-count cost.
        """
        statement = insert(table).values(rows).on_conflict_do_nothing(index_elements=[key])
        await self.session.execute(statement)
        return len(rows)

    def _agent_run_values(self, run: AgentRun) -> dict[str, object]:
        return {
            "run_id": run.run_id,
            "research_id": run.research_id or self.research_id,
            "task_id": run.task_id,
            "parent_run_id": run.parent_run_id,
            "agent": run.agent,
            "node": run.node,
            "provider": run.provider,
            "model": run.model,
            "tier": run.tier,
            "prompt_name": run.prompt_name,
            "prompt_version": run.prompt_version,
            "input_tokens": run.input_tokens,
            "output_tokens": run.output_tokens,
            "cached_tokens": run.cached_tokens,
            "latency_ms": run.latency_ms,
            "cost_usd": run.cost_usd,
            "status": run.status,
            "error_type": run.error_type,
            "error_message": run.error_message,
            "retry_count": run.retry_count,
            "finish_reason": run.finish_reason,
            "started_at": run.started_at,
            "completed_at": run.completed_at,
        }

    def _tool_call_values(self, call: ToolCall) -> dict[str, object]:
        return {
            "call_id": call.call_id,
            "research_id": call.research_id or self.research_id,
            "task_id": call.task_id,
            "parent_run_id": call.parent_run_id,
            "tool": call.tool,
            "agent": call.agent,
            "arguments": call.arguments,
            "latency_ms": call.latency_ms,
            "status": call.status,
            "error_type": call.error_type,
            "error_message": call.error_message,
            "retry_count": call.retry_count,
            "result_count": call.result_count,
            "result_bytes": call.result_bytes,
            "cache_hit": call.cache_hit,
            "started_at": call.started_at,
            "completed_at": call.completed_at,
        }
