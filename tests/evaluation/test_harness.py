"""Tests for the benchmark harness.

The harness is not where the science is -- that is in the metrics -- but it is
where a day's quota gets wasted or a bad number gets recorded permanently. Both
of the properties below were found by running it for real and watching it do the
wrong thing.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from core.evaluation.dataset import BENCHMARK
from core.evaluation.harness import BenchmarkResults, run_benchmark
from core.evaluation.metrics import Measurement, RunEvaluation
from core.evaluation.report import render


def a_result(question_id: str, *, succeeded: bool, score: float | None = 1.0) -> RunEvaluation:
    return RunEvaluation(
        question_id=question_id,
        research_id=f"res_{question_id}",
        succeeded=succeeded,
        citation_correctness=Measurement(score, 1, 1) if score is not None else Measurement(None),
    )


class TestResuming:
    def test_a_succeeded_question_is_skipped(self, tmp_path: Path) -> None:
        results = BenchmarkResults(tmp_path / "r.jsonl")
        results.append(a_result("cmp-01", succeeded=True))

        assert results.completed_ids() == {"cmp-01"}

    def test_a_failed_question_is_retried(self, tmp_path: Path) -> None:
        """The one that matters.

        The first real baseline attempt died because the provider returned 503
        for the strong-tier model on three questions in a row. If a resume
        treated those as complete, a Google outage would be recorded as those
        questions' permanent benchmark result.
        """
        results = BenchmarkResults(tmp_path / "r.jsonl")
        results.append(a_result("cmp-01", succeeded=False, score=None))

        assert results.completed_ids() == set()

    def test_failures_can_be_skipped_deliberately(self, tmp_path: Path) -> None:
        """Available, but never the default -- a question that fails
        deterministically should not cost quota on every rerun."""
        results = BenchmarkResults(tmp_path / "r.jsonl")
        results.append(a_result("cmp-01", succeeded=False, score=None))

        assert results.completed_ids(include_failures=True) == {"cmp-01"}

    def test_a_truncated_final_line_does_not_break_reading(self, tmp_path: Path) -> None:
        """A process killed mid-write leaves one bad line. The rest of the file
        is still the record of an hour that was paid for."""
        path = tmp_path / "r.jsonl"
        results = BenchmarkResults(path)
        results.append(a_result("cmp-01", succeeded=True))
        with path.open("a") as handle:
            handle.write('{"question_id": "exp-01", "succ')

        assert results.completed_ids() == {"cmp-01"}
        assert len(results.load()) == 1


class TestDeduplication:
    def test_a_retried_question_appears_once(self, tmp_path: Path) -> None:
        """A rerun appends rather than rewrites. Without deduplication the
        failed attempt is averaged in beside the good one, dragging every mean
        down by an amount nobody could account for."""
        results = BenchmarkResults(tmp_path / "r.jsonl")
        results.append(a_result("cmp-01", succeeded=False, score=None))
        results.append(a_result("cmp-01", succeeded=True, score=1.0))

        rows = results.load()

        assert len(rows) == 1
        assert rows[0]["succeeded"] is True

    def test_a_later_failure_does_not_erase_an_earlier_success(self, tmp_path: Path) -> None:
        """The mirror of `test_a_failed_question_is_retried`, and the bug that
        rule created. Failures are deliberately not treated as complete, so any
        rerun re-attempts them -- and when that rerun was itself rate limited,
        the newest row per question replaced three real measurements with empty
        ones and the report announced "0 of 24 produced a report".

        A failed row carries no metrics. It can never be the more informative of
        the two.
        """
        results = BenchmarkResults(tmp_path / "r.jsonl")
        results.append(a_result("cmp-01", succeeded=True, score=1.0))
        results.append(a_result("cmp-01", succeeded=False, score=None))

        rows = results.load()

        assert len(rows) == 1
        assert rows[0]["succeeded"] is True
        assert rows[0]["citation_correctness"]["value"] == 1.0

    def test_a_question_that_only_ever_failed_keeps_its_failure(self, tmp_path: Path) -> None:
        """Otherwise the report cannot say why the question has no numbers."""
        results = BenchmarkResults(tmp_path / "r.jsonl")
        results.append(a_result("cmp-01", succeeded=False, score=None))

        rows = results.load()

        assert len(rows) == 1
        assert rows[0]["succeeded"] is False


class TestRunning:
    async def test_a_question_that_raises_does_not_stop_the_benchmark(self, tmp_path: Path) -> None:
        """One provider outage should cost one question, not the hour of quota
        already spent on the ones before it."""
        seen: list[str] = []

        async def execute(question: object) -> object:
            seen.append(question.id)  # type: ignore[attr-defined]
            raise RuntimeError("provider is down")

        evaluations = await run_benchmark(
            execute,
            questions=BENCHMARK[:3],
            results=BenchmarkResults(tmp_path / "r.jsonl"),
        )

        assert len(seen) == 3
        assert len(evaluations) == 3
        assert all(not item.succeeded for item in evaluations)
        assert "provider is down" in (evaluations[0].error or "")

    async def test_results_are_written_as_they_finish(self, tmp_path: Path) -> None:
        """Not at the end. A benchmark runs for an hour on a rate-limited
        provider, and a harness that buffers turns one crash into an hour of
        wasted quota."""
        path = tmp_path / "r.jsonl"

        async def execute(question: object) -> object:
            # Whatever is on disk when the second question starts is what a
            # crash at that moment would have preserved.
            if question.id == BENCHMARK[1].id:  # type: ignore[attr-defined]
                assert path.exists() and path.read_text().strip()
            raise RuntimeError("stop")

        await run_benchmark(execute, questions=BENCHMARK[:2], results=BenchmarkResults(path))

        assert len(path.read_text().strip().splitlines()) == 2


class TestTheReportSaysWhatItWasMeasuredOn:
    """Provenance is stamped per row, not per report.

    A twenty-four question suite against a twenty-request daily quota cannot be
    run in one sitting, so its rows arrive over several -- and the code moved
    between two of them here. A single header over all of them asserts a
    uniformity that was never measured.
    """

    def test_one_configuration_is_stated_plainly(self) -> None:
        results = [
            a_result("cmp-01", succeeded=True),
            a_result("cmp-02", succeeded=True),
        ]
        results = [replace(r, commit="abc1234", model_strong="gemini-3.5-flash") for r in results]

        document = render(results, {"commit": "zzz9999", "model_strong": "something-else"})

        assert "- **Commit** — `abc1234`" in document
        assert "zzz9999" not in document

    def test_rows_that_disagree_are_not_averaged_silently(self) -> None:
        results = [
            replace(a_result("cmp-01", succeeded=True), commit="aaa1111"),
            replace(a_result("cmp-02", succeeded=True), commit="bbb2222"),
        ]

        document = render(results, {"commit": "aaa1111"})

        assert "2 across these runs" in document
        assert "`aaa1111`" in document
        assert "`bbb2222`" in document
        assert "were not all measured on the same thing" in document

    def test_a_failed_row_does_not_vote_on_provenance(self) -> None:
        """It produced no number, so it has no configuration to attribute one
        to. Counting it would report a disagreement that no figure depends on."""
        results = [
            replace(a_result("cmp-01", succeeded=True), commit="aaa1111"),
            replace(a_result("cmp-02", succeeded=False, score=None), commit="bbb2222"),
        ]

        document = render(results, {"commit": "aaa1111"})

        assert "- **Commit** — `aaa1111`" in document
        assert "were not all measured on the same thing" not in document
