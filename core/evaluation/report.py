"""Turning benchmark results into a document, with its own caveats attached.

The rule this file exists to enforce: **a number never appears without what it
was measured on**. A groundedness score means nothing without the model, the
depth budget, the commit, and how many questions actually produced a value --
and the failure mode of leaving those out is not that the number is wrong, but
that nobody can ever tell whether it still is.

So every table here carries its denominator, the header carries the provenance,
and the coverage line says outright how many questions failed. A benchmark that
reports "groundedness 0.94" over the eleven questions that happened to work,
without saying so, is how a figure nobody can reproduce ends up on a slide.
"""

from __future__ import annotations

from typing import Any

from core.evaluation.metrics import Measurement, RunEvaluation, aggregate

HEADINGS = {
    "citation_correctness": "Citation correctness",
    "citation_completeness": "Citation completeness",
    "groundedness": "Groundedness",
    "coverage": "Coverage",
    "verbatim_rate": "Verbatim rate",
    "source_quality": "Source quality",
    "publisher_diversity": "Publisher diversity",
}

MEANING = {
    "citation_correctness": "Quotations that appear on the page they cite, re-verified from stored source text",
    "citation_completeness": "Asserting sections that carry at least one citation",
    "groundedness": "Publishable claims that trace to evidence",
    "coverage": "Declared concepts the specification's scope reached",
    "verbatim_rate": "Evidence matching its source word for word, rather than paraphrased",
    "source_quality": "Mean domain-based quality of the sources used",
    "publisher_diversity": "Distinct publishers per source (1.00 = every source a different site)",
}


def _cell(measurement: Measurement) -> str:
    if not measurement.measured:
        return "not measured"
    return f"{measurement.value:.2f}"


def _stamped(results: list[RunEvaluation], field: str) -> list[str]:
    """The distinct values of one provenance field across the measured runs.

    Read from the rows rather than from the current process, because the rows
    are the only place that knows. A benchmark large enough to need resuming --
    and a free-tier quota of twenty requests a day makes twenty-four questions
    exactly that -- collects its rows over several sittings, and the code has
    moved in between at least once here.
    """
    seen: list[str] = []
    for result in results:
        if not result.succeeded:
            continue
        value = getattr(result, field, "")
        if value and value not in seen:
            seen.append(value)
    return seen


def _provenance_line(
    label: str, results: list[RunEvaluation], field: str, fallback: Any
) -> tuple[str, bool]:
    """One header line, and whether the rows disagreed about it.

    When no row carries a stamp the answer is *unknown*, and the process
    rendering the document is not asked. It knows what it is configured with
    now, which is a different question from what produced rows written days ago
    -- and answering the second with the first is how a report ends up naming
    the wrong model in the one line a reader would check. That is not a
    hypothetical either: rendering this file from a shell without the
    environment override captioned three `gemini-3.5-flash` runs as
    `gemini-3.7-flash`, a model that had not answered a request all day.
    """
    del fallback  # deliberately unused; see above
    values = _stamped(results, field)
    if not values:
        return f"- **{label}** — unknown, not recorded for these runs", False
    if len(values) == 1:
        return f"- **{label}** — `{values[0]}`", False
    rendered = ", ".join(f"`{value}`" for value in values)
    return f"- **{label}** — {len(values)} across these runs: {rendered}", True


def _measured_at(results: list[RunEvaluation], provenance: dict[str, Any]) -> str:
    """When the runs happened, not when the document was written.

    Those coincide only for a suite that finished in one sitting, which a daily
    quota of twenty requests makes impossible here. Rendering the report a week
    later must not restamp week-old numbers as today's.
    """
    stamps = sorted(_stamped(results, "measured_at"))
    if not stamps:
        return str(provenance.get("measured_at", "unknown")) + " (document written; runs undated)"
    if len(stamps) == 1:
        return stamps[0]
    return f"{stamps[0]} to {stamps[-1]}, over {len(stamps)} sittings"


def render(results: list[RunEvaluation], provenance: dict[str, Any]) -> str:
    """Render EVALUATION.md."""
    summary = aggregate(results)
    succeeded = [result for result in results if result.succeeded]
    failed = [result for result in results if not result.succeeded]

    lines: list[str] = [
        "# Evaluation",
        "",
        "Measured, not estimated. Every figure below came from a real run through",
        "the ordinary pipeline; nothing here is projected, rounded up, or carried",
        "over from an earlier configuration.",
        "",
        "## What was measured",
        "",
        f"- **Measured at** — {_measured_at(results, provenance)}",
    ]

    mixed = False
    for label, field, fallback in (
        ("Commit", "commit", provenance.get("commit")),
        ("Cheap tier", "model_cheap", provenance.get("model_cheap")),
        ("Strong tier", "model_strong", provenance.get("model_strong")),
    ):
        line, disagreed = _provenance_line(label, results, field, fallback)
        lines.append(line)
        mixed = mixed or disagreed

    lines += [
        f"- **Depth budget** — {provenance.get('depth', 'unknown')}",
        f"- **Questions attempted** — {len(results)}",
        f"- **Runs that produced a report** — {len(succeeded)}",
        "",
    ]

    if mixed:
        lines += [
            "> **These runs were not all measured on the same thing.** The rows",
            "> above disagree on the commit or the model, because the suite was",
            "> resumed after the code or the configuration moved. Every figure",
            "> below is therefore a mean across configurations, which is not the",
            "> same claim as a benchmark score, and the per-question table is the",
            "> only place the difference is visible.",
            "",
        ]

    if failed:
        lines += [
            f"> {len(failed)} of {len(results)} questions did not produce a report. Every",
            "> average below is over the runs that did, and the count beside each",
            "> figure says how many that was. A mean over the questions that happened",
            "> to work is not the benchmark score unless it says so.",
            "",
        ]

    lines += [
        "## Results",
        "",
        "| Metric | Score | Runs measured | What it means |",
        "|---|---|---|---|",
    ]
    for key, heading in HEADINGS.items():
        measurement = summary[key]
        lines.append(
            f"| {heading} | {_cell(measurement)} | "
            f"{measurement.numerator}/{measurement.denominator} | {MEANING[key]} |"
        )

    # Classification and contradiction are counts rather than means: a boolean
    # averaged into a decimal reads as a quality score when it is a tally.
    classified = [r for r in results if r.classified_correctly is not None]
    if classified:
        correct = sum(1 for r in classified if r.classified_correctly)
        lines += [
            "",
            f"**Question classification** — {correct}/{len(classified)} questions were "
            "classified as the research type the benchmark expected.",
        ]

    contested = [r for r in results if r.surfaced_contradiction is not None]
    if contested:
        surfaced = sum(1 for r in contested if r.surfaced_contradiction)
        lines += [
            "",
            f"**Contradictions surfaced** — {surfaced}/{len(contested)} of the questions "
            "marked contested produced a reported disagreement rather than a single "
            "averaged position.",
        ]

    priced = [r.cost_usd for r in results if r.cost_usd is not None]
    lines += [
        "",
        "## Cost and latency",
        "",
        "- **Total cost** — "
        + (
            f"${sum(priced):.4f} across {len(priced)} runs"
            if len(priced) == len(results) and priced
            else f"not measured for {len(results) - len(priced)} of {len(results)} runs, so no total"
        ),
        f"- **Total tokens** — {sum(r.total_tokens for r in results):,}",
        "- **Mean latency** — "
        + (
            f"{sum(r.elapsed_seconds for r in results) / len(results):.1f}s"
            if results
            else "not measured"
        ),
        "",
        "## Per question",
        "",
        "| Question | Report | Citations | Grounded | Coverage | Sources | Cost | Latency |",
        "|---|---|---|---|---|---|---|---|",
    ]

    for result in results:
        cost = f"${result.cost_usd:.4f}" if result.cost_usd is not None else "unknown"
        lines.append(
            f"| `{result.question_id}` | {'yes' if result.succeeded else 'no'} | "
            f"{_cell(result.citation_correctness)} | {_cell(result.groundedness)} | "
            f"{_cell(result.coverage)} | {result.sources} | {cost} | "
            f"{result.elapsed_seconds:.0f}s |"
        )

    if failed:
        lines += ["", "## Failures", ""]
        for result in failed:
            lines.append(f"- `{result.question_id}` — {result.error or 'no report produced'}")

    lines += [
        "",
        "## What this does not measure",
        "",
        "Nothing here scores whether an answer is correct, insightful, or useful.",
        "Those need a human reader, and a number invented for them would be worse",
        "than their absence -- it would be the exact failure this system exists to",
        "prevent, committed by the thing meant to detect it.",
        "",
        "No metric here asks a model to judge a model. Every figure is computed by",
        "deterministic comparison against what the run stored, for the same reason",
        "quotations are verified by string matching: a judge that shares the",
        "generator's blind spots agrees with it, and the agreement scores well.",
        "",
    ]
    return "\n".join(lines)


__all__ = ["render"]
