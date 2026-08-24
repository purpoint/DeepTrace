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
        from infrastructure.db.repositories.scope import Viewer

        async with session_scope() as session:
            recorder = PostgresRunRecorder(session, research_id=run.research_id)
            for record in run.usage.agent_runs:
                recorder.record_agent_run(record)
            for call in run.usage.tool_calls:
                recorder.record_tool_call(call)

            # No account. A run made from the command line belongs to the
            # machine, not to a user of the service, and it is therefore
            # invisible through the API -- which is the honest outcome rather
            # than an oversight. `deeptrace submit` is the way to make a run a
            # person owns.
            await ResearchRepository(session, Viewer.system()).save_run(run)
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


def _evaluate(args: argparse.Namespace, settings: Settings) -> int:
    """Run the benchmark and write EVALUATION.md.

    ``--dry-run`` is the default-adjacent mode for a reason: a full benchmark
    makes hundreds of model calls, and on a free tier that is most of a day's
    quota. The dry run validates the dataset and shows exactly what would be
    spent, so nobody discovers a broken question after paying for twenty-three
    others.
    """
    from pathlib import Path

    from core.evaluation.dataset import BENCHMARK, BenchmarkQuestion, coverage_summary
    from core.evaluation.harness import (
        BenchmarkResults,
        make_executor,
        provenance,
        run_benchmark,
    )
    from core.evaluation.metrics import RunEvaluation, aggregate
    from core.evaluation.report import render

    depth = ResearchDepth(args.depth)
    budget = DEPTH_BUDGETS[depth]
    if args.questions:
        wanted = [item.strip() for item in args.questions.split(",") if item.strip()]
        known = {question.id: question for question in BENCHMARK}
        unknown = [item for item in wanted if item not in known]
        if unknown:
            print(f"unknown question id(s): {', '.join(unknown)}")
            return 1
        questions = tuple(known[item] for item in wanted)
    elif args.per_type:
        # N from each research type rather than the first N overall. A subset
        # taken off the top of the list is five comparisons and one
        # explanation, which reports a comparison score and calls it a
        # benchmark -- the same failure the dataset test guards against.
        grouped: dict[str, list[BenchmarkQuestion]] = {}
        for question in BENCHMARK:
            grouped.setdefault(question.research_type.value, []).append(question)
        questions = tuple(
            question for group in grouped.values() for question in group[: args.per_type]
        )
    else:
        questions = BENCHMARK[: args.limit] if args.limit else BENCHMARK

    results_path = Path(args.results)
    store = BenchmarkResults(results_path)
    already = store.completed_ids() if args.resume else set()
    outstanding = [q for q in questions if q.id not in already]

    print(f"Benchmark: {len(questions)} question(s) at depth {depth.value}")
    print(f"  by type      {coverage_summary()}")
    print(f"  budget       {budget.max_tasks} tasks, {budget.max_sources} sources each")
    if already:
        print(f"  already done {len(already)} (resuming; use --no-resume to redo)")
    print(f"  to run       {len(outstanding)}")
    print(f"  results      {results_path}")
    print()

    if args.dry_run:
        # Deliberately concrete about the bill. A run makes roughly one model
        # call per task plus one each for analysis, verification and the report;
        # stated as an order of magnitude rather than a promise, because the
        # research agent's loop is bounded but not fixed.
        per_run = budget.max_tasks + 4
        print("Dry run. Nothing was called and nothing was spent.")
        print(
            f"  rough model calls  ~{per_run * len(outstanding)} "
            f"({per_run} per question x {len(outstanding)})"
        )
        print(f"  strong-tier calls  ~{3 * len(outstanding)} (planner, analyst, reporter)")
        print()
        print("  Run it for real with:  deeptrace evaluate --run")
        return 0

    if not outstanding:
        print("Nothing to run. Every question already has a result.")
    else:

        def announce(_question: object, evaluation: RunEvaluation) -> None:
            mark = "ok " if evaluation.succeeded else "FAIL"
            print(
                f"  [{mark}] {evaluation.question_id:8} "
                f"cite={evaluation.citation_correctness} "
                f"grounded={evaluation.groundedness} "
                f"{evaluation.elapsed_seconds:.0f}s"
            )

        executor = make_executor(depth=depth, settings=settings, max_tasks=args.max_tasks)
        asyncio.run(
            run_benchmark(
                executor,
                questions=outstanding,
                results=store,
                resume=args.resume,
                on_result=announce,
            )
        )

    rows = store.load()
    if not rows:
        print("No results recorded.")
        return 1

    # Rebuilt from the results file rather than from what this process ran, so
    # a resumed benchmark reports the whole suite rather than today's slice.
    evaluations = [RunEvaluation.from_row(row) for row in rows]
    document = render(
        evaluations,
        provenance(depth=depth, settings=settings, questions=questions),
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document, encoding="utf-8")

    summary = aggregate(evaluations)
    print()
    for name, measurement in summary.items():
        print(f"  {name:24} {measurement}")
    print()
    print(f"Wrote {output}")
    return 0


def _pricing(settings: Settings) -> int:
    """Show what every model costs, and which figures are not trustworthy.

    This command was referenced in ``core/llm/pricing.py`` long before it
    existed -- the module docstring told the reader to run it. Documentation
    describing a command nobody wrote is the same class of defect as a field
    that is designed and never wired: it reads as a working feature.

    What it exists to answer is narrow and specific: before a cost figure is
    quoted anywhere, is it based on a price that was verified, and is that
    price still current?
    """
    from datetime import date

    from core.llm.pricing import PRICING, PRICING_LAST_VERIFIED, normalise_model

    print(f"Model pricing  (per 1M tokens, USD; verified {PRICING_LAST_VERIFIED})")
    print()
    print(f"  {'model':28} {'input':>9} {'output':>9} {'cached':>9}  note")

    routed = {
        normalise_model(settings.llm_model_cheap),
        normalise_model(settings.llm_model_strong),
        normalise_model(settings.llm_model_embed),
    }

    for name in sorted(PRICING):
        price = PRICING[name]
        cached = (
            f"{price.cached_input_per_million:>9}"
            if price.cached_input_per_million is not None
            else f"{'-':>9}"
        )
        notes = []
        if name in routed:
            notes.append("routed")
        if price.superseded_on:
            word = "EXPIRED" if price.is_stale() else "changes"
            notes.append(f"{word} {price.superseded_on}")
        print(
            f"  {name:28} {price.input_per_million:>9} "
            f"{price.output_per_million:>9} {cached}  {', '.join(notes)}"
        )

    # The part that matters. A model this deployment actually calls but has no
    # price for reports "unknown" for every run, which is honest and useless --
    # and it is invisible unless something says so.
    unpriced = sorted(name for name in routed if name not in PRICING)
    print()
    if unpriced:
        print("Configured but unpriced -- these runs report cost as unknown:")
        for name in unpriced:
            print(f"  {name}")
        print()
        print("  Add them to core/llm/pricing.py from the provider's pricing page,")
        print("  and update PRICING_LAST_VERIFIED. Never estimate: a guessed price")
        print("  silently corrupts every total that depends on it.")
    else:
        print("Every configured model is priced.")

    stale = sorted(name for name, price in PRICING.items() if price.is_stale())
    if stale:
        print()
        print("Prices past their published end date -- re-verify before quoting:")
        for name in stale:
            print(f"  {name}")

    age = (date.today() - PRICING_LAST_VERIFIED).days
    if age > 90:
        print()
        print(f"Last verified {age} days ago. Providers change pricing; re-check before quoting.")

    return 0


def _users(args: argparse.Namespace) -> int:
    """Create and list accounts, for an operator with a shell but no browser.

    The API's registration endpoint is open, so this is not the only way to make
    an account -- but it is the one that works before anything is deployed, and
    the one that does not consume a rate limit meant for strangers.
    """
    import getpass

    from infrastructure.db.engine import session_scope
    from infrastructure.db.repositories.users import (
        EmailAlreadyRegistered,
        UserRepository,
    )

    async def create(email: str, password: str, name: str | None) -> str:
        from infrastructure.auth.passwords import check_policy, hash_password

        settings = get_settings()
        check_policy(
            password,
            minimum=settings.password_min_length,
            maximum=settings.password_max_length,
        )
        password_hash = await hash_password(password)
        async with session_scope() as session:
            user = await UserRepository(session).create(
                email, password_hash=password_hash, display_name=name
            )
            return user.id

    async def listing() -> list[tuple[str, str, bool]]:
        from sqlalchemy import select

        from infrastructure.db.models import User

        async with session_scope() as session:
            rows = (await session.execute(select(User).order_by(User.created_at))).scalars().all()
            return [(row.id, row.email, row.is_active) for row in rows]

    if args.users_command == "list":
        try:
            accounts = asyncio.run(listing())
        except Exception as exc:
            print(f"could not reach the database: {type(exc).__name__}: {exc}")
            return 1
        if not accounts:
            print("no accounts yet.  create one:  deeptrace users create you@example.com")
            return 0
        for user_id, email, active in accounts:
            print(f"{user_id}  {email}{'' if active else '  (disabled)'}")
        return 0

    # Prompted rather than accepted as an argument. A password on the command
    # line is written to the shell history file and is visible in `ps` to every
    # other user on the machine, for as long as the command runs.
    password = getpass.getpass("password: ")
    if password != getpass.getpass("confirm:  "):
        print("those did not match.")
        return 1

    try:
        user_id = asyncio.run(create(args.email, password, args.name))
    except EmailAlreadyRegistered:
        print(f"an account already exists for {args.email}")
        return 1
    except ValueError as exc:  # the password policy
        print(str(exc))
        return 1
    except Exception as exc:
        print(f"could not reach the database: {type(exc).__name__}: {exc}")
        return 1

    print(f"created {user_id}  {args.email}")
    return 0


def _submit(args: argparse.Namespace) -> int:
    """Queue a job and return. The work happens in a worker."""
    from infrastructure.queue.job import Job

    async def owner_id() -> str | None:
        """Resolve --as to a user id, so the run has somewhere to belong.

        Without it the job has no owner, the run it produces has no owner, and
        it is invisible through the API -- which is correct, and surprising the
        first time it happens from a shell.
        """
        if not args.as_user:
            return None

        from infrastructure.db.engine import session_scope
        from infrastructure.db.repositories.users import UserRepository

        async with session_scope() as session:
            user = await UserRepository(session).by_email(args.as_user)
            if user is None:
                raise LookupError(args.as_user)
            return user.id

    async def execute() -> Job:
        user_id = await owner_id()
        async with _queue() as queue:
            return await queue.enqueue(
                Job(
                    question=args.question,
                    depth=ResearchDepth(args.depth),
                    max_tasks=args.max_tasks,
                    user_id=user_id,
                )
            )

    try:
        job = asyncio.run(execute())
    except LookupError as exc:
        print(f"no account for {exc}.  create one:  deeptrace users create {exc}")
        return 1
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
    subcommands.add_parser("pricing", help="Show model prices and which are unverified")

    evaluate = subcommands.add_parser(
        "evaluate", help="Run the benchmark and write docs/EVALUATION.md"
    )
    evaluate.add_argument(
        "--run",
        dest="dry_run",
        action="store_false",
        help="Actually run it. Without this flag nothing is called and nothing is spent.",
    )
    evaluate.add_argument(
        "--depth",
        choices=[d.value for d in ResearchDepth],
        default=ResearchDepth.QUICK.value,
        help="Budget per question (default: quick -- a benchmark is run repeatedly)",
    )
    evaluate.add_argument(
        "--limit", type=int, default=None, metavar="N", help="Only the first N questions"
    )
    evaluate.add_argument(
        "--questions",
        default=None,
        metavar="IDS",
        help="Comma-separated question ids, e.g. cmp-01,exp-01",
    )
    evaluate.add_argument(
        "--per-type",
        type=int,
        default=None,
        metavar="N",
        help="N questions from each research type, so a subset stays balanced",
    )
    evaluate.add_argument("--max-tasks", type=int, default=None, metavar="N")
    evaluate.add_argument(
        "--no-resume",
        dest="resume",
        action="store_false",
        help="Re-run questions that already have a result, instead of skipping them",
    )
    # data/eval_runs/ is already reserved in .gitignore -- the path was set
    # aside for this milestone before it was built. Writing raw benchmark
    # output anywhere else means committing regenerable measurement data.
    evaluate.add_argument("--results", default="data/eval_runs/results.jsonl")
    # Repo root, not docs/. The roadmap says docs/EVALUATION.md, but docs/ is
    # git-ignored -- so writing there would mean the one artefact that makes
    # this project's quality claims checkable never reaches the repository.
    # Benchmark numbers are the most quotable thing here; they should be the
    # most visible too.
    evaluate.add_argument("--output", default="EVALUATION.md")
    evaluate.set_defaults(dry_run=True, resume=True)

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
    submit.add_argument(
        "--as",
        dest="as_user",
        metavar="EMAIL",
        default=None,
        help="Attribute the run to this account, so it is visible through the API",
    )

    users = subcommands.add_parser("users", help="Create and list accounts")
    user_commands = users.add_subparsers(dest="users_command")
    create_user = user_commands.add_parser("create", help="Create an account")
    create_user.add_argument("email")
    create_user.add_argument("--name", default=None, help="Display name")
    user_commands.add_parser("list", help="List accounts")

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
    if args.command == "pricing":
        return _pricing(settings)
    if args.command == "evaluate":
        return _evaluate(args, settings)
    if args.command == "research":
        return _run_research(args)
    if args.command == "resume":
        return _resume_research(args)
    if args.command == "submit":
        return _submit(args)
    if args.command == "users":
        if not args.users_command:
            parser.parse_args(["users", "--help"])
        return _users(args)
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
