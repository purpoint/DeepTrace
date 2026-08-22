"""Command-line entry point.

Its job at this stage is diagnostic: prove the package imports, configuration
resolves, and logging works, without needing a database, a queue, or an API key.
That makes "the application starts" a claim anyone can verify on a fresh clone
rather than something the README asserts.

Research commands are added by the milestones that implement them.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import textwrap
from typing import TYPE_CHECKING

from core.config import DEPTH_BUDGETS, ResearchDepth, Settings, get_settings
from core.llm.pricing import format_cost
from core.logging import configure_logging, get_logger

if TYPE_CHECKING:
    from core.pipeline import ResearchRun

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


def _run_research(args: argparse.Namespace) -> int:
    """Run the full pipeline and print a readable trace.

    Prints each stage as it becomes available rather than only the final
    result, because the point of the system is that a conclusion can be walked
    back to what produced it.
    """
    from core.pipeline import run_research

    depth = ResearchDepth(args.depth)

    async def execute() -> tuple[ResearchRun, str | None]:
        result = await run_research(args.question, depth=depth, max_tasks=args.max_tasks)
        save_error = await _persist(result) if args.save else None
        return result, save_error

    run, save_error = asyncio.run(execute())

    print()
    print(f"Research {run.research_id}   depth={run.depth.value}   {run.elapsed_seconds}s")
    print("=" * 94)

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

    if args.save:
        print()
        if save_error:
            print(f"   NOT SAVED: {save_error}")
        else:
            print(f"   saved to database as {run.research_id}")

    if run.error:
        print()
        print(f"FAILED: {run.error}")
        return 1

    print()
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

    research = subcommands.add_parser("research", help="Run the full research pipeline")
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

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
