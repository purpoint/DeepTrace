"""Running the benchmark.

Two properties matter more than anything else here, and both are about the
numbers being trustworthy afterwards.

**A benchmark run must be interruptible without losing what it paid for.** The
suite makes hundreds of model calls over an hour or more, on a provider that
rate limits and occasionally returns 503. A harness that holds every result in
memory and writes at the end turns one failure into an hour of wasted quota, so
each run is appended to a results file as it finishes, and a resumed benchmark
skips the questions already recorded.

**A partial benchmark must never be reported as a whole one.** Every result
carries whether its run succeeded, and the summary reports how many questions
were actually measured beside every average. Publishing a mean over the eleven
questions that happened to work, labelled as the benchmark score, is how a
number that nobody can reproduce ends up on a slide.

The runs themselves are ordinary research runs through the ordinary pipeline. A
benchmark that exercises a special path measures the special path.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.config import ResearchDepth, Settings, get_settings
from core.evaluation.dataset import BENCHMARK, BenchmarkQuestion
from core.evaluation.metrics import RunEvaluation, evaluate_run
from core.logging import get_logger
from core.models.run import ResearchRun

log = get_logger(__name__)

RunResearch = Callable[[BenchmarkQuestion], Any]
"""How the harness obtains a run. Injected so the harness can be tested without
spending anything -- and so a future comparison can replay stored runs."""


def _encode(value: Any) -> Any:
    """JSON-safe form of a measurement or evaluation."""
    if isinstance(value, dict):
        return {key: _encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return _encode(asdict(value))
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class BenchmarkResults:
    """Results on disk, appended as they are produced.

    JSONL rather than one JSON document, for the same reason the run recorder
    started as JSONL: a process killed halfway through leaves a file that is
    still readable up to the last complete line, where a partially written JSON
    object is a file nothing can open.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def completed_ids(self) -> set[str]:
        """Question ids already recorded, so a resumed run does not pay twice."""
        if not self.path.exists():
            return set()

        done: set[str] = set()
        for line in self.path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                done.add(str(json.loads(line)["question_id"]))
            except (json.JSONDecodeError, KeyError):
                # A truncated final line from a killed process. Skipped rather
                # than fatal: the point of this format is that the rest is
                # still usable.
                continue
        return done

    def append(self, evaluation: RunEvaluation) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_encode(evaluation)) + "\n")

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows = []
        for line in self.path.read_text().splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows


async def run_benchmark(
    execute: RunResearch,
    *,
    questions: Sequence[BenchmarkQuestion] = BENCHMARK,
    results: BenchmarkResults | None = None,
    resume: bool = True,
    on_result: Callable[[BenchmarkQuestion, RunEvaluation], None] | None = None,
) -> list[RunEvaluation]:
    """Run each question and evaluate it, recording as it goes.

    ``execute`` is injected rather than calling the pipeline directly, which is
    what lets the harness be tested against constructed runs without spending
    quota, and what will let a later comparison replay stored runs instead of
    re-buying them.

    A question that raises is recorded as a failed evaluation and the benchmark
    continues. One provider outage should cost one question, not the hour of
    quota already spent on the ones before it.
    """
    already = results.completed_ids() if (results and resume) else set()
    if already:
        log.info("evaluation.resuming", completed=len(already))

    evaluations: list[RunEvaluation] = []
    for question in questions:
        if question.id in already:
            continue

        started = time.perf_counter()
        try:
            run: ResearchRun = await execute(question)
            evaluation = evaluate_run(
                run,
                question_id=question.id,
                concepts=question.concepts,
                expected_type=question.research_type.value,
                contested=question.contested,
            )
        except Exception as exc:
            # Recorded, not raised. The benchmark's own robustness is what makes
            # the numbers affordable to produce.
            log.error(
                "evaluation.question_failed",
                question_id=question.id,
                error_type=type(exc).__name__,
                error=str(exc)[:300],
            )
            evaluation = RunEvaluation(
                question_id=question.id,
                research_id="",
                succeeded=False,
                error=f"{type(exc).__name__}: {exc}"[:500],
                elapsed_seconds=time.perf_counter() - started,
            )

        evaluations.append(evaluation)
        if results is not None:
            results.append(evaluation)
        if on_result is not None:
            on_result(question, evaluation)

    return evaluations


def make_executor(
    *,
    depth: ResearchDepth,
    settings: Settings | None = None,
    max_tasks: int | None = None,
) -> RunResearch:
    """An executor that runs each question through the real pipeline.

    Imported lazily so that importing the harness -- which the dataset tests do
    -- does not pull in the whole research engine.
    """
    settings = settings or get_settings()

    async def execute(question: BenchmarkQuestion) -> ResearchRun:
        from core.pipeline import run_research

        return await run_research(
            question.question,
            depth=depth,
            max_tasks=max_tasks,
            settings=settings,
        )

    return execute


def provenance(
    *,
    depth: ResearchDepth,
    settings: Settings,
    questions: Iterable[BenchmarkQuestion],
) -> dict[str, Any]:
    """What has to be recorded beside the numbers for them to mean anything.

    A score without the model that produced it, the depth budget it ran under,
    and the commit it was measured at is not reproducible and therefore not
    evidence of anything. This is the difference between a benchmark and a
    number in a README.
    """
    import subprocess

    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip()
    except Exception:  # pragma: no cover - git absent
        commit = ""

    return {
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": commit,
        "depth": depth.value,
        "model_cheap": settings.llm_model_cheap,
        "model_strong": settings.llm_model_strong,
        "questions": len(list(questions)),
    }


__all__ = [
    "BenchmarkResults",
    "make_executor",
    "provenance",
    "run_benchmark",
]
