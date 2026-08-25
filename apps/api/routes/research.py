"""The research endpoints.

Nine of them, and the shape of the set is the design: one to ask a question,
one to list what has been asked, and seven to look at a single run from
different distances -- its status, its report, its claims, its evidence, its
sources, its trace, and one to stop it.

That fan-out is the product. A system whose answer can only be read as prose is
a chatbot with extra steps; the reason to keep sources, evidence, claims and
verdicts as separate records is so a reader can descend from a sentence to the
page it came from, one endpoint at a time.

Submitting never runs research. It writes a job and returns, because a research
run takes minutes and an HTTP request that waits for one has already failed --
the client times out, retries, and now two runs are in flight for one question.

Every one of them is scoped to the caller, and almost none of them say so. The
repository these routes are handed can only see its owner's research, so a
request for a stranger's run returns nothing and becomes a 404 without any route
comparing two ids. The exception is the job record, which lives in Redis rather
than behind that repository -- and the ownership check for it is written out
explicitly below, because it is the one place the structure does not do it.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, status

from apps.api.dependencies.identity import Authenticated, CurrentUser, Repository
from apps.api.dependencies.providers import Config, Limiter, Queue
from apps.api.errors import ApiError
from apps.api.schemas import (
    AgentSpend,
    CancelResponse,
    ClaimView,
    CostView,
    EvidenceView,
    JobView,
    ReportView,
    ResearchDetail,
    ResearchSummary,
    SourceView,
    SubmitRequest,
    SubmitResponse,
    ToolSpend,
    TraceEntry,
    TraceView,
)
from core.llm.pricing import PRICING, normalise_model
from core.logging import get_logger
from core.observability.tracing import carrier_for_current_span
from infrastructure.db.models import AgentRunRow
from infrastructure.queue.job import Job
from infrastructure.queue.redis_queue import RedisJobQueue

log = get_logger(__name__)

router = APIRouter(prefix="/research", tags=["research"])


@router.post(
    "",
    response_model=SubmitResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ask a question",
)
async def submit(
    body: SubmitRequest,
    queue: Queue,
    user: Authenticated,
    limiter: Limiter,
    settings: Config,
) -> SubmitResponse:
    """Queue a research job and return immediately.

    202 rather than 201: nothing has been created yet except the intention. The
    research does not exist until a worker has run it, and saying otherwise
    would make a client that follows the Location header find nothing there.

    Rate limited per user rather than per address, because what this limit
    protects is money. A research run costs model calls and search credits, and
    the account is who those are spent on -- counting by address would let one
    person on two networks spend twice, and charge two colleagues in one office
    as though they were one.
    """
    decision = await limiter.check(
        "submit",
        user.id,
        limit=settings.submit_rate_limit,
        window=settings.submit_rate_window_seconds,
    )
    if not decision.allowed:
        log.warning("api.submit_rate_limited", user_id=user.id)
        raise ApiError.rate_limited(decision.retry_after, limit=decision.limit)

    job = Job(
        question=body.question,
        depth=body.depth,
        max_tasks=body.max_tasks,
        user_id=user.id,
        # The submitting span's context, so the worker's run continues this
        # trace instead of starting an unrelated one.
        trace_carrier=carrier_for_current_span(),
    )

    try:
        await queue.enqueue(job)
    except Exception as exc:
        # The queue being unreachable is not the caller's fault and is worth
        # retrying, which a 503 says and a 500 does not.
        log.error("api.enqueue_failed", error_type=type(exc).__name__, error=str(exc))
        raise ApiError.unavailable("The job queue") from exc

    log.info("api.submitted", job_id=job.id, research_id=job.research_id, user_id=user.id)
    return SubmitResponse(
        job_id=job.id,
        research_id=job.research_id,
        status=job.status.value,
        poll=f"/research/{job.research_id}",
    )


@router.get("", response_model=list[ResearchSummary], summary="List research")
async def list_research(
    repository: Repository,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[ResearchSummary]:
    """The caller's research history, newest first.

    Bounded by default and capped at a hundred. An unbounded list endpoint is
    fine until the table is large, and then it is the query that takes the
    service down.

    No user filter appears here. The repository was built scoped to the caller,
    so the filter is in the query it issues -- which is the difference between
    an endpoint that shows the right rows and an endpoint that shows the right
    rows as long as this line is not deleted.
    """
    sessions = await repository.list_sessions(limit=limit)
    return [
        ResearchSummary(
            research_id=row.id,
            question=row.question,
            depth=row.depth,  # type: ignore[arg-type]
            status=row.status,
            created_at=row.created_at,
            completed_at=row.completed_at,
            error=row.error,
        )
        for row in sessions
    ]


@router.get("/{research_id}", response_model=ResearchDetail, summary="One run")
async def get_research(
    research_id: str, repository: Repository, queue: Queue, user: Authenticated
) -> ResearchDetail:
    """A run's status and size, from whichever record exists.

    A run that has been queued but not yet archived has no database row -- the
    worker writes that at the end. Falling back to the job means a client can
    poll this endpoint from the moment it submits, rather than receiving 404s
    for several minutes and having to tell a user something reassuring about
    them.
    """
    row = await repository.get_session(research_id)
    job = await _job_for(queue, research_id, user)

    if row is None:
        if job is None:
            raise ApiError.not_found("research", research_id)

        return ResearchDetail(
            research_id=job.research_id,
            question=job.question,
            depth=job.depth,
            status=job.status.value,
            created_at=job.created_at,
            error=job.error,
            job=JobView(
                job_id=job.id,
                status=job.status.value,
                attempts=job.attempts,
                worker=job.worker,
                error=job.error,
            ),
        )

    sources = await repository.get_sources(research_id)
    evidence = await repository.get_evidence(research_id)
    claims = await repository.get_claims(research_id)

    return ResearchDetail(
        research_id=row.id,
        question=row.question,
        normalized_question=row.normalized_question,
        research_type=row.research_type,
        depth=row.depth,  # type: ignore[arg-type]
        status=row.status,
        created_at=row.created_at,
        completed_at=row.completed_at,
        error=row.error,
        sources=len(sources),
        evidence=len(evidence),
        claims=len(claims),
        has_report=row.report_markdown is not None,
        job=(
            JobView(
                job_id=job.id,
                status=job.status.value,
                attempts=job.attempts,
                worker=job.worker,
                error=job.error,
            )
            if job
            else None
        ),
    )


@router.get("/{research_id}/report", response_model=ReportView, summary="The report")
async def get_report(research_id: str, repository: Repository) -> ReportView:
    """The finished document.

    404 when the run exists but produced no report, rather than an empty one: a
    report that has not been written and a report that says nothing are
    different outcomes, and a client showing an empty page for the first is
    lying about the second.
    """
    row = await repository.get_session(research_id)
    if row is None:
        raise ApiError.not_found("research", research_id)
    if row.report is None or row.report_markdown is None:
        raise ApiError.not_found("report for research", research_id)

    report: dict[str, Any] = row.report
    return ReportView(
        research_id=research_id,
        title=report.get("title", ""),
        markdown=row.report_markdown,
        structured=report,
        citations=len(report.get("citations", [])),
        fully_cited=not report.get("unresolved_markers"),
    )


@router.get("/{research_id}/claims", response_model=list[ClaimView], summary="Claims")
async def get_claims(
    research_id: str,
    repository: Repository,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> list[ClaimView]:
    """What the run asserts, with each verdict and its reasoning.

    Unfiltered by default, including the unsupported ones. A client building a
    report view asks for supported claims; a client showing how the research was
    checked needs to see what was rejected, and hiding it here would make the
    verification invisible.
    """
    await _require_research(repository, research_id)
    rows = await repository.get_claims(research_id, status=status_filter)
    return [
        ClaimView(
            id=row.id,
            text=row.text,
            kind=row.kind,
            status=row.status,
            confidence=row.confidence,
            strength=row.strength,
            condition=row.condition,
            disposition=row.disposition,
            reasoning=row.verification_reasoning,
            overgeneralization=row.overgeneralization,
            suggested_revision=row.suggested_revision,
            follow_up_question=row.follow_up_question,
            conflicts_with=(row.conflicts_with or {}).get("claims", []),
            contradicted_by=(row.contradicted_by or {}).get("evidence", []),
        )
        for row in rows
    ]


@router.get("/{research_id}/evidence", response_model=list[EvidenceView], summary="Evidence")
async def get_evidence(research_id: str, repository: Repository) -> list[EvidenceView]:
    """The passages, strongest first, with how each matched its source."""
    await _require_research(repository, research_id)
    rows = await repository.get_evidence(research_id)
    return [
        EvidenceView(
            id=row.id,
            source_id=row.source_id,
            task_id=row.task_id,
            claim=row.claim,
            supporting_text=row.supporting_text,
            location=row.location,
            quote_status=row.quote_status,
            quote_similarity=row.quote_similarity,
            weight=row.weight,
        )
        for row in rows
    ]


@router.get("/{research_id}/sources", response_model=list[SourceView], summary="Sources")
async def get_sources(research_id: str, repository: Repository) -> list[SourceView]:
    """The pages retrieved, without their text.

    The content is deliberately absent. A source's page can be tens of
    kilobytes, and a list of twenty would be a megabyte of HTML sent to answer
    "where did this come from".
    """
    await _require_research(repository, research_id)
    rows = await repository.get_sources(research_id)
    return [
        SourceView(
            id=row.id,
            url=row.url,
            title=row.title,
            domain=row.domain,
            source_type=row.source_type,
            quality_score=row.quality_score,
            word_count=row.word_count,
            fetch_failed=row.fetch_failed,
            retrieved_at=row.retrieved_at,
        )
        for row in rows
    ]


@router.get("/{research_id}/trace", response_model=TraceView, summary="What it did")
async def get_trace(research_id: str, repository: Repository) -> TraceView:
    """Every model call and tool call, in order.

    The endpoint the whole project is named for. A reader who does not trust the
    report can read what produced it: which prompts ran, which searches were
    issued, what each cost, and what failed.
    """
    await _require_research(repository, research_id)
    rows = await repository.get_trace(research_id)

    entries: list[TraceEntry] = []
    total_tokens = 0
    for row in rows:
        if isinstance(row, AgentRunRow):
            total_tokens += row.input_tokens + row.output_tokens
            entries.append(
                TraceEntry(
                    kind="model",
                    name=row.agent or "unknown",
                    started_at=row.started_at,
                    latency_ms=row.latency_ms,
                    status=row.status,
                    detail={
                        "model": row.model,
                        "prompt": row.prompt_name,
                        "input_tokens": row.input_tokens,
                        "output_tokens": row.output_tokens,
                        "retry_count": row.retry_count,
                    },
                )
            )
        else:
            entries.append(
                TraceEntry(
                    kind="tool",
                    name=row.tool,
                    started_at=row.started_at,
                    latency_ms=row.latency_ms,
                    status=row.status,
                    detail={
                        "task_id": row.task_id,
                        "result_count": row.result_count,
                        "cache_hit": row.cache_hit,
                        "error_type": row.error_type,
                    },
                )
            )

    return TraceView(
        research_id=research_id,
        entries=entries,
        total_tokens=total_tokens,
        cost_usd=await repository.total_cost(research_id),
    )


@router.get("/{research_id}/cost", response_model=CostView, summary="What it cost")
async def get_cost(research_id: str, repository: Repository) -> CostView:
    """A run's bill, broken down by agent and by tool.

    The trace endpoint already carries a total. This exists because a total is
    the least useful form of the answer: "this run cost eleven cents" prompts
    the question "on what", and the only actionable version names the agent.

    Every figure here is measured rather than estimated, and the response says
    so. A run containing calls with no recorded price reports ``complete:
    false`` and counts them, because a sum over unpriced calls is a number that
    looks authoritative and understates -- which is worse than admitting the
    total is unknown.
    """
    await _require_research(repository, research_id)

    agents = await repository.cost_breakdown(research_id)
    tools = await repository.tool_breakdown(research_id)

    unpriced = sum(int(row["unpriced"]) for row in agents)
    total = await repository.total_cost(research_id)

    # A group is only quotable if every call in it was priced. Reporting a
    # partial sum per row would repeat, per agent, the exact mistake the
    # run-level total is careful to avoid.
    by_agent = [
        AgentSpend(
            agent=row["agent"],
            model=row["model"],
            calls=int(row["calls"]),
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            latency_ms=float(row["latency_ms"]),
            cost_usd=(
                float(row["cost_usd"])
                if row["cost_usd"] is not None and not row["unpriced"]
                else None
            ),
            unpriced=int(row["unpriced"]),
            failed=int(row["failed"]),
        )
        for row in agents
    ]

    stale = sorted(
        {
            row["model"]
            for row in agents
            if (price := PRICING.get(normalise_model(row["model"]))) is not None
            and price.is_stale()
        }
    )

    return CostView(
        research_id=research_id,
        total_cost_usd=total,
        complete=unpriced == 0 and bool(agents),
        unpriced_calls=unpriced,
        total_input_tokens=sum(int(row["input_tokens"]) for row in agents),
        total_output_tokens=sum(int(row["output_tokens"]) for row in agents),
        model_latency_ms=sum(float(row["latency_ms"]) for row in agents),
        tool_latency_ms=sum(float(row["latency_ms"]) for row in tools),
        by_agent=by_agent,
        by_tool=[
            ToolSpend(
                tool=row["tool"],
                calls=int(row["calls"]),
                latency_ms=float(row["latency_ms"]),
                failed=int(row["failed"]),
            )
            for row in tools
        ],
        stale_prices=stale,
    )


@router.post("/{research_id}/cancel", response_model=CancelResponse, summary="Stop a run")
async def cancel(
    research_id: str, repository: Repository, queue: Queue, user: Authenticated
) -> CancelResponse:
    """Ask a running job to stop, and stop it spending.

    Cancellation reaches the worker as a flag rather than a signal, because the
    process running the research may be on another machine. What it stops is the
    task, which stops the model calls -- marking a job cancelled while its calls
    continue would be a status that lies and a bill that grows.
    """
    job = await _job_for(queue, research_id, user)
    if job is None:
        await _require_research(repository, research_id)
        return CancelResponse(
            research_id=research_id,
            cancelled=False,
            message="This research has already finished; there is nothing to stop.",
        )

    stopped = await queue.request_cancel(job.id)
    return CancelResponse(
        research_id=research_id,
        cancelled=stopped,
        message=(
            "Cancellation requested. The worker stops at its next check."
            if stopped
            else "This job has already finished."
        ),
    )


async def _require_research(repository: Repository, research_id: str) -> None:
    """404 before doing work, so a bad id costs one query rather than four."""
    if await repository.get_session(research_id) is None:
        raise ApiError.not_found("research", research_id)


async def _job_for(queue: RedisJobQueue, research_id: str, user: CurrentUser) -> Job | None:
    """The job behind a run, if it is this user's job.

    The one ownership check written by hand in this module, and it is here
    because the job lives in Redis rather than behind the scoped repository --
    so nothing filters it unless this does.

    It is load-bearing. A queued run has no database row yet, which is exactly
    why ``get_research`` falls back to the job: without this comparison, anyone
    who guessed a research id could read the question someone else had just
    asked, during the only window in which the repository has nothing to refuse
    them with. And cancelling is worse than reading -- it would let a stranger
    stop a stranger's work.
    """
    job = await queue.get_by_research(research_id)
    if job is None or job.user_id != user.id:
        return None
    return job
