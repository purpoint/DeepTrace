"""The research agent: the first component that uses tools.

    task -> generate queries -> search -> collect sources -> fetch gaps
         -> check sufficiency -> stop, or refine and repeat

The loop is ordinary code, not a model deciding what to do next. Only two
judgements are delegated: how to phrase a search, and whether the material
answers the question. Everything else -- when to stop, what to fetch, what to
discard -- is explicit here.

That matters because an agent that decides its own stop condition can always
argue for one more round. Five bounds apply, and whichever hits first wins:

1. The sufficiency check says the material answers the task.
2. The check says the information is not publicly available, so more searching
   would find nothing.
3. The source budget for this depth is reached.
4. The round limit is reached.
5. A round discovers no new sources, which means the queries have converged and
   further rounds would repeat themselves.

The fifth is the one that is easy to leave out and expensive to omit: without it
a task with a slightly-off framing can burn its entire budget re-finding the
same three pages.
"""

from __future__ import annotations

from core.config import DEPTH_BUDGETS, ResearchDepth
from core.llm.client import LLMClient
from core.logging import get_logger
from core.models.plan import ResearchTask
from core.models.query import QuerySpec
from core.models.research import (
    SearchQueries,
    SufficiencyCheck,
    SufficiencyVerdict,
    TaskResult,
)
from core.models.source import Source, classify_domain, score_source
from core.observability.recorder import RunRecorder, new_run_id
from core.prompts.registry import wrap_untrusted
from core.prompts.researcher import QUERY_GENERATOR_V1, SUFFICIENCY_V1
from core.tools.base import ToolError
from core.tools.fetch import extract_page
from core.tools.search import SearchProvider, SearchResult, canonical_url, web_search

log = get_logger(__name__)

AGENT_NAME = "researcher"

MAX_ROUNDS = 3
QUERIES_PER_ROUND = 3
RESULTS_PER_QUERY = 6

MATERIAL_CHARS_PER_SOURCE = 1200
"""How much of each source is shown to the sufficiency check.

Enough to judge relevance, far less than the whole page. The check answers "is
this on topic and specific enough", which does not need the full text, and
sending fifty complete pages would cost more than the research itself.
"""


class ResearchAgent:
    """Researches one task using search and page retrieval."""

    def __init__(
        self,
        client: LLMClient,
        search_provider: SearchProvider,
        *,
        recorder: RunRecorder | None = None,
        max_rounds: int = MAX_ROUNDS,
    ) -> None:
        self.client = client
        self.search_provider = search_provider
        self.recorder = recorder
        self.max_rounds = max_rounds

    async def research(
        self,
        task: ResearchTask,
        *,
        spec: QuerySpec | None = None,
        depth: ResearchDepth = ResearchDepth.STANDARD,
        research_id: str | None = None,
    ) -> TaskResult:
        """Research one task until a stop condition is met.

        Never raises for research failure. A task that found nothing returns a
        result recording that, because one failed task must not discard the
        successful ones alongside it.
        """
        budget = DEPTH_BUDGETS[depth]
        freshness = spec.freshness_required if spec else False
        objective = spec.normalized_question if spec else task.question

        sources: dict[str, Source] = {}
        queries_used: list[str] = []
        failed: list[tuple[str, str]] = []
        refinement = ""
        check: SufficiencyCheck | None = None
        stop_reason = "round limit reached"
        rounds = 0

        for round_number in range(1, self.max_rounds + 1):
            rounds = round_number
            before = len(sources)

            queries = await self._generate_queries(
                task,
                objective=objective,
                freshness=freshness,
                refinement=refinement,
                research_id=research_id,
            )
            if queries is None:
                stop_reason = "could not generate search queries"
                break

            queries_used.extend(queries.queries)
            await self._run_searches(
                queries.queries,
                task=task,
                sources=sources,
                failed=failed,
                freshness=freshness,
                budget_remaining=budget.max_sources - len(sources),
                research_id=research_id,
            )

            if len(sources) == before:
                stop_reason = "no new sources found; queries have converged"
                break

            if len(sources) >= budget.max_sources:
                stop_reason = f"source budget reached ({budget.max_sources})"
                break

            check = await self._check_sufficiency(
                task,
                sources=list(sources.values()),
                rounds=round_number,
                research_id=research_id,
            )
            if check is None:
                stop_reason = "sufficiency check unavailable; stopping with what was found"
                break

            if check.verdict is SufficiencyVerdict.SUFFICIENT:
                stop_reason = "evidence is sufficient"
                break
            if check.verdict is SufficiencyVerdict.NOT_AVAILABLE:
                stop_reason = "information does not appear to be publicly available"
                break
            if not check.should_continue:
                stop_reason = "insufficient, but no specific gap to search for"
                break

            refinement = (
                "A previous round did not find enough. Target these gaps "
                "specifically:\n" + "\n".join(f"- {topic}" for topic in check.missing_topics)
            )

        result = TaskResult(
            task_id=task.id,
            question=task.question,
            sources=list(sources.values()),
            queries_used=queries_used,
            rounds=rounds,
            verdict=check.verdict if check else SufficiencyVerdict.INSUFFICIENT,
            stop_reason=stop_reason,
            missing_topics=check.missing_topics if check else [],
            failed_urls=failed,
        )

        log.info(
            "research.task_completed",
            research_id=research_id,
            task_id=task.id,
            sources=len(result.sources),
            usable_sources=len(result.usable_sources),
            primary_sources=len(result.primary_sources),
            rounds=rounds,
            verdict=result.verdict.value,
            stop_reason=stop_reason,
            mean_quality=result.mean_quality,
            failed_fetches=len(failed),
        )
        return result

    # -- steps -------------------------------------------------------------

    async def _generate_queries(
        self,
        task: ResearchTask,
        *,
        objective: str,
        freshness: bool,
        refinement: str,
        research_id: str | None,
    ) -> SearchQueries | None:
        """Generate queries, or None if the model could not produce valid ones.

        Returning None rather than raising keeps a single unlucky task from
        failing the whole research run.
        """
        try:
            return await self.client.complete_structured(
                QUERY_GENERATOR_V1,
                SearchQueries,
                {
                    "question": task.question,
                    "objective": objective,
                    "source_requirements": ", ".join(
                        requirement.value for requirement in task.source_requirements
                    ),
                    "freshness": "yes" if freshness else "no",
                    "query_count": QUERIES_PER_ROUND,
                    "refinement": refinement or "(this is the first round)",
                },
                agent=AGENT_NAME,
                research_id=research_id,
                task_id=task.id,
            )
        except Exception as exc:
            log.warning(
                "research.query_generation_failed",
                research_id=research_id,
                task_id=task.id,
                error=str(exc),
            )
            return None

    async def _run_searches(
        self,
        queries: list[str],
        *,
        task: ResearchTask,
        sources: dict[str, Source],
        failed: list[tuple[str, str]],
        freshness: bool,
        budget_remaining: int,
        research_id: str | None,
    ) -> None:
        """Search each query and collect new sources, deduplicated across rounds."""
        for query in queries:
            if budget_remaining <= 0:
                return
            try:
                response = await web_search(
                    query,
                    self.search_provider,
                    max_results=RESULTS_PER_QUERY,
                    recorder=self.recorder,
                    research_id=research_id,
                    task_id=task.id,
                )
            except ToolError as exc:
                log.warning(
                    "research.search_failed",
                    research_id=research_id,
                    task_id=task.id,
                    query=query,
                    error_type=type(exc).__name__,
                )
                continue

            # Blocked URLs are recorded, not dropped, so a gap in the evidence
            # has a cause rather than being an unexplained absence.
            failed.extend(response.blocked)

            for result in response.results:
                key = canonical_url(result.url)
                if key in sources or budget_remaining <= 0:
                    continue
                source = await self._to_source(
                    result,
                    task=task,
                    query=query,
                    freshness=freshness,
                    failed=failed,
                    research_id=research_id,
                )
                if source is not None:
                    sources[key] = source
                    budget_remaining -= 1

    async def _to_source(
        self,
        result: SearchResult,
        *,
        task: ResearchTask,
        query: str,
        freshness: bool,
        failed: list[tuple[str, str]],
        research_id: str | None,
    ) -> Source | None:
        """Turn a search result into a source, fetching the page only if needed.

        Tavily returns extracted content with most results. When that content is
        substantial the page is not fetched again -- a saved request, a saved
        second or two, and one less chance of being blocked by the site.
        """
        source_type = classify_domain(result.url)
        content = result.content
        word_count = len(content.split())
        fetch_failed = False
        fetch_error: str | None = None
        title = result.title

        if not result.has_content:
            try:
                page = await extract_page(
                    result.url,
                    recorder=self.recorder,
                    research_id=research_id,
                    task_id=task.id,
                )
                content = page.text
                word_count = page.word_count
                title = title or page.title
            except ToolError as exc:
                fetch_failed = True
                fetch_error = str(exc)
                failed.append((result.url, fetch_error))

        return Source(
            id=new_run_id("src"),
            url=result.url,
            title=title,
            domain=result.domain,
            source_type=source_type,
            quality_score=score_source(
                result.url, source_type=source_type, freshness_matters=freshness
            ),
            task_id=task.id,
            search_query=query,
            content=content,
            word_count=word_count,
            fetch_failed=fetch_failed,
            fetch_error=fetch_error,
        )

    async def _check_sufficiency(
        self,
        task: ResearchTask,
        *,
        sources: list[Source],
        rounds: int,
        research_id: str | None,
    ) -> SufficiencyCheck | None:
        """Ask whether the material answers the task.

        Source content is wrapped as untrusted before the model sees it. This is
        the first point in the pipeline where attacker-controlled text reaches a
        prompt, so a page instructing the model to declare the research complete
        must be readable as document content rather than as an instruction.
        """
        usable = [source for source in sources if source.has_content]
        if not usable:
            return SufficiencyCheck(
                verdict=SufficiencyVerdict.INSUFFICIENT,
                reason="No sources with retrievable content were found.",
                missing_topics=[task.question],
                confidence=0.9,
            )

        material = "\n\n".join(
            wrap_untrusted(
                f"{source.title}\n{source.content}",
                source=source.domain,
                max_chars=MATERIAL_CHARS_PER_SOURCE,
            )
            for source in usable
        )

        try:
            return await self.client.complete_structured(
                SUFFICIENCY_V1,
                SufficiencyCheck,
                {
                    "question": task.question,
                    "rounds": rounds,
                    "source_count": len(usable),
                    "material": material,
                },
                agent=AGENT_NAME,
                research_id=research_id,
                task_id=task.id,
            )
        except Exception as exc:
            log.warning(
                "research.sufficiency_check_failed",
                research_id=research_id,
                task_id=task.id,
                error=str(exc),
            )
            return None
