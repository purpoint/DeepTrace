"""Command-line entry point.

Four commands, in the order you need them: ``status`` and ``check`` are
diagnostic -- they prove the package imports, configuration resolves, and
logging works without needing a database, a queue, or an API key, which makes
"the application starts" a claim anyone can verify on a fresh clone. ``research``
runs the workflow. ``resume`` continues one that stopped.

The CLI is a composition root, not a layer: it parses arguments, opens the stores
a run needs, calls one function, and prints what came back. Anything it did more
than that would be logic the API and the worker could not reuse.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import textwrap
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from core.config import DEPTH_BUDGETS, ResearchDepth, Settings, get_settings
from core.llm.pricing import format_cost
from core.logging import configure_logging, get_logger
from core.models.report import render_markdown

if TYPE_CHECKING:
    from core.models.run import ResearchRun
    from infrastructure.queue.redis_queue import RedisJobQueue

__version__ = "0.1.0"


def _mask(value: str | None) -> str:
    """Report whether a credential is configured without revealing it."""
    return "configured" if value else "not set"


def _print_status(settings: Settings) -> None:
    """Print resolved configuration, including which credentials are present.

    Values are never printed -- only presence. This is the same discipline the
    log redaction enforces, applied to terminal output.
    """
    print(f"DeepTrace {__version__}")
    print()
    print("Application")
    print(f"  environment            {settings.app_env.value}")
    print(f"  log level              {settings.log_level}")
    print(f"  json logs              {settings.json_logs}")
    print()
    print("Model routing")
    print(f"  provider               {settings.llm_provider}")
    print(f"  cheap tier             {settings.llm_model_cheap}")
    print(f"  strong tier            {settings.llm_model_strong}")
    print(f"  embeddings             {settings.llm_model_embed}")
    print()
    print("Credentials")
    print(f"  openai                 {_mask(settings.openai_api_key)}")
    print(f"  anthropic              {_mask(settings.anthropic_api_key)}")
    print(f"  google                 {_mask(settings.google_api_key)}")
    print(f"  tavily                 {_mask(settings.tavily_api_key)}")
    print()
    print("Research limits")
    print(f"  default depth          {settings.default_depth.value}")
    print(f"  max concurrent tasks   {settings.max_concurrent_tasks}")
    print(f"  max graph iterations   {settings.max_graph_iterations}")
    print()
    print("Depth budgets")
    header = f"  {'depth':<10} {'tasks':>6} {'sources':>8} {'loops':>6} {'tokens':>9}"
    print(header)
    for depth, budget in DEPTH_BUDGETS.items():
        print(
            f"  {depth.value:<10} {budget.max_tasks:>6} {budget.max_sources:>8} "
            f"{budget.max_verification_loops:>6} {budget.max_tokens:>9,}"
        )


def _run_check(settings: Settings) -> int:
    """Verify the foundation is wired correctly. Returns a process exit code."""
    log = get_logger("deeptrace.cli")

    log.info("startup.check", environment=settings.app_env.value, version=__version__)

    problems: list[str] = []
    if not any((settings.openai_api_key, settings.anthropic_api_key, settings.google_api_key)):
        problems.append("No LLM provider key is set. Add one to .env before running research.")
    if not settings.tavily_api_key:
        problems.append("No search provider key is set. Add TAVILY_API_KEY to .env.")

    print(f"configuration            loaded ({settings.app_env.value})")
    print(f"logging                  configured (level {settings.log_level})")
    print(f"depth budgets            {len(DEPTH_BUDGETS)} defined")

    if problems:
        print()
        for problem in problems:
            print(f"pending: {problem}")
        print()
        print("The foundation is healthy. Research requires the keys listed above.")
    else:
        print()
        print("All foundation checks passed.")

    return 0


def _wrap(text: str, indent: str = "     ") -> str:
    return textwrap.fill(text, width=92, initial_indent=indent, subsequent_indent=indent)


async def _persist(run: ResearchRun) -> str | None:
    """Save a finished run, returning an error string rather than raising.

    Persistence failing must not discard a run that already completed. The
    research is done and its results are in memory; losing them because the
    database was unreachable would be a worse outcome than reporting that they
    were not stored.
    """
    try:
        from infrastructure.db.engine import session_scope
        from infrastructure.db.recorder import PostgresRunRecorder
        from infrastructure.db.repositories.research import ResearchRepository

        async with session_scope() as session:
            recorder = PostgresRunRecorder(session, research_id=run.research_id)
            for record in run.usage.agent_runs:
                recorder.record_agent_run(record)
            for call in run.usage.tool_calls:
                recorder.record_tool_call(call)

            await ResearchRepository(session).save_run(run)
            await recorder.flush()
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


@asynccontextmanager
async def _checkpointer(enabled: bool) -> AsyncIterator[Any]:
    """Open the durable checkpoint store, or hand back nothing.

    A run without one still completes; it simply cannot be resumed. Making that
    a flag rather than the default keeps a smoke test from requiring a database,
    which is the same reason the research engine does not import one.
    """
    if not enabled:
        yield None
        return

    from infrastructure.db.checkpointer import checkpointer_scope

    async with checkpointer_scope() as saver:
        yield saver


def _run_research(args: argparse.Namespace) -> int:
    """Run the workflow and print a readable trace."""
    from core.pipeline import run_research

    depth = ResearchDepth(args.depth)

    async def execute() -> tuple[ResearchRun, str | None]:
        async with _checkpointer(args.checkpoint) as saver:
            result = await run_research(
                args.question, depth=depth, max_tasks=args.max_tasks, checkpointer=saver
            )
        save_error = await _persist(result) if args.save else None
        return result, save_error

    try:
        run, save_error = asyncio.run(execute())
    except Exception as exc:
        # Research failure is returned, never raised, so anything arriving here
        # came from opening the checkpoint store -- which is worth reporting as
        # what it is rather than as a failed run.
        print(f"checkpointing unavailable: {type(exc).__name__}: {exc}")
        print("Run without --checkpoint to research without a resumable record.")
        return 1

    return _print_run(run, save=args.save, save_error=save_error, checkpointed=args.checkpoint)


def _resume_research(args: argparse.Namespace) -> int:
    """Continue a checkpointed run from wherever it stopped."""
    from core.graph.workflow import CheckpointNotFound
    from core.pipeline import resume_research

    async def execute() -> tuple[ResearchRun, str | None]:
        async with _checkpointer(True) as saver:
            result = await resume_research(args.research_id, checkpointer=saver)
        save_error = await _persist(result) if args.save else None
        return result, save_error

    try:
        run, save_error = asyncio.run(execute())
    except CheckpointNotFound as exc:
        print(f"cannot resume: {exc}")
        return 1

    return _print_run(run, save=args.save, save_error=save_error, checkpointed=True)


def _print_run(
    run: ResearchRun, *, save: bool, save_error: str | None, checkpointed: bool = False
) -> int:
    """Print the trace, stage by stage.

    Prints each stage rather than only the final result, because the point of
    the system is that a conclusion can be walked back to what produced it.

    Shared by ``research`` and ``resume`` so a resumed run is displayed by the
    same code as any other. A second printer would eventually disagree with this
    one about what a run contains.
    """
    continued = "   resumed" if run.resumed else ""
    print()
    print(
        f"Research {run.research_id}   depth={run.depth.value}   {run.elapsed_seconds}s{continued}"
    )
    print("=" * 94)

    # The report first. It is what the run was for, and printing the trace
    # ahead of it would make the trace the product and the answer an appendix.
    if run.report is not None:
        print()
        print(render_markdown(run.report))
        print("-" * 94)
        print(f"report: {run.report.summary()}")
        print("-" * 94)

    if run.spec is not None:
        print()
        print("QUESTION ANALYSIS")
        print(_wrap(run.spec.normalized_question))
        print(
            f"     type: {run.spec.research_type.value}   "
            f"freshness: {run.spec.time_sensitivity.value}   "
            f"scope items: {len(run.spec.scope)}"
        )
        for item in run.spec.ambiguities:
            print(_wrap(f"assumed - {item.aspect}: {item.assumption}"))

    if run.plan is not None:
        print()
        print(f"PLAN   {run.plan.summary()}")
        for task in run.plan.tasks:
            marker = "*" if any(r.task_id == task.id for r in run.task_results) else " "
            print(f"   {marker} [{task.priority.value:<6}] {task.id}")

    if run.task_results:
        print()
        print("RESEARCH")
        for result in run.task_results:
            print(f"   {result.summary()}")
            for source in sorted(
                result.usable_sources, key=lambda s: s.quality_score, reverse=True
            )[:5]:
                print(f"       {source.quality_score:.2f}  {source.summary()}")
            for url, reason in result.failed_urls[:3]:
                print(f"       ----  blocked: {url[:52]} ({reason[:40]})")

    if run.evidence_report is not None:
        report = run.evidence_report
        print()
        print(f"EVIDENCE   {report.summary()}")
        for evidence in sorted(run.evidence, key=lambda e: e.weight, reverse=True)[:8]:
            print()
            print(_wrap(f"[{evidence.weight:.2f}] {evidence.claim}", indent="   "))
            print(_wrap(f'"{evidence.supporting_text[:220]}"', indent="       "))
            status = evidence.verification.status.value if evidence.verification else "unchecked"
            print(f"       -> {evidence.source_id} ({status})")

        if report.rejected:
            print()
            print("   REJECTED (passage not found in its source)")
            for claim, reason in report.rejected[:5]:
                print(_wrap(f"x {claim}", indent="       "))
                print(_wrap(reason, indent="         "))

        if report.injection_attempts:
            print()
            print(f"   prompt injection observed in: {', '.join(report.injection_attempts)}")

    if run.analysis_report is not None:
        analysis = run.analysis_report.analysis
        print()
        print(f"ANALYSIS   {run.analysis_report.summary()}")
        print(_wrap(analysis.summary, indent="   "))

        for finding in analysis.findings:
            print()
            print(_wrap(f"* {finding.statement}", indent="   "))
            print(
                f"       {finding.confidence.value} confidence, "
                f"{finding.corroborating_domains} publisher(s), "
                f"evidence: {', '.join(finding.evidence_ids[:4])}"
            )

        for tradeoff in analysis.tradeoffs:
            print()
            print(_wrap(f"trade-off - {tradeoff.subject}", indent="   "))
            print(_wrap(f"+ {tradeoff.benefit}", indent="       "))
            print(_wrap(f"- {tradeoff.cost}", indent="       "))

        # Printed prominently rather than folded into the findings: a contested
        # question is one of the most useful things research establishes, and a
        # reader who skims must not miss that the sources disagree.
        for contradiction in analysis.contradictions:
            print()
            print(_wrap(f"CONTRADICTION - {contradiction.subject}", indent="   "))
            print(_wrap(f"A: {contradiction.position_a}", indent="       "))
            print(_wrap(f"B: {contradiction.position_b}", indent="       "))

        for recommendation in analysis.recommendations:
            print()
            print(_wrap(f"> {recommendation.recommendation}", indent="   "))
            print(_wrap(f"when: {recommendation.condition}", indent="       "))

        if analysis.open_questions:
            print()
            print("   NOT ANSWERED BY THIS RESEARCH")
            for question in analysis.open_questions:
                print(_wrap(f"? {question.question}", indent="       "))
                print(_wrap(question.why_unanswered, indent="         "))

        if run.analysis_report.dropped:
            print()
            print("   DISCARDED (cited evidence that does not exist)")
            for statement, reason in run.analysis_report.dropped[:5]:
                print(_wrap(f"x {statement}", indent="       "))
                print(_wrap(reason, indent="         "))

    if run.claim_set is not None and run.claims:
        print()
        print(f"CLAIMS   {run.claim_set.summary()}")
        for stated in sorted(run.claims, key=lambda item: item.strength, reverse=True):
            print()
            print(_wrap(f"{stated.id}  {stated.text}", indent="   "))
            detail = (
                f"       {stated.kind.value}/{stated.status.value}  "
                f"{stated.confidence.value} confidence  "
                f"strength {stated.strength}  "
                f"{len(stated.evidence)} evidence"
            )
            if stated.merged_from > 1:
                detail += f"  ({stated.merged_from} merged)"
            print(detail)
            if stated.condition:
                print(_wrap(f"when: {stated.condition}", indent="       "))
            if stated.conflicts_with:
                print(f"       conflicts with: {', '.join(stated.conflicts_with)}")

            verdict = run.verification.verdict_for(stated.id) if run.verification else None
            if verdict is not None:
                print(_wrap(verdict.reasoning, indent="       "))
                if verdict.contradicting_evidence_ids:
                    # The most valuable thing verification finds: a passage
                    # from elsewhere in the run that undercuts this claim.
                    print(
                        "       CONTRADICTED BY: "
                        f"{', '.join(verdict.contradicting_evidence_ids[:3])}"
                    )
                if verdict.overgeneralization:
                    print(_wrap(f"too broad: {verdict.overgeneralization}", indent="       "))
                if verdict.suggested_revision:
                    print(_wrap(f"revise to: {verdict.suggested_revision}", indent="       "))

    if run.verification is not None and run.verification.verdicts:
        print()
        rounds = (
            f"   (after {run.research_loops} extra research round"
            f"{'s' if run.research_loops > 1 else ''})"
            if run.research_loops
            else ""
        )
        print(f"VERIFICATION   {run.verification.summary()}{rounds}")
        if run.verification.follow_up_questions:
            print()
            print("   WOULD SETTLE WHAT IS STILL OPEN")
            for follow_up in run.verification.follow_up_questions:
                print(_wrap(f"? {follow_up}", indent="       "))

    print()
    print("COST")
    usage = run.usage
    for record in usage.agent_runs:
        print(
            f"   {record.agent:<22} {record.model:<24} "
            f"{record.input_tokens:>6} in /{record.output_tokens:>6} out  "
            f"{record.latency_ms:>7.0f} ms"
        )
    print(
        f"   {'total':<22} {'':<24} {usage.total_tokens():>6} tokens"
        f"          {format_cost(usage.total_cost())}"
    )
    print(f"   tool calls: {len(usage.tool_calls)}")
    if run.resumed:
        # The tally covers this execution only. Everything restored from the
        # checkpoint was paid for on the earlier attempt, and presenting the two
        # as one total would understate what the research cost.
        print("   (this attempt only -- steps restored from the checkpoint cost nothing here)")

    if save:
        print()
        if save_error:
            print(f"   NOT SAVED: {save_error}")
        else:
            print(f"   saved to database as {run.research_id}")

    if run.error:
        print()
        print(f"FAILED: {run.error}")
        if checkpointed:
            # Only offered when there is something to continue. Suggesting it
            # for a run with no checkpoint would send the reader to a command
            # that can only tell them the state does not exist.
            print(f"       deeptrace resume {run.research_id}")
        return 1

    print()
    return 0


@asynccontextmanager
async def _queue() -> AsyncIterator[RedisJobQueue]:
    """Open the job queue, and close its connection pool afterwards.

    Imported inside the function like the database is: a CLI that resolves
    Redis at import time cannot run ``status`` on a machine without it, and
    diagnosing a broken install is exactly when that matters.
    """
    from infrastructure.queue.redis_queue import RedisJobQueue

    queue = RedisJobQueue.from_settings()
    try:
        yield queue
    finally:
        await queue.close()


def _submit(args: argparse.Namespace) -> int:
    """Queue a job and return. The work happens in a worker."""
    from infrastructure.queue.job import Job

    async def execute() -> Job:
        async with _queue() as queue:
            return await queue.enqueue(
                Job(
                    question=args.question,
                    depth=ResearchDepth(args.depth),
                    max_tasks=args.max_tasks,
                )
            )

    try:
        job = asyncio.run(execute())
    except Exception as exc:
        print(f"could not reach the queue: {type(exc).__name__}: {exc}")
        return 1

    print(f"queued {job.id}")
    print(f"   research id  {job.research_id}")
    print(f"   depth        {job.depth.value}")
    print()
    print("Run a worker to execute it:  deeptrace work")
    print(f"Check on it:                 deeptrace jobs {job.id}")
    return 0


def _work(args: argparse.Namespace) -> int:
    """Run a worker until it is stopped."""
    from apps.worker.runner import Worker, install_signal_handlers
    from infrastructure.db.checkpointer import checkpointer_scope
    from infrastructure.queue.events import RedisProgressStream

    async def execute() -> None:
        stream = RedisProgressStream.from_settings()
        try:
            await _work_with(args, stream)
        finally:
            # Closed explicitly: the stream holds its own connection pool, and a
            # worker restarted in a loop would leak one per restart.
            await stream.close()

    async def _work_with(args: argparse.Namespace, stream: object) -> None:
        async with _queue() as queue, checkpointer_scope() as saver:
            worker = Worker(queue, checkpointer=saver, progress=stream)  # type: ignore[arg-type]
            install_signal_handlers(worker)

            if args.once:
                # Reclaim first. A single-shot worker that only reserved could
                # never pick up a job abandoned by a crashed one -- which is
                # the case this whole layer exists for.
                reclaimed = await queue.reclaim_stalled()
                if reclaimed:
                    print(f"reclaimed {len(reclaimed)} abandoned job(s)")

                job = await queue.reserve(worker.name, timeout=5)
                if job is None:
                    print("no job waiting")
                    return
                await worker.execute(job)
                return

            await worker.run_forever()

    try:
        asyncio.run(execute())
    except KeyboardInterrupt:  # pragma: no cover - a second interrupt
        print("stopped")
    except Exception as exc:
        print(f"worker could not start: {type(exc).__name__}: {exc}")
        return 1
    return 0


def _jobs(args: argparse.Namespace) -> int:
    """Show one job, the queue's depth, or ask a job to stop."""

    async def execute() -> int:
        async with _queue() as queue:
            if args.job_id is None:
                depth = await queue.depth()
                print(f"pending    {depth['pending']}")
                print(f"processing {depth['processing']}")
                print(f"dead       {depth['dead']}")
                return 0

            if args.cancel:
                if await queue.request_cancel(args.job_id):
                    print(f"cancellation requested for {args.job_id}")
                    return 0
                print(f"{args.job_id} is not running, or does not exist")
                return 1

            job = await queue.get(args.job_id)
            if job is None:
                print(f"no job {args.job_id}")
                return 1

            print(job.summary())
            print(f"   research id  {job.research_id}")
            print(f"   worker       {job.worker or '-'}")
            if job.error:
                print(_wrap(f"error: {job.error}", indent="   "))
            return 0

    try:
        return asyncio.run(execute())
    except Exception as exc:
        print(f"could not reach the queue: {type(exc).__name__}: {exc}")
        return 1


def _serve(args: argparse.Namespace) -> int:
    """Run the API.

    Uvicorn is given the factory rather than an instance, so the application is
    built inside the server's process. With ``--reload`` that matters: the
    reloader re-imports the module, and an app constructed at import time would
    open a second database pool on every code change.
    """
    import uvicorn

    uvicorn.run(
        "apps.api.main:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_config=None,  # the application configures structured logging itself
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deeptrace",
        description="Autonomous AI research and evidence synthesis platform.",
    )
    parser.add_argument("--version", action="version", version=f"deeptrace {__version__}")

    subcommands = parser.add_subparsers(dest="command")
    subcommands.add_parser("status", help="Show resolved configuration and depth budgets")
    subcommands.add_parser("check", help="Verify the foundation is correctly wired")

    research = subcommands.add_parser("research", help="Run the full research workflow")
    research.add_argument("question", help="The research question")
    research.add_argument(
        "--depth",
        choices=[d.value for d in ResearchDepth],
        default=ResearchDepth.STANDARD.value,
        help="Budget ceilings for the run (default: standard)",
    )
    research.add_argument(
        "--max-tasks",
        type=int,
        default=None,
        metavar="N",
        help="Research only the first N tasks. Useful for a cheap smoke test.",
    )
    research.add_argument(
        "--save",
        action="store_true",
        help="Persist the run to PostgreSQL (requires DATABASE_URL and migrations)",
    )
    research.add_argument(
        "--checkpoint",
        action="store_true",
        help="Write workflow state after each step so the run can be resumed",
    )

    resume = subcommands.add_parser(
        "resume",
        help="Continue a checkpointed run from wherever it stopped",
    )
    resume.add_argument("research_id", help="The id of a run started with --checkpoint")
    resume.add_argument(
        "--save",
        action="store_true",
        help="Persist the run to PostgreSQL once it finishes",
    )

    submit = subcommands.add_parser("submit", help="Queue a research job for a worker to run")
    submit.add_argument("question", help="The research question")
    submit.add_argument(
        "--depth",
        choices=[d.value for d in ResearchDepth],
        default=ResearchDepth.STANDARD.value,
    )
    submit.add_argument("--max-tasks", type=int, default=None, metavar="N")

    work = subcommands.add_parser("work", help="Run a worker that consumes research jobs")
    work.add_argument(
        "--once",
        action="store_true",
        help="Take a single job and exit, rather than running until stopped",
    )

    serve = subcommands.add_parser("serve", help="Run the HTTP API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument(
        "--reload", action="store_true", help="Restart on code changes, for development"
    )

    jobs = subcommands.add_parser("jobs", help="Show a job, or the queue's depth")
    jobs.add_argument("job_id", nargs="?", help="A job id. Omit for queue depth.")
    jobs.add_argument("--cancel", action="store_true", help="Ask a running job to stop")

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point registered as the ``deeptrace`` console script."""
    parser = build_parser()
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings)

    if args.command == "status":
        _print_status(settings)
        return 0
    if args.command == "check":
        return _run_check(settings)
    if args.command == "research":
        return _run_research(args)
    if args.command == "resume":
        return _resume_research(args)
    if args.command == "submit":
        return _submit(args)
    if args.command == "work":
        return _work(args)
    if args.command == "jobs":
        return _jobs(args)
    if args.command == "serve":
        return _serve(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
