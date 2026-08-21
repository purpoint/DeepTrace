"""The walking skeleton: the whole research pipeline, wired end to end.

    question -> analyse -> plan -> research each task -> extract evidence

Deliberately the simplest thing that runs the real pipeline. Tasks execute
sequentially, nothing is persisted, and there is no verification stage yet.
Those arrive with the milestones that implement them, and each will replace a
piece of this module rather than being bolted onto it:

    bounded parallel execution   replaces the sequential loop here
    PostgreSQL persistence       replaces returning results in memory
    the LangGraph workflow       replaces this function entirely

Its purpose is to keep the system runnable from the first milestone onward. A
pipeline that has executed end to end, however crudely, fails in ways you can
see. A stack of well-tested layers that have never been run together fails all
at once, at integration, with no history of ever having worked.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from core.agents.evidence import EvidenceAgent, EvidenceExtractionReport
from core.agents.planner import ResearchPlanner
from core.agents.query_analyzer import QueryAnalyzer
from core.agents.researcher import ResearchAgent
from core.config import ResearchDepth, Settings, get_settings
from core.llm.client import LLMClient
from core.logging import bind_research_context, clear_research_context, get_logger
from core.models.evidence import Evidence
from core.models.plan import ResearchPlan
from core.models.query import QuerySpec
from core.models.research import TaskResult
from core.models.source import Source
from core.observability.recorder import (
    InMemoryRunRecorder,
    MultiRunRecorder,
    RunRecorder,
    new_run_id,
)
from core.tools.base import ToolConfigurationError
from core.tools.search import SearchProvider, TavilySearchProvider

log = get_logger(__name__)


@dataclass(slots=True)
class ResearchRun:
    """Everything one end-to-end run produced.

    Holds the intermediate stages, not only the final output. The point of the
    system is that a conclusion can be walked back to what produced it, so the
    plan and the per-task results are part of the result rather than discarded
    once the next stage consumes them.
    """

    research_id: str
    question: str
    depth: ResearchDepth
    spec: QuerySpec | None = None
    plan: ResearchPlan | None = None
    task_results: list[TaskResult] = field(default_factory=list)
    evidence_report: EvidenceExtractionReport | None = None
    elapsed_seconds: float = 0.0
    error: str | None = None
    usage: InMemoryRunRecorder = field(default_factory=InMemoryRunRecorder)
    """Every model and tool call this run made, for the cost and trace summary."""

    @property
    def evidence(self) -> list[Evidence]:
        return self.evidence_report.evidence if self.evidence_report else []

    @property
    def sources(self) -> list[Source]:
        return [source for result in self.task_results for source in result.sources]

    @property
    def succeeded(self) -> bool:
        return self.error is None and bool(self.evidence)


def build_search_provider(settings: Settings | None = None) -> SearchProvider:
    """Construct the configured search provider.

    Mirrors ``build_provider`` in the LLM layer: adding a vendor means adding a
    branch here and a module implementing the protocol, and nothing above this
    boundary changes.
    """
    settings = settings or get_settings()
    if not settings.tavily_api_key:
        raise ToolConfigurationError(
            "No search provider is configured. Add TAVILY_API_KEY to your .env "
            "file (see .env.example). A free key is available at tavily.com.",
            tool="web_search",
        )
    return TavilySearchProvider(api_key=settings.tavily_api_key)


async def run_research(
    question: str,
    *,
    depth: ResearchDepth = ResearchDepth.STANDARD,
    max_tasks: int | None = None,
    settings: Settings | None = None,
    recorder: RunRecorder | None = None,
) -> ResearchRun:
    """Run the full pipeline for one question.

    Args:
        question: The research question.
        depth: Budget ceilings for the run.
        max_tasks: Research only the first N tasks of the plan. Useful for a
            cheap smoke test; the full plan runs when omitted.

    Never raises for research failure. A run that fails partway returns what it
    completed with ``error`` set, because a partial trace is more useful than an
    exception, and this is the object the report and the trace view read from.
    """
    settings = settings or get_settings()
    research_id = new_run_id("res")
    started = time.perf_counter()

    tally = InMemoryRunRecorder()
    recorder = MultiRunRecorder(tally, recorder) if recorder else tally

    run = ResearchRun(research_id=research_id, question=question, depth=depth, usage=tally)
    bind_research_context(research_id=research_id, depth=depth.value)

    try:
        client = LLMClient.from_settings(settings, recorder=recorder)
        search = build_search_provider(settings)

        log.info("research.started", question=question[:200])

        run.spec = await QueryAnalyzer(client).analyze(
            question, depth=depth, research_id=research_id
        )
        run.plan = await ResearchPlanner(client).plan(
            run.spec, depth=depth, research_id=research_id
        )

        # `is not None` rather than a truthiness check: max_tasks=0 means run
        # no tasks, and a falsy test would silently run all of them.
        tasks = run.plan.tasks if max_tasks is None else run.plan.tasks[:max_tasks]
        researcher = ResearchAgent(client, search, recorder=recorder)

        # Sequential on purpose. Bounded parallel execution is its own
        # milestone, and doing it here badly would make the latency improvement
        # it delivers impossible to measure.
        for task in tasks:
            run.task_results.append(
                await researcher.research(task, spec=run.spec, depth=depth, research_id=research_id)
            )

        collected = [source for result in run.task_results for source in result.sources]
        run.evidence_report = await EvidenceAgent(client).extract(
            collected, question=run.spec.normalized_question, research_id=research_id
        )

    except Exception as exc:
        run.error = f"{type(exc).__name__}: {exc}"
        log.warning("research.failed", error_type=type(exc).__name__, error=str(exc))
    finally:
        run.elapsed_seconds = round(time.perf_counter() - started, 2)
        log.info(
            "research.completed",
            elapsed_seconds=run.elapsed_seconds,
            sources=len(run.sources),
            evidence=len(run.evidence),
            total_tokens=tally.total_tokens(),
            succeeded=run.succeeded,
        )
        clear_research_context()

    return run
