"""Tests for the composition root.

The module itself is thin -- it assembles the workflow's dependencies and
translates its state into a run object. What is worth testing is exactly that:
that each stage receives what the previous one produced, that a failure partway
still returns a usable trace, that a missing credential is discovered before any
money is spent, and that a run stopped halfway resumes where it stopped instead
of paying again for work it already did.
"""

from __future__ import annotations

import json

import pytest

from core.config import ResearchDepth, Settings
from core.models.run import ResearchRun
from core.models.source import SourceType
from core.observability.recorder import InMemoryRunRecorder
from core.pipeline import resume_research, run_research
from core.tools.base import ToolConfigurationError
from core.tools.search import SearchResult, build_search_provider

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _hermetic_dns(stub_dns: object) -> None:
    stub_dns()  # type: ignore[operator]


SPEC = json.dumps(
    {
        "normalized_question": "How does Kafka guarantee message ordering?",
        "research_type": "explanation",
        "scope": ["partition ordering"],
        "out_of_scope": [],
        "constraints": [],
        "ambiguities": [],
        "success_criteria": ["Ordering guarantees are described"],
        "time_sensitivity": "static",
        "requires_current_information": False,
    }
)

PLAN = json.dumps(
    {
        "objective": "Establish how Kafka orders records within a partition",
        "tasks": [
            {
                "id": "partition_ordering",
                "question": "How are records ordered within a Kafka partition?",
                "priority": "high",
                "dependencies": [],
                "parallelizable": True,
                "source_requirements": ["official_docs"],
            }
        ],
        "completion_criteria": ["Ordering behaviour is documented"],
    }
)

QUERIES = json.dumps({"queries": ["kafka partition ordering"], "reasoning": "direct"})
SUFFICIENT = json.dumps(
    {
        "verdict": "sufficient",
        "reason": "The documentation states the ordering guarantee directly.",
        "missing_topics": [],
        "confidence": 0.9,
    }
)

# Deliberately over the 50-word usability floor. A shorter page would be treated
# as unusable, and -- worse -- would make the researcher attempt a real fetch to
# retrieve the rest, turning a unit test into a network call.
PAGE = (
    "Kafka provides ordering guarantees at the partition level. Records sent by a "
    "producer to a particular partition are appended in the order they are sent, "
    "and a consumer sees records in the order they are stored in the log. There is "
    "no global ordering guarantee across partitions. Applications that require "
    "total ordering across all records must route them through a single partition, "
    "which limits throughput to a single consumer within each consumer group. "
    "Partition assignment is determined by the record key when one is supplied, so "
    "records sharing a key are placed on the same partition and therefore retain "
    "their relative order."
)
QUOTE = (
    "Records sent by a producer to a particular partition are appended in the order they are sent"
)

EVIDENCE = json.dumps(
    {
        "evidence": [
            {
                "claim": "Kafka preserves record order within a single partition.",
                "supporting_text": QUOTE,
                "location": "Ordering guarantees",
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


VERIFICATION = json.dumps(
    {
        "verdict": "supported",
        "disposition": "pass",
        "reasoning": "The cited passage states the ordering guarantee directly.",
        "supporting_evidence_ids": ["C1"],
        "contradicting_evidence_ids": [],
    }
)


REPORT = json.dumps(
    {
        "title": "Ordering guarantees in Kafka partitions",
        "sections": [
            {
                "kind": "summary",
                "body": "Records are appended to a partition in the order they are sent [1].",
                "claim_ids": [],
            }
        ],
    }
)


class WorkerKilled(BaseException):
    """Stands in for the process being killed.

    Deliberately not an ``Exception``: every node catches those and records them
    as a failed stage, which is a different outcome from a run that was
    interrupted with work still owed.
    """


class StubSearch:
    name = "stub"

    async def search(
        self, query: str, *, max_results: int = 8, timeout_seconds: float = 30.0
    ) -> list[SearchResult]:
        return [
            SearchResult(
                url="https://kafka.apache.org/documentation/",
                title="Kafka Documentation",
                content=PAGE,
                provider="stub",
            )
        ]


def configured(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "google_api_key": "test-key",
        "tavily_api_key": "test-key",
        "llm_provider": "google",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> InMemoryRunRecorder:
    """Replace the provider constructors with stubs, leaving the wiring intact."""
    import core.graph.workflow as workflow
    from core.llm.client import LLMClient, ModelRouter
    from tests.fakes import FakeProvider

    recorder = InMemoryRunRecorder()
    responses = [SPEC, PLAN, QUERIES, SUFFICIENT, EVIDENCE, ANALYSIS, VERIFICATION, REPORT]

    def fake_client(settings: object = None, *, recorder: object = None) -> LLMClient:
        return LLMClient(
            FakeProvider(responses),
            router=ModelRouter("fake", "cheap-model", "strong-model", "embed-model"),
            recorder=recorder,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(LLMClient, "from_settings", staticmethod(fake_client))

    def fake_search(settings: object = None) -> StubSearch:
        return StubSearch()

    monkeypatch.setattr(workflow, "build_search_provider", fake_search)
    return recorder


class TestPipelineWiring:
    async def test_every_stage_produces_output(self, wired: object) -> None:
        run = await run_research("How does Kafka order records?", settings=configured())

        assert run.spec is not None
        assert run.plan is not None
        assert run.task_results
        assert run.evidence_report is not None
        assert run.succeeded

    async def test_evidence_traces_back_to_a_source(self, wired: object) -> None:
        """The chain the whole project promises, exercised end to end."""
        run = await run_research("How does Kafka order records?", settings=configured())

        evidence = run.evidence[0]
        source = next(s for s in run.sources if s.id == evidence.source_id)

        assert evidence.is_verified
        assert source.url.startswith("https://kafka.apache.org")
        assert source.source_type is SourceType.OFFICIAL_DOCS

    async def test_the_plan_drives_which_tasks_run(self, wired: object) -> None:
        run = await run_research("How does Kafka order records?", settings=configured())

        assert [r.task_id for r in run.task_results] == [t.id for t in run.plan.tasks]  # type: ignore[union-attr]

    async def test_max_tasks_limits_the_run(self, wired: object) -> None:
        run = await run_research(
            "How does Kafka order records?", settings=configured(), max_tasks=0
        )
        assert run.task_results == []

    async def test_every_call_shares_one_research_id(self, wired: InMemoryRunRecorder) -> None:
        """The trace is reconstructed by filtering on this one field."""
        run = await run_research("How does Kafka order records?", settings=configured())

        ids = {record.research_id for record in run.usage.agent_runs}
        assert ids == {run.research_id}

    async def test_usage_is_tallied(self, wired: object) -> None:
        run = await run_research("How does Kafka order records?", settings=configured())

        assert run.usage.total_tokens() > 0
        # analyse, plan, queries, sufficiency, evidence, analysis, verification,
        # report
        assert len(run.usage.agent_runs) == 8
        assert run.usage.tool_calls


class TestFailureHandling:
    async def test_a_failure_returns_a_partial_trace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An exception loses everything the run had already established. The
        partial trace is what the report and the trace view read from."""
        import core.graph.workflow as workflow

        def explode(settings: object = None) -> object:
            raise ToolConfigurationError("no search key", tool="web_search")

        monkeypatch.setattr(workflow, "build_search_provider", explode)

        run = await run_research("anything", settings=configured())

        assert isinstance(run, ResearchRun)
        assert run.error is not None
        assert "ToolConfigurationError" in run.error
        assert run.succeeded is False

    async def test_a_missing_search_key_is_found_before_any_model_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Discovering it after the analyzer and planner have run would waste
        two paid calls on a run that cannot proceed."""
        run = await run_research("anything", settings=configured(tavily_api_key=None))

        assert run.error is not None
        assert "TAVILY_API_KEY" in run.error
        assert run.usage.agent_runs == []

    async def test_elapsed_time_is_always_recorded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await run_research("anything", settings=configured(tavily_api_key=None))
        assert run.elapsed_seconds >= 0


class TestSearchProviderConstruction:
    def test_missing_key_names_the_variable_and_where_to_get_one(self) -> None:
        with pytest.raises(ToolConfigurationError) as exc:
            build_search_provider(configured(tavily_api_key=None))

        assert "TAVILY_API_KEY" in str(exc.value)
        assert "tavily.com" in str(exc.value)

    def test_a_configured_key_builds_a_provider(self) -> None:
        assert build_search_provider(configured()).name == "tavily"


class TestExtractionOutcomeSurvivesTheGraph:
    """What extraction rejected has to reach the run object.

    The state carries evidence and rejections in separate keys, because a
    reducer cannot merge a nested report. If the translation back drops one of
    them, a run whose passages were mostly fabricated looks identical to a run
    that simply found little -- and the rejection is the more interesting half.
    """

    async def test_rejections_reach_the_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import core.graph.workflow as workflow
        from core.llm.client import LLMClient, ModelRouter
        from tests.fakes import FakeProvider

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
        responses = [SPEC, PLAN, QUERIES, SUFFICIENT, fabricated]

        def fake_client(settings: object = None, *, recorder: object = None) -> LLMClient:
            return LLMClient(
                FakeProvider(responses),
                router=ModelRouter("fake", "cheap-model", "strong-model", "embed-model"),
                recorder=recorder,  # type: ignore[arg-type]
            )

        monkeypatch.setattr(LLMClient, "from_settings", staticmethod(fake_client))
        monkeypatch.setattr(workflow, "build_search_provider", lambda _settings=None: StubSearch())

        run = await run_research("How does Kafka order records?", settings=configured())

        assert run.evidence == []
        assert run.evidence_report is not None
        assert run.evidence_report.rejected
        assert run.evidence_report.sources_processed == 1
        assert run.succeeded is False

    async def test_a_run_that_never_extracted_has_no_report(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Distinct from extracting nothing. A run that failed while planning
        never reached the stage, and saying it processed zero sources would
        claim an outcome it never produced."""
        import core.graph.workflow as workflow

        monkeypatch.setattr(workflow, "build_search_provider", lambda _settings=None: StubSearch())
        run = await run_research("anything", settings=configured(google_api_key=None))

        assert run.evidence_report is None
        assert run.error is not None


class TestResume:
    """What resuming covers, and what it does not.

    A node that fails records the failure and the graph ends -- there is nothing
    pending, so resuming such a run returns it unchanged rather than retrying the
    stage. What resume is for is the case a failed stage cannot represent: the
    process dying mid-run, which leaves the last completed node checkpointed and
    the next one still owed.
    """

    async def test_a_killed_run_does_not_repeat_completed_work(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reason for a workflow engine at all: a worker killed after the
        research step must not pay for those searches a second time."""
        import core.graph.workflow as workflow
        from core.agents.evidence import EvidenceAgent
        from core.graph.workflow import memory_checkpointer
        from core.llm.client import LLMClient, ModelRouter
        from tests.fakes import FakeProvider

        searches: list[str] = []
        stub = StubSearch()

        async def counting_search(query: str, **kwargs: object) -> list[SearchResult]:
            searches.append(query)
            return await StubSearch.search(stub, query)

        monkeypatch.setattr(stub, "search", counting_search)
        monkeypatch.setattr(workflow, "build_search_provider", lambda _settings=None: stub)

        # One provider across both attempts, so the second picks up where the
        # first stopped rather than replaying the analyzer's response.
        #
        # REPORT included, and it was not before: the run reached the reporter
        # with nothing left to return, the report failed validation, and the
        # `succeeded` assertion below passed anyway because success then meant
        # "collected evidence" rather than "produced a report".
        provider = FakeProvider(
            [SPEC, PLAN, QUERIES, SUFFICIENT, EVIDENCE, ANALYSIS, VERIFICATION, REPORT]
        )

        def fake_client(settings: object = None, *, recorder: object = None) -> LLMClient:
            return LLMClient(
                provider,
                router=ModelRouter("fake", "cheap-model", "strong-model", "embed-model"),
                recorder=recorder,  # type: ignore[arg-type]
            )

        monkeypatch.setattr(LLMClient, "from_settings", staticmethod(fake_client))

        # A BaseException rather than an ordinary exception on purpose: a node
        # catches Exception and turns it into state, which is a run that failed,
        # not a run that was interrupted. This is the process being killed.
        real_extract = EvidenceAgent.extract
        killed = False

        async def die_once(self: EvidenceAgent, *args: object, **kwargs: object) -> object:
            nonlocal killed
            if not killed:
                killed = True
                raise WorkerKilled("worker killed")
            return await real_extract(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(EvidenceAgent, "extract", die_once)
        saver = memory_checkpointer()

        with pytest.raises(WorkerKilled):
            await run_research(
                "How does Kafka order records?",
                settings=configured(),
                checkpointer=saver,
                research_id="res_resume",
            )
        assert len(searches) == 1

        second = await resume_research("res_resume", checkpointer=saver, settings=configured())

        assert second.resumed is True
        assert second.succeeded
        assert second.research_id == "res_resume"
        assert len(searches) == 1, "resuming re-ran a search that was already paid for"
        assert second.spec is not None, "the checkpointed spec was not restored"
        assert second.task_results, "the checkpointed research was not restored"

    async def test_the_task_limit_comes_from_the_checkpoint(
        self, wired: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Found live: a run started with --max-tasks 1 was interrupted, and the
        resume researched all three planned tasks -- the same run, finished
        under limits it never had."""
        from core.graph.workflow import load_state, memory_checkpointer

        saver = memory_checkpointer()
        await run_research(
            "How does Kafka order records?",
            settings=configured(),
            max_tasks=1,
            checkpointer=saver,
            research_id="res_limit",
        )

        state = await load_state(saver, "res_limit")
        assert state is not None
        assert state["max_tasks"] == 1

    async def test_resuming_an_unknown_id_is_refused(self) -> None:
        """Starting a fresh run under an id whose history the caller believes
        exists would be worse than failing."""
        from core.graph.workflow import CheckpointNotFound, memory_checkpointer

        with pytest.raises(CheckpointNotFound) as exc:
            await resume_research("res_nothing", checkpointer=memory_checkpointer())

        assert "res_nothing" in str(exc.value)

    async def test_depth_comes_from_the_checkpoint_not_the_caller(
        self, wired: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A resume that could change the budget would mean the ceilings a run
        executed under are not the ones it is recorded as having used."""
        from core.config import ResearchDepth
        from core.graph.workflow import load_state, memory_checkpointer

        saver = memory_checkpointer()
        await run_research(
            "How does Kafka order records?",
            settings=configured(),
            depth=ResearchDepth.QUICK,
            checkpointer=saver,
            research_id="res_depth",
        )

        state = await load_state(saver, "res_depth")
        assert state is not None
        assert state["depth"] == ResearchDepth.QUICK.value


class TestInterruptionIsNotATraceback:
    """An outage is an expected outcome, not a bug in DeepTrace.

    Both entry points return the run, so a CLI reports "the provider is down,
    resume when it is back" rather than printing a stack trace at a user who
    can do nothing with it.
    """

    async def test_an_interrupted_run_returns_the_partial_trace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import core.graph.workflow as workflow
        from core.graph.workflow import memory_checkpointer
        from core.llm.client import LLMClient, ModelRouter
        from core.llm.errors import LLMServerError
        from tests.fakes import FakeProvider

        provider = FakeProvider([SPEC, LLMServerError("503 high demand", provider="google")])

        def fake_client(settings: object = None, *, recorder: object = None) -> LLMClient:
            return LLMClient(
                provider,
                router=ModelRouter("fake", "cheap-model", "strong-model", "embed-model"),
                recorder=recorder,  # type: ignore[arg-type]
            )

        monkeypatch.setattr(LLMClient, "from_settings", staticmethod(fake_client))
        monkeypatch.setattr(workflow, "build_search_provider", lambda _settings=None: StubSearch())
        saver = memory_checkpointer()

        run = await run_research(
            "How does Kafka order records?",
            settings=configured(),
            checkpointer=saver,
            research_id="res_interrupted",
        )

        assert run.error is not None
        assert "LLMServerError" in run.error
        assert run.spec is not None, "the stage that completed before the outage was lost"

    async def test_an_interrupted_resume_returns_the_run_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Found live: the provider that was down was still down, and the
        second attempt printed a traceback instead of a resumable run."""
        import core.graph.workflow as workflow
        from core.graph.workflow import memory_checkpointer
        from core.llm.client import LLMClient, ModelRouter
        from core.llm.errors import LLMServerError
        from tests.fakes import FakeProvider

        outage = LLMServerError("503 high demand", provider="google")
        provider = FakeProvider([SPEC, outage])

        def fake_client(settings: object = None, *, recorder: object = None) -> LLMClient:
            return LLMClient(
                provider,
                router=ModelRouter("fake", "cheap-model", "strong-model", "embed-model"),
                recorder=recorder,  # type: ignore[arg-type]
            )

        monkeypatch.setattr(LLMClient, "from_settings", staticmethod(fake_client))
        monkeypatch.setattr(workflow, "build_search_provider", lambda _settings=None: StubSearch())
        saver = memory_checkpointer()

        await run_research(
            "How does Kafka order records?",
            settings=configured(),
            checkpointer=saver,
            research_id="res_still_down",
        )
        again = await resume_research("res_still_down", checkpointer=saver, settings=configured())

        assert again.resumed is True
        assert again.error is not None
        assert "LLMServerError" in again.error
        assert again.spec is not None


class TestARunThatProducedNothingSaysSo:
    """The first run of the deployed stack reported `status: completed,
    error: null` having produced no report at all.

    Its analyst failed -- the configured strong model had not answered a
    request in four days -- and a failed analysis is deliberately not a failed
    run: the evidence is collected and verified and worth more than the
    conclusions drawn from it. But two things then went wrong at the reporting
    boundary. Success meant "collected evidence" rather than "produced a
    report", and the graph's record of what failed was dropped on the way out,
    so the run was indistinguishable from one that answered the question.
    """

    @staticmethod
    def _evidence() -> object:
        from core.models.evidence import (
            Evidence,
            QuoteStatus,
            QuoteVerification,
            SupportStrength,
        )

        return Evidence(
            id="ev_1",
            source_id="src_1",
            task_id="t",
            claim="Records are appended in the order they are sent.",
            supporting_text="Records are appended in the order they are sent.",
            support_strength=SupportStrength.STRONG,
            verification=QuoteVerification(status=QuoteStatus.VERBATIM, similarity=1.0),
            source_quality=0.9,
        )

    @staticmethod
    def _report(**kwargs: object) -> object:
        from core.models.report import Report

        fields: dict[str, object] = {"title": "A report", "question": "does it?"}
        fields.update(kwargs)
        return Report(**fields)  # type: ignore[arg-type]

    def _run(self, **kwargs: object) -> ResearchRun:
        from core.models.evidence import EvidenceExtractionReport

        defaults: dict[str, object] = {
            "research_id": "res_x",
            "question": "does it?",
            "depth": ResearchDepth.QUICK,
            "evidence_report": EvidenceExtractionReport(evidence=[self._evidence()]),  # type: ignore[list-item]
        }
        defaults.update(kwargs)
        return ResearchRun(**defaults)  # type: ignore[arg-type]

    def test_evidence_without_a_report_is_not_success(self) -> None:
        assert self._run().succeeded is False

    def test_a_report_is_success(self) -> None:
        assert self._run(report=self._report()).succeeded is True

    def test_a_report_with_nothing_publishable_is_still_success(self) -> None:
        """`Reporter` assembles a "No verified answer" report when every claim
        was rejected. That is the system working -- it did the research and said
        plainly that nothing survived checking -- and calling it a failure would
        punish the honesty the pipeline is built around."""
        empty = self._report(title="No verified answer: does it?", sections=[])

        assert self._run(report=empty).succeeded is True

    def test_the_reason_survives_to_something_a_reader_can_see(self) -> None:
        """The graph accumulates non-fatal failures in `errors`, and
        `run_from_state` used to drop them -- so the one question a reader has
        about a run with no report had no answer anywhere they could reach."""
        from core.graph.result import run_from_state

        state = {
            "research_id": "res_x",
            "question": "does it?",
            "depth": "quick",
            "errors": ["analysis: LLMError: the model never answered"],
        }
        run = run_from_state(state)  # type: ignore[arg-type]

        assert run.problems == ["analysis: LLMError: the model never answered"]
