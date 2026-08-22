"""Integration tests for durable workflow checkpoints.

The in-process checkpointer survives exactly as long as the process, which is no
use to the case resumability exists for: a worker that was killed. These tests
run against a real PostgreSQL database and, in the one that matters, against a
second process entirely -- because state that only survives within one event
loop would pass an in-memory test and fail the situation it was built for.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from core.config import ResearchDepth, Settings
from core.graph.nodes import NodeContext
from core.graph.state import ResearchStatus, initial_state
from core.graph.workflow import (
    CheckpointNotFound,
    build_graph,
    load_state,
    resume_workflow,
)
from core.llm.client import LLMClient, ModelRouter
from core.models.plan import ResearchPlan
from core.models.query import QuerySpec
from core.observability.recorder import InMemoryRunRecorder
from core.pipeline import run_research
from core.tools.search import SearchResult
from infrastructure.db.checkpointer import checkpointer_scope, psycopg_url
from tests.fakes import FakeProvider

pytestmark = [pytest.mark.integration]


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


class StubSearch:
    """Counts searches, because "did not pay for this twice" is the assertion."""

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


def make_ctx(*responses: object) -> tuple[NodeContext, StubSearch]:
    recorder = InMemoryRunRecorder()
    client = LLMClient(
        FakeProvider(responses),
        router=ModelRouter("fake", "cheap-model", "strong-model", "embed-model"),
        recorder=recorder,
    )
    search = StubSearch()
    return NodeContext(client=client, search_provider=search, recorder=recorder), search


class WorkerKilled(BaseException):
    """The process dying, rather than a stage failing. Nodes catch Exception."""


@pytest.fixture
def thread_id() -> str:
    """A fresh research id per test.

    The checkpoint tables are LangGraph's own and outlive the transaction the
    other integration tests roll back, so a fixed id would let one test run see
    the previous one's state -- and because the state reducers append, the
    result is a green suite that fails the second time it is run. Unique ids are
    also what production does: a research id is never reused.
    """
    return f"res_{uuid4().hex[:12]}"


@pytest.fixture
async def checkpointer(migrated_database: str) -> Any:
    """A Postgres checkpointer against the test database.

    ``setup()`` inside the scope creates LangGraph's own tables, which Alembic
    deliberately does not manage -- so this fixture also proves that the tables
    a fresh database needs are created by the code that needs them.
    """
    async with checkpointer_scope(url=migrated_database) as saver:
        yield saver


class TestUrlNormalisation:
    def test_the_sqlalchemy_async_dialect_is_stripped(self) -> None:
        """One DATABASE_URL serves both drivers. psycopg cannot read the
        SQLAlchemy dialect string, and fails naming the scheme, not the driver."""
        assert (
            psycopg_url("postgresql+asyncpg://user@localhost:5432/deeptrace")
            == "postgresql://user@localhost:5432/deeptrace"
        )

    def test_a_plain_url_is_left_alone(self) -> None:
        assert psycopg_url("postgresql://localhost/db") == "postgresql://localhost/db"


class TestDurableCheckpoints:
    async def test_a_completed_run_is_readable_afterwards(
        self, checkpointer: Any, thread_id: str
    ) -> None:
        ctx, _ = make_ctx(SPEC, PLAN, QUERIES, SUFFICIENT, EVIDENCE)
        app = build_graph(ctx, checkpointer=checkpointer)
        config: Any = {"configurable": {"thread_id": thread_id}}

        await app.ainvoke(
            initial_state(research_id=thread_id, question="q", depth="quick"),
            config=config,
        )

        state = await load_state(checkpointer, thread_id)
        assert state is not None
        assert state["status"] == ResearchStatus.COMPLETED.value
        assert isinstance(state["spec"], QuerySpec)
        assert isinstance(state["plan"], ResearchPlan)

    async def test_an_unknown_id_has_no_state(self, checkpointer: Any) -> None:
        assert await load_state(checkpointer, "res_never_ran") is None

    async def test_resuming_an_unknown_id_is_refused(self, checkpointer: Any) -> None:
        ctx, _ = make_ctx(SPEC)
        with pytest.raises(CheckpointNotFound):
            await resume_workflow("res_never_ran", ctx=ctx, checkpointer=checkpointer)

    async def test_a_killed_run_resumes_where_it_stopped(
        self, checkpointer: Any, thread_id: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole reason for a durable checkpointer.

        The first attempt dies during extraction, after the searches are paid
        for. The second attempt is a different graph, a different client, and a
        different search provider -- everything the first one held in memory is
        gone. What it recovers, it recovers from the database.
        """
        from core.agents.evidence import EvidenceAgent

        first_ctx, first_search = make_ctx(SPEC, PLAN, QUERIES, SUFFICIENT, EVIDENCE)
        app = build_graph(first_ctx, checkpointer=checkpointer)
        config: Any = {"configurable": {"thread_id": thread_id}}

        async def die(self: EvidenceAgent, *args: object, **kwargs: object) -> object:
            raise WorkerKilled("worker killed")

        monkeypatch.setattr(EvidenceAgent, "extract", die)

        with pytest.raises(WorkerKilled):
            await app.ainvoke(
                initial_state(research_id=thread_id, question="q", depth="quick"),
                config=config,
            )
        assert first_search.calls == 1

        monkeypatch.undo()
        second_ctx, second_search = make_ctx(EVIDENCE)
        final = await resume_workflow(thread_id, ctx=second_ctx, checkpointer=checkpointer)

        assert final["status"] == ResearchStatus.COMPLETED.value
        assert len(final["evidence"]) == 1
        assert second_search.calls == 0, "the resumed run re-ran a paid search"
        assert isinstance(final["spec"], QuerySpec), "the checkpointed spec was not restored"
        assert final["task_results"], "the checkpointed research was not restored"


class TestIdReuse:
    async def test_starting_a_run_under_an_existing_id_is_refused(
        self, checkpointer: Any, thread_id: str
    ) -> None:
        """Found by a test that passed once and failed the second time.

        Invoking an existing thread does not start over -- the reducers append,
        so the second run silently becomes one run holding both runs' sources
        and evidence drawn from the union. Nothing in the output says so.
        """
        from core.graph.workflow import RunAlreadyCheckpointed

        ctx, _ = make_ctx(SPEC, PLAN, QUERIES, SUFFICIENT, EVIDENCE)
        app = build_graph(ctx, checkpointer=checkpointer)
        config: Any = {"configurable": {"thread_id": thread_id}}
        await app.ainvoke(
            initial_state(research_id=thread_id, question="q", depth="quick"),
            config=config,
        )

        with pytest.raises(RunAlreadyCheckpointed) as exc:
            await run_research(
                "q",
                settings=Settings(_env_file=None, google_api_key="k", tavily_api_key="k"),  # type: ignore[call-arg]
                checkpointer=checkpointer,
                research_id=thread_id,
            )

        assert thread_id in str(exc.value)
        assert "resume" in str(exc.value).lower()


class TestConfigurationErrors:
    async def test_a_missing_database_url_names_the_variable(self) -> None:
        from core.config import MissingConfigurationError

        settings = Settings(_env_file=None, database_url=None)  # type: ignore[call-arg]
        with pytest.raises(MissingConfigurationError) as exc:
            async with checkpointer_scope(settings):
                pass

        assert "database_url" in str(exc.value).lower()


class TestDepthSurvives:
    async def test_the_budget_is_read_back_from_the_checkpoint(
        self, checkpointer: Any, thread_id: str
    ) -> None:
        """A resume that took depth from its caller could run a quick run under
        deep budgets, and record it as the depth it was started with."""
        ctx, _ = make_ctx(SPEC, PLAN, QUERIES, SUFFICIENT, EVIDENCE)
        app = build_graph(ctx, checkpointer=checkpointer)
        config: Any = {"configurable": {"thread_id": thread_id}}

        await app.ainvoke(
            initial_state(research_id=thread_id, question="q", depth=ResearchDepth.DEEP.value),
            config=config,
        )

        state = await load_state(checkpointer, thread_id)
        assert state is not None
        assert state["depth"] == ResearchDepth.DEEP.value
