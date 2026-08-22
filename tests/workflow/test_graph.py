"""Tests for the LangGraph research workflow.

The graph adds three things the sequential pipeline could not offer: explicit
inspectable state, bounded execution enforced in code, and resumability. Those
are what these tests pin.
"""

from __future__ import annotations

import json

import pytest

from core.graph.nodes import NodeContext
from core.graph.serde import CHECKPOINTED_TYPES, build_serializer
from core.graph.state import ResearchStatus, initial_state, state_summary
from core.graph.workflow import build_graph, make_router, memory_checkpointer, run_workflow
from core.llm.client import LLMClient, ModelRouter
from core.models.plan import ResearchPlan
from core.models.query import QuerySpec
from core.models.source import Source
from core.observability.recorder import InMemoryRunRecorder
from core.tools.search import SearchResult
from tests.fakes import FakeProvider, SchemaRoutedProvider

pytestmark = [pytest.mark.workflow, pytest.mark.unit]


SPEC = json.dumps(
    {
        "normalized_question": "How does Kafka order records?",
        "research_type": "explanation",
        "scope": ["ordering"],
        "out_of_scope": [],
        "constraints": [],
        "ambiguities": [],
        "success_criteria": ["described"],
        "time_sensitivity": "static",
        "requires_current_information": False,
    }
)
PLAN = json.dumps(
    {
        "objective": "Establish Kafka ordering behaviour",
        "tasks": [
            {
                "id": "ordering",
                "question": "How are records ordered in a partition?",
                "priority": "high",
                "dependencies": [],
                "parallelizable": True,
                "source_requirements": ["official_docs"],
            }
        ],
        "completion_criteria": ["documented"],
    }
)
QUERIES = json.dumps({"queries": ["kafka ordering"], "reasoning": "direct"})
SUFFICIENT = json.dumps(
    {
        "verdict": "sufficient",
        "reason": "The documentation states the guarantee directly.",
        "missing_topics": [],
        "confidence": 0.9,
    }
)
PAGE = "Kafka appends records to a partition log in the order they are sent. " * 12
EVIDENCE = json.dumps(
    {
        "evidence": [
            {
                "claim": "Kafka appends records in the order they are sent.",
                "supporting_text": (
                    "Kafka appends records to a partition log in the order they are sent."
                ),
                "location": "Ordering",
                "support_strength": "strong",
            }
        ],
        "injection_observed": False,
    }
)

ANALYSIS = json.dumps(
    {
        "summary": (
            "The sources agree that ordering is guaranteed within a partition "
            "and say nothing about ordering across partitions."
        ),
        "findings": [
            {
                "statement": "Kafka preserves record order within a single partition.",
                "evidence_ids": ["E1"],
                "confidence": "high",
            }
        ],
        "tradeoffs": [],
        "contradictions": [],
        "recommendations": [],
        "open_questions": [],
    }
)

HAPPY_PATH = [SPEC, PLAN, QUERIES, SUFFICIENT, EVIDENCE, ANALYSIS]


class StubSearch:
    def __init__(self) -> None:
        self.calls = 0

    @property
    def name(self) -> str:
        return "stub"

    async def search(
        self, query: str, *, max_results: int = 8, timeout_seconds: float = 30.0
    ) -> list[SearchResult]:
        self.calls += 1
        return [
            SearchResult(
                url="https://kafka.apache.org/docs",
                title="Kafka Documentation",
                content=PAGE,
                provider="stub",
            )
        ]


def make_ctx(
    *responses: object, **client_kwargs: object
) -> tuple[NodeContext, InMemoryRunRecorder, StubSearch]:
    recorder = InMemoryRunRecorder()
    client = LLMClient(
        FakeProvider(responses or HAPPY_PATH),
        router=ModelRouter("fake", "cheap-model", "strong-model", "embed-model"),
        recorder=recorder,
        **client_kwargs,  # type: ignore[arg-type]
    )
    search = StubSearch()
    ctx = NodeContext(client=client, search_provider=search, recorder=recorder)
    return ctx, recorder, search


class TestFullRun:
    async def test_a_run_passes_through_every_node(self) -> None:
        ctx, _, _ = make_ctx()
        final = await run_workflow("How does Kafka order records?", ctx=ctx)

        assert final["status"] == ResearchStatus.COMPLETED.value
        # analyze, plan, dispatch, the task, dispatch again to find no wave
        # left, evidence, analysis, claims.
        assert final["iteration"] == 8
        assert final["spec"] is not None
        assert final["plan"] is not None
        assert len(final["task_results"]) == 1
        assert len(final["evidence"]) == 1

    async def test_state_is_explicit_not_hidden_in_agents(self) -> None:
        """Every intermediate result is readable from the returned state, which
        is what makes a run inspectable rather than inferred from logs."""
        ctx, _, _ = make_ctx()
        final = await run_workflow("q", ctx=ctx)

        assert isinstance(final["spec"], QuerySpec)
        assert isinstance(final["plan"], ResearchPlan)
        assert isinstance(final["sources"][0], Source)

    async def test_the_summary_omits_page_bodies(self) -> None:
        """The full state holds entire page texts; logging it on every
        transition would produce megabytes per run."""
        ctx, _, _ = make_ctx()
        final = await run_workflow("q", ctx=ctx)

        rendered = json.dumps(state_summary(final))
        assert "Kafka appends records" not in rendered
        assert '"sources": 1' in rendered


class TestBoundedExecution:
    def test_the_router_stops_at_the_ceiling(self) -> None:
        """A hard limit in code, not a prompt instruction, so no agent can talk
        its way past it."""
        route = make_router(max_iterations=3)

        assert route({"iteration": 2}) == "continue"  # type: ignore[arg-type]
        assert route({"iteration": 3}) == "stop"  # type: ignore[arg-type]
        assert route({"iteration": 99}) == "stop"  # type: ignore[arg-type]

    def test_the_router_stops_on_error(self) -> None:
        route = make_router(max_iterations=100)
        assert route({"iteration": 1, "error": "boom"}) == "stop"  # type: ignore[arg-type]

    async def test_a_low_ceiling_ends_the_run_early(self) -> None:
        ctx, _, search = make_ctx()
        final = await run_workflow("q", ctx=ctx, max_iterations=2)

        assert final["status"] == ResearchStatus.FAILED.value
        assert search.calls == 0  # never reached the research node

    async def test_a_run_never_ends_in_a_working_status(self) -> None:
        """Stopping early must not leave a status that claims work is ongoing."""
        ctx, _, _ = make_ctx()
        final = await run_workflow("q", ctx=ctx, max_iterations=1)

        assert ResearchStatus(final["status"]).is_terminal


class TestFailureHandling:
    async def test_a_failing_node_records_rather_than_raises(self) -> None:
        """An exception escaping the graph loses the accumulated state, and that
        state is the trace."""
        ctx, _, _ = make_ctx("not valid json", max_repair_attempts=0)
        final = await run_workflow("q", ctx=ctx)

        assert final["status"] == ResearchStatus.FAILED.value
        assert final["error"] is not None
        assert "analyze:" in final["errors"][0]

    async def test_a_failure_stops_later_nodes(self) -> None:
        ctx, _, search = make_ctx("not valid json", max_repair_attempts=0)
        await run_workflow("q", ctx=ctx)

        assert search.calls == 0

    async def test_work_completed_before_a_failure_is_kept(self) -> None:
        """A run that fails at extraction still has its sources, and those cost
        money to obtain."""
        ctx, _, _ = make_ctx(SPEC, PLAN, QUERIES, SUFFICIENT, "not json", max_repair_attempts=0)
        final = await run_workflow("q", ctx=ctx)

        assert final["status"] == ResearchStatus.FAILED.value
        assert len(final["sources"]) == 1
        assert final["plan"] is not None

    async def test_extracting_no_evidence_is_a_failure_not_a_success(self) -> None:
        """Regression test. The evidence agent isolates per-source failures, so
        every source failing yields an empty report rather than an exception --
        and the run would otherwise report "completed" having produced nothing.
        """
        ctx, _, _ = make_ctx(SPEC, PLAN, QUERIES, SUFFICIENT, "not json", max_repair_attempts=0)
        final = await run_workflow("q", ctx=ctx)

        assert final["status"] == ResearchStatus.FAILED.value
        assert final["error"] == "evidence extraction produced no results"

    async def test_a_task_finding_nothing_is_a_gap_not_a_run_failure(self) -> None:
        insufficient = json.dumps(
            {
                "verdict": "not_available",
                "reason": "No public documentation covers this.",
                "missing_topics": [],
                "confidence": 0.8,
            }
        )
        ctx, _, _ = make_ctx(SPEC, PLAN, QUERIES, insufficient, EVIDENCE)
        final = await run_workflow("q", ctx=ctx)

        assert final["status"] == ResearchStatus.COMPLETED.value
        assert any("task ordering" in problem for problem in final["errors"])


class TestInterruptionVersusFailure:
    """A node distinguishes "this run cannot proceed" from "come back later".

    Found live: a Gemini 503 during planning was recorded as a failed run. The
    graph stopped on the error, so nothing was left pending, and resuming the
    run returned the same failure while the question analysis it had already
    paid for sat in the checkpoint unusable.
    """

    async def test_a_transient_provider_error_is_not_swallowed(self) -> None:
        from core.llm.errors import LLMServerError

        ctx, _, _ = make_ctx(SPEC, LLMServerError("503 high demand", provider="google"))

        with pytest.raises(LLMServerError):
            await run_workflow("q", ctx=ctx)

    async def test_a_permanent_error_is_recorded_as_a_failed_run(self) -> None:
        """The other half of the rule. Retrying a rejected schema forever would
        be a loop, not a recovery."""
        from core.llm.errors import LLMBadRequestError

        ctx, _, _ = make_ctx(SPEC, LLMBadRequestError("schema rejected", provider="google"))
        final = await run_workflow("q", ctx=ctx)

        assert final["status"] == ResearchStatus.FAILED.value
        assert "LLMBadRequestError" in str(final["error"])
        assert final["spec"] is not None, "the completed stage was discarded"

    async def test_an_interrupted_step_is_still_owed_after_the_checkpoint(self) -> None:
        """What the distinction buys: the interrupted node is pending, so a
        resume runs it instead of returning the failure again."""
        from core.graph.workflow import resume_workflow
        from core.llm.errors import LLMServerError

        ctx, _, _ = make_ctx(SPEC, LLMServerError("503 high demand", provider="google"))
        saver = memory_checkpointer()
        app = build_graph(ctx, checkpointer=saver)
        config = {"configurable": {"thread_id": "res_503"}}

        with pytest.raises(LLMServerError):
            await app.ainvoke(
                initial_state(research_id="res_503", question="q", depth="quick"),
                config=config,
            )

        assert (await app.aget_state(config)).next == ("plan",)

        recovered, _, _ = make_ctx(PLAN, QUERIES, SUFFICIENT, EVIDENCE)
        final = await resume_workflow("res_503", ctx=recovered, checkpointer=saver)

        assert final["status"] == ResearchStatus.COMPLETED.value
        assert len(final["evidence"]) == 1


class TestExtractionOutcomeInState:
    """A rejection is a finding about the run, so it belongs in the state.

    The evidence and the rejections travel in separate keys because a reducer
    cannot merge a nested report. Both have to arrive, or a run whose passages
    were fabricated reads as a run that found nothing.
    """

    async def test_a_fabricated_passage_is_recorded_as_rejected(self) -> None:
        fabricated = json.dumps(
            {
                "evidence": [
                    {
                        "claim": "Kafka reorders records during compaction.",
                        "supporting_text": "Compaction rewrites the log in timestamp order.",
                        "location": "Compaction",
                        "support_strength": "strong",
                    }
                ],
                "injection_observed": False,
            }
        )
        ctx, _, _ = make_ctx(SPEC, PLAN, QUERIES, SUFFICIENT, fabricated)
        final = await run_workflow("q", ctx=ctx)

        assert final["evidence"] == []
        assert len(final["rejected"]) == 1
        assert final["sources_processed"] == 1
        assert final["status"] == ResearchStatus.FAILED.value

    async def test_rejections_survive_a_checkpoint_round_trip(self) -> None:
        """They are pairs, and msgpack has no tuple of its own. A round trip
        that flattened them would break the reader that unpacks (claim, reason)
        -- and only on resume, never on the run that wrote them."""
        fabricated = json.dumps(
            {
                "evidence": [
                    {
                        "claim": "Kafka reorders records during compaction.",
                        "supporting_text": "Compaction rewrites the log in timestamp order.",
                        "location": "Compaction",
                        "support_strength": "strong",
                    }
                ],
                "injection_observed": False,
            }
        )
        ctx, _, _ = make_ctx(SPEC, PLAN, QUERIES, SUFFICIENT, fabricated)
        saver = memory_checkpointer()
        app = build_graph(ctx, checkpointer=saver)
        config = {"configurable": {"thread_id": "res_rejected"}}

        await app.ainvoke(
            initial_state(research_id="res_rejected", question="q", depth="quick"),
            config=config,
        )
        restored = (await app.aget_state(config)).values

        claim, reason = restored["rejected"][0]
        assert "compaction" in claim.lower()
        assert reason


class TestCheckpointing:
    async def test_state_is_written_after_each_node(self) -> None:
        ctx, _, _ = make_ctx()
        saver = memory_checkpointer()
        app = build_graph(ctx, checkpointer=saver)
        config = {"configurable": {"thread_id": "res_ckpt"}}

        await app.ainvoke(
            initial_state(research_id="res_ckpt", question="q", depth="quick"),
            config=config,
        )
        snapshot = await app.aget_state(config)

        assert snapshot.values["status"] == ResearchStatus.COMPLETED.value
        assert len(snapshot.values["evidence"]) == 1

    async def test_domain_models_survive_a_checkpoint_round_trip(self) -> None:
        """Without the configured serializer these come back as plain dicts, and
        a future library version refuses to load them at all -- so checkpoints
        written today would silently stop resuming after an upgrade."""
        ctx, _, _ = make_ctx()
        saver = memory_checkpointer()
        app = build_graph(ctx, checkpointer=saver)
        config = {"configurable": {"thread_id": "res_types"}}

        await app.ainvoke(
            initial_state(research_id="res_types", question="q", depth="quick"),
            config=config,
        )
        restored = (await app.aget_state(config)).values

        assert isinstance(restored["plan"], ResearchPlan)
        assert isinstance(restored["spec"], QuerySpec)
        assert isinstance(restored["sources"][0], Source)
        assert restored["plan"].tasks[0].id == "ordering"

    def test_every_state_type_is_on_the_allowlist(self) -> None:
        """A type added to the state and forgotten here fails on first resume."""
        listed = {name for _, name in CHECKPOINTED_TYPES}

        for required in ("QuerySpec", "ResearchPlan", "Source", "TaskResult", "Evidence"):
            assert required in listed

    def test_the_serializer_is_built_with_the_allowlist(self) -> None:
        assert build_serializer() is not None


class TestNodeIsolation:
    async def test_a_node_can_be_tested_without_the_graph(self) -> None:
        """State in, partial update out. No graph, no database, no other node."""
        from core.graph.nodes import make_analyze_node

        ctx, _, _ = make_ctx(SPEC)
        node = make_analyze_node(ctx)

        update = await node(initial_state(research_id="r", question="q", depth="quick"))

        assert isinstance(update["spec"], QuerySpec)
        assert update["status"] == ResearchStatus.PLANNING.value
        assert update["iteration"] == 1

    async def test_a_node_returns_only_what_it_changed(self) -> None:
        """Requiring every key would make each node depend on fields it has no
        business knowing about."""
        from core.graph.nodes import make_analyze_node

        ctx, _, _ = make_ctx(SPEC)
        update = await make_analyze_node(ctx)(
            initial_state(research_id="r", question="q", depth="quick")
        )

        assert set(update) == {"spec", "status", "iteration"}


class TestObservability:
    async def test_every_call_carries_the_research_id(self) -> None:
        ctx, recorder, _ = make_ctx()
        final = await run_workflow("q", ctx=ctx, research_id="res_fixed")

        assert final["research_id"] == "res_fixed"
        assert {r.research_id for r in recorder.agent_runs} == {"res_fixed"}


THREE_TASK_PLAN = json.dumps(
    {
        "objective": "Establish Kafka ordering behaviour",
        "tasks": [
            {
                "id": "producer_side",
                "question": "How does a producer preserve order when sending records?",
                "priority": "high",
                "dependencies": [],
                "parallelizable": True,
                "source_requirements": ["official_docs"],
            },
            {
                "id": "broker_side",
                "question": "How does a broker append records to a partition log?",
                "priority": "high",
                "dependencies": [],
                "parallelizable": True,
                "source_requirements": ["official_docs"],
            },
            {
                "id": "consumer_side",
                "question": "How does a consumer read records back in order?",
                "priority": "medium",
                "dependencies": ["producer_side"],
                "parallelizable": True,
                "source_requirements": ["official_docs"],
            },
        ],
        "completion_criteria": ["documented"],
    }
)


class SlowSearch(StubSearch):
    """Records when each search starts and ends, so overlap is observable.

    Concurrency cannot be asserted from a wall-clock total alone -- a fast
    machine makes a sequential run look parallel. Overlapping intervals are the
    actual claim.
    """

    def __init__(self, delay: float = 0.05) -> None:
        super().__init__()
        self.delay = delay
        self.spans: list[tuple[float, float]] = []
        self.peak = 0
        self._live = 0

    async def search(
        self, query: str, *, max_results: int = 8, timeout_seconds: float = 30.0
    ) -> list[SearchResult]:
        import asyncio
        import time

        self._live += 1
        self.peak = max(self.peak, self._live)
        started = time.perf_counter()
        try:
            await asyncio.sleep(self.delay)
            return await super().search(query)
        finally:
            self.spans.append((started, time.perf_counter()))
            self._live -= 1


def make_parallel_ctx(
    *, max_concurrency: int = 5, delay: float = 0.05
) -> tuple[NodeContext, SlowSearch]:
    # Routed by prompt rather than queued in order: three researchers interleave,
    # and a queue would hand the second task's answer to the third -- failing on
    # the double instead of on the code.
    provider = SchemaRoutedProvider(
        {
            "query_analyzer": SPEC,
            "planner": THREE_TASK_PLAN,
            "query_generator": QUERIES,
            "sufficiency_check": SUFFICIENT,
            "evidence_extractor": EVIDENCE,
            "analyst": ANALYSIS,
        }
    )
    recorder = InMemoryRunRecorder()
    client = LLMClient(
        provider,
        router=ModelRouter("fake", "cheap-model", "strong-model", "embed-model"),
        recorder=recorder,
    )
    search = SlowSearch(delay)
    ctx = NodeContext(
        client=client,
        search_provider=search,
        recorder=recorder,
        max_concurrency=max_concurrency,
    )
    return ctx, search


class TestParallelResearch:
    async def test_tasks_in_a_wave_actually_overlap(self) -> None:
        ctx, search = make_parallel_ctx()
        final = await run_workflow("q", ctx=ctx)

        assert len(final["task_results"]) == 3
        assert search.peak > 1, "tasks in the same wave ran one after another"

    async def test_every_task_result_survives_the_merge(self) -> None:
        """What the reducers are for. Last-write-wins would keep one task's
        result and silently discard the rest of the wave."""
        ctx, _ = make_parallel_ctx()
        final = await run_workflow("q", ctx=ctx)

        assert {result.task_id for result in final["task_results"]} == {
            "producer_side",
            "broker_side",
            "consumer_side",
        }
        assert len(final["sources"]) == 3

    async def test_a_dependency_is_researched_after_what_it_depends_on(self) -> None:
        """The plan's waves are executed, not merely recorded. consumer_side
        depends on producer_side, so it cannot start until that one is done."""
        ctx, search = make_parallel_ctx()
        await run_workflow("q", ctx=ctx)

        first_wave = sorted(search.spans)[:2]
        last = sorted(search.spans)[2]
        assert last[0] >= min(end for _, end in first_wave)

    async def test_concurrency_is_bounded(self) -> None:
        """The bound is what protects search quota and memory. Without it a wide
        plan opens as many researchers as it has tasks."""
        ctx, search = make_parallel_ctx(max_concurrency=1)
        final = await run_workflow("q", ctx=ctx)

        assert len(final["task_results"]) == 3
        assert search.peak == 1

    async def test_the_ceiling_leaves_room_for_the_largest_legitimate_run(self) -> None:
        """Fanning out made the step count scale with the plan. A ceiling that
        did not move with it would truncate the biggest runs rather than guard
        against runaway ones -- and would do it silently."""
        from core.config import DEPTH_BUDGETS
        from core.graph.workflow import DEFAULT_MAX_ITERATIONS

        widest = max(budget.max_tasks for budget in DEPTH_BUDGETS.values())

        # analyze + plan + evidence, one dispatch per wave plus one to discover
        # there are none left, and one step per task. A fully serial plan has as
        # many waves as tasks, which is the worst case.
        worst_case = 2 * widest + 4
        assert worst_case <= DEFAULT_MAX_ITERATIONS


class TestAnInterruptedWave:
    """The reason research fans out instead of gathering inside one node.

    A task that finished before a sibling raised keeps its result: LangGraph
    stores a completed task's writes even when the wave as a whole fails, so a
    resume runs only what is still owed. Gathering inside one node would make
    the wave a single unit of work, and one task failing would mean paying for
    every task in it again.

    The guarantee stops at tasks that had finished. A sibling still in flight is
    cancelled when one task raises, so its work is lost and it re-runs. Measured
    rather than assumed -- the first version of this test asserted the stronger
    claim and caught a search being cancelled mid-call.
    """

    async def test_a_task_that_finished_first_is_not_re_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        from core.agents.researcher import ResearchAgent
        from core.graph.workflow import resume_workflow
        from core.llm.errors import LLMServerError

        real_research = ResearchAgent.research
        already_failed = False

        async def flaky(self: ResearchAgent, task: object, **kwargs: object) -> object:
            nonlocal already_failed
            if task.id == "broker_side" and not already_failed:  # type: ignore[attr-defined]
                already_failed = True
                # Long enough that producer_side is finished, not merely
                # started, when this raises. What survives is what completed.
                await asyncio.sleep(0.3)
                raise LLMServerError("503 high demand", provider="google")
            return await real_research(self, task, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(ResearchAgent, "research", flaky)

        ctx, search = make_parallel_ctx()
        saver = memory_checkpointer()
        app = build_graph(ctx, checkpointer=saver)
        config = {"configurable": {"thread_id": "res_wave"}}

        with pytest.raises(LLMServerError):
            await app.ainvoke(
                initial_state(research_id="res_wave", question="q", depth="standard"),
                config=config,
            )

        # producer_side searched and finished; broker_side raised without ever
        # reaching a search.
        assert search.calls == 1

        final = await resume_workflow("res_wave", ctx=ctx, checkpointer=saver)

        assert len(final["task_results"]) == 3
        # broker_side re-run, then consumer_side in the second wave. A fourth
        # search would mean producer_side was researched twice -- work that had
        # already finished and had already been paid for.
        assert search.calls == 3


class TestTheRunBudgetIsARunBudget:
    """``DepthBudget`` says it limits "a research run". It did not.

    The researcher applied ``max_sources`` to each task it was handed, so a
    three-task quick run collected 24 sources against a budget of 8 -- and at
    standard depth, six tasks against a budget of 20 could collect 120. Every
    source collected buys an extraction call, so the ceiling that was supposed
    to be the main cost control multiplied the cost instead.
    """

    async def test_each_task_gets_a_share_rather_than_the_whole_budget(self) -> None:
        from core.config import DEPTH_BUDGETS, ResearchDepth

        seen: list[int] = []
        ctx, _ = make_parallel_ctx()

        from core.agents.researcher import ResearchAgent

        real_research = ResearchAgent.research

        async def capture(self: ResearchAgent, task: object, **kwargs: object) -> object:
            seen.append(kwargs["source_budget"])  # type: ignore[arg-type]
            return await real_research(self, task, **kwargs)  # type: ignore[arg-type]

        ResearchAgent.research = capture  # type: ignore[method-assign]
        try:
            ctx.depth = ResearchDepth.QUICK
            await run_workflow("q", ctx=ctx, depth=ResearchDepth.QUICK)
        finally:
            ResearchAgent.research = real_research  # type: ignore[method-assign]

        budget = DEPTH_BUDGETS[ResearchDepth.QUICK].max_sources
        assert len(seen) == 3
        assert sum(seen) <= budget + len(seen), "the shares add up to more than the run's budget"
        assert all(share >= 1 for share in seen), "a task with no allowance researches nothing"

    async def test_extraction_never_exceeds_the_budget(self) -> None:
        """The belt to the shares' braces: rounding up a share, or a task
        returning more than it was asked for, must not raise the run's cost."""
        from core.config import DEPTH_BUDGETS, ResearchDepth

        ctx, _ = make_parallel_ctx()
        ctx.depth = ResearchDepth.QUICK
        final = await run_workflow("q", ctx=ctx, depth=ResearchDepth.QUICK)

        assert final["sources_processed"] <= DEPTH_BUDGETS[ResearchDepth.QUICK].max_sources
