"""Tests for the walking skeleton.

The pipeline itself is thin -- it wires four agents together. What is worth
testing is the wiring: that each stage receives what the previous one produced,
that a failure partway still returns a usable trace, and that a missing
credential is discovered before any money is spent.
"""

from __future__ import annotations

import json

import pytest

from core.config import Settings
from core.models.source import SourceType
from core.observability.recorder import InMemoryRunRecorder
from core.pipeline import ResearchRun, build_search_provider, run_research
from core.tools.base import ToolConfigurationError
from core.tools.search import SearchResult

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
    import core.pipeline as pipeline
    from core.llm.client import LLMClient, ModelRouter
    from tests.fakes import FakeProvider

    recorder = InMemoryRunRecorder()
    responses = [SPEC, PLAN, QUERIES, SUFFICIENT, EVIDENCE]

    def fake_client(settings: object = None, *, recorder: object = None) -> LLMClient:
        return LLMClient(
            FakeProvider(responses),
            router=ModelRouter("fake", "cheap-model", "strong-model", "embed-model"),
            recorder=recorder,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(LLMClient, "from_settings", staticmethod(fake_client))

    def fake_search(settings: object = None) -> StubSearch:
        return StubSearch()

    monkeypatch.setattr(pipeline, "build_search_provider", fake_search)
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
        assert len(run.usage.agent_runs) == 5  # analyse, plan, queries, sufficiency, evidence
        assert run.usage.tool_calls


class TestFailureHandling:
    async def test_a_failure_returns_a_partial_trace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An exception loses everything the run had already established. The
        partial trace is what the report and the trace view read from."""
        import core.pipeline as pipeline

        def explode(settings: object = None) -> object:
            raise ToolConfigurationError("no search key", tool="web_search")

        monkeypatch.setattr(pipeline, "build_search_provider", explode)

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
