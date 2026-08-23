"""Integration tests for the HTTP API.

Against the real app with a real database and a real Redis, driven through
ASGI rather than a socket. What is being tested is the contract a client
depends on -- status codes, the error envelope, which fields are present -- and
a mocked repository would test the mock's idea of that contract.

The properties that matter here are not about research. They are about a client
being able to rely on the service: that submitting returns immediately, that a
run can be polled from the moment it is submitted, that failures all look the
same, and that nothing internal leaks out of one.

Every request here is signed in, because every research endpoint requires it.
The fixture registers a real account through the real endpoint rather than
minting a token directly -- a token built by the test would prove the routes
accept tokens the test knows how to make, which is not the same claim.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apps.api.main import create_app
from core.config import ResearchDepth, Settings
from infrastructure.auth.sessions import SessionStore
from infrastructure.queue.redis_queue import PENDING, RedisJobQueue
from infrastructure.rate_limit import RateLimiter

pytestmark = [pytest.mark.integration]

TEST_REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://localhost:6379/15")

TEST_JWT_SECRET = "a-test-signing-key-long-enough-to-pass-validation"


@pytest.fixture
async def anonymous(migrated_database: str) -> AsyncIterator[AsyncClient]:
    """The real application, wired to test infrastructure, with nobody signed in.

    Built through create_app rather than by importing a module-level instance,
    which is what lets this point at test infrastructure instead of whatever the
    environment happens to configure.
    """
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=migrated_database,
        redis_url=TEST_REDIS_URL,
        google_api_key="test-key",
        tavily_api_key="test-key",
        jwt_secret=TEST_JWT_SECRET,
    )
    app = create_app(settings)

    engine = create_async_engine(migrated_database)
    app.state.session_factory = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    queue = RedisJobQueue(RedisJobQueue.from_settings(settings).client, heartbeat_ttl=2)
    await queue.client.flushdb()
    app.state.queue = queue

    # The lifespan is not run here -- the transport speaks ASGI directly -- so
    # everything it would have built has to be built by hand. Missing one is
    # not a silent failure: the dependency reports the service as unavailable,
    # which is what a real outage would look like too.
    app.state.sessions = SessionStore(queue.client)
    app.state.limiter = RateLimiter(queue.client)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    await queue.client.flushdb()
    await queue.close()
    await engine.dispose()


async def register(
    client: AsyncClient, email: str, password: str = "a-long-enough-password"
) -> str:
    """Create an account and leave the client signed in as it. Returns the id."""
    response = await client.post("/auth/register", json={"email": email, "password": password})
    assert response.status_code == 201, response.text
    client.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
    return str((await client.get("/auth/me")).json()["id"])


@pytest.fixture
async def api(anonymous: AsyncClient) -> AsyncClient:
    """The application with a signed-in account. What most tests want.

    A fresh address per test. The Redis flush between tests does not reach
    PostgreSQL, so a fixed address would be registered once and conflict for
    every test after -- and the failure would appear in setup, where it reads
    as a broken fixture rather than as leaked state.
    """
    await register(anonymous, f"owner-{uuid4().hex[:12]}@example.com")
    return anonymous


@pytest.fixture
async def owner_id(api: AsyncClient) -> str:
    """The id of the account the ``api`` fixture is signed in as.

    Runs saved directly into the database have to be attributed to it, or the
    scoped repository behind every endpoint will correctly refuse to show them
    -- which would look like a broken endpoint rather than a working one.
    """
    return str((await api.get("/auth/me")).json()["id"])


class TestSubmitting:
    async def test_a_question_is_accepted_and_returns_immediately(self, api: AsyncClient) -> None:
        """202, not 201. Nothing exists yet except the intention -- a client
        following a Location header would find nothing there."""
        started = time.perf_counter()
        response = await api.post(
            "/research",
            json={"question": "How does Kafka guarantee message ordering?", "depth": "quick"},
        )
        elapsed = time.perf_counter() - started

        assert response.status_code == 202
        body = response.json()
        assert body["research_id"].startswith("res_")
        assert body["job_id"].startswith("job_")
        assert body["status"] == "queued"
        assert elapsed < 0.2, "submitting waited for something it should not have"

    async def test_the_question_reaches_the_queue(self, api: AsyncClient) -> None:
        await api.post(
            "/research",
            json={"question": "How does Kafka guarantee message ordering?"},
        )

        queue = api._transport.app.state.queue  # type: ignore[attr-defined]
        assert await queue.client.llen(PENDING) == 1

    async def test_a_question_that_is_too_short_is_refused(self, api: AsyncClient) -> None:
        response = await api.post("/research", json={"question": "kafka?"})

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request"

    async def test_an_unknown_field_is_refused(self, api: AsyncClient) -> None:
        """extra="forbid" on the request model. A typo in a field name is
        otherwise accepted silently and the value ignored, which looks to the
        caller like the service disagreeing with its own documentation."""
        response = await api.post(
            "/research",
            json={"question": "How does Kafka order records?", "dept": "quick"},
        )

        assert response.status_code == 422

    async def test_a_depth_outside_the_enum_is_refused(self, api: AsyncClient) -> None:
        response = await api.post(
            "/research",
            json={"question": "How does Kafka order records?", "depth": "exhaustive"},
        )

        assert response.status_code == 422


class TestPollingBeforeThereIsAnything:
    async def test_a_submitted_run_can_be_read_immediately(self, api: AsyncClient) -> None:
        """The worker writes the database row at the end of the run. Without a
        fallback to the job, a client polling from the moment it submits would
        get 404s for several minutes."""
        submitted = (
            await api.post(
                "/research",
                json={"question": "How does Kafka guarantee ordering?", "depth": "quick"},
            )
        ).json()

        response = await api.get(f"/research/{submitted['research_id']}")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "queued"
        assert body["job"]["attempts"] == 0
        assert body["sources"] == 0

    async def test_an_unknown_id_is_a_clean_404(self, api: AsyncClient) -> None:
        response = await api.get("/research/res_nothing")

        assert response.status_code == 404
        error = response.json()["error"]
        assert error["code"] == "not_found"
        assert error["details"]["id"] == "res_nothing"


class TestReadingAFinishedRun:
    @pytest.fixture
    async def finished(self, api: AsyncClient, owner_id: str) -> str:
        """A completed run written straight to the database.

        The research itself is not exercised here: what is under test is
        whether the API renders a stored run, and running the real workflow
        would make every assertion depend on a model's mood.
        """
        from core.config import ResearchDepth as Depth
        from core.models.analysis import Analysis, AnalysisReport, Confidence, Finding
        from core.models.claim import build_claims
        from core.models.evidence import (
            Evidence,
            EvidenceExtractionReport,
            QuoteStatus,
            QuoteVerification,
            SupportStrength,
        )
        from core.models.research import SufficiencyVerdict, TaskResult
        from core.models.run import ResearchRun
        from core.models.source import Source, SourceType
        from infrastructure.db.repositories.research import ResearchRepository
        from infrastructure.db.repositories.scope import Viewer

        source = Source(
            id="src_api_1",
            url="https://kafka.apache.org/documentation",
            title="Kafka Documentation",
            domain="kafka.apache.org",
            source_type=SourceType.OFFICIAL_DOCS,
            quality_score=0.97,
            task_id="ordering",
            content="Records are appended in the order they are sent." * 12,
            word_count=90,
        )
        evidence = Evidence(
            id="ev_api_1",
            source_id=source.id,
            task_id="ordering",
            claim="Kafka preserves order within a partition.",
            supporting_text="Records are appended in the order they are sent.",
            location="Ordering guarantees",
            support_strength=SupportStrength.STRONG,
            verification=QuoteVerification(status=QuoteStatus.VERBATIM, similarity=1.0),
            source_quality=0.97,
        )
        run = ResearchRun(
            research_id="res_api_done",
            question="How does Kafka guarantee message ordering?",
            depth=Depth.QUICK,
        )
        run.task_results = [
            TaskResult(
                task_id="ordering",
                question="How are records ordered?",
                sources=[source],
                verdict=SufficiencyVerdict.SUFFICIENT,
                stop_reason="evidence is sufficient",
                rounds=1,
            )
        ]
        run.evidence_report = EvidenceExtractionReport(evidence=[evidence], sources_processed=1)
        analysis = Analysis(
            summary="The evidence describes partition-level ordering guarantees.",
            findings=[
                Finding(
                    statement="Kafka preserves record order within a partition.",
                    evidence_ids=[evidence.id],
                    confidence=Confidence.MODERATE,
                )
            ],
        )
        run.analysis_report = AnalysisReport(analysis=analysis, evidence_considered=1)
        run.claim_set = build_claims(analysis, [evidence], research_id=run.research_id)

        from core.models.report import Citation, Report, ReportSection, SectionKind

        run.report = Report(
            title="Ordering guarantees in Kafka",
            question=run.question,
            sections=[
                ReportSection(
                    kind=SectionKind.SUMMARY,
                    body="Order is preserved within a partition [1].",
                    citation_numbers=[1],
                )
            ],
            citations=[
                Citation(
                    number=1,
                    evidence_id=evidence.id,
                    source_id=source.id,
                    url=source.url,
                    title=source.title,
                    domain=source.domain,
                    quote=evidence.supporting_text,
                )
            ],
        )

        factory = api._transport.app.state.session_factory  # type: ignore[attr-defined]
        async with factory() as session:
            repository = ResearchRepository(session, Viewer.system())
            await repository.save_run(run, user_id=owner_id)
            await session.commit()
        return run.research_id

    async def test_the_detail_endpoint_counts_without_sending_bodies(
        self, api: AsyncClient, finished: str
    ) -> None:
        response = await api.get(f"/research/{finished}")

        body = response.json()
        assert body["status"] == "completed"
        assert body["sources"] == 1
        assert body["evidence"] == 1
        assert body["claims"] == 1
        assert body["has_report"] is True

    async def test_the_report_is_served_both_ways(self, api: AsyncClient, finished: str) -> None:
        response = await api.get(f"/research/{finished}/report")

        body = response.json()
        assert body["markdown"].startswith("# Ordering guarantees")
        assert body["citations"] == 1
        assert body["fully_cited"] is True
        assert body["structured"]["citations"][0]["url"].startswith("https://kafka.apache.org")

    async def test_a_source_is_served_without_its_page(
        self, api: AsyncClient, finished: str
    ) -> None:
        """A page can be tens of kilobytes. Twenty of them would be a megabyte
        sent to answer "where did this come from"."""
        response = await api.get(f"/research/{finished}/sources")

        source = response.json()[0]
        assert source["url"].startswith("https://kafka.apache.org")
        assert "content" not in source

    async def test_evidence_carries_how_it_was_verified(
        self, api: AsyncClient, finished: str
    ) -> None:
        response = await api.get(f"/research/{finished}/evidence")

        item = response.json()[0]
        assert item["quote_status"] == "verbatim"
        assert item["supporting_text"].startswith("Records are appended")

    async def test_claims_carry_their_verdict(self, api: AsyncClient, finished: str) -> None:
        response = await api.get(f"/research/{finished}/claims")

        claim = response.json()[0]
        assert claim["status"] == "proposed"
        assert claim["text"].startswith("Kafka preserves")

    async def test_a_run_with_no_report_says_so_rather_than_returning_an_empty_one(
        self, api: AsyncClient, owner_id: str
    ) -> None:
        """A report that has not been written and one that says nothing are
        different outcomes."""
        from core.models.run import ResearchRun
        from infrastructure.db.repositories.research import ResearchRepository
        from infrastructure.db.repositories.scope import Viewer

        run = ResearchRun(
            research_id="res_api_noreport",
            question="A question whose research failed",
            depth=ResearchDepth.QUICK,
            error="LLMServerError: 503",
        )
        factory = api._transport.app.state.session_factory  # type: ignore[attr-defined]
        async with factory() as session:
            repository = ResearchRepository(session, Viewer.system())
            await repository.save_run(run, user_id=owner_id)
            await session.commit()

        response = await api.get("/research/res_api_noreport/report")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    async def test_history_lists_runs(self, api: AsyncClient, finished: str) -> None:
        response = await api.get("/research?limit=5")

        assert response.status_code == 200
        assert any(row["research_id"] == finished for row in response.json())

    async def test_the_list_limit_is_capped(self, api: AsyncClient) -> None:
        """An unbounded list endpoint is fine until the table is large, and then
        it is the query that takes the service down."""
        response = await api.get("/research?limit=5000")

        assert response.status_code == 422


class TestCancelling:
    async def test_a_queued_run_can_be_cancelled(self, api: AsyncClient) -> None:
        submitted = (
            await api.post(
                "/research",
                json={"question": "How does Kafka guarantee ordering?", "depth": "quick"},
            )
        ).json()

        response = await api.post(f"/research/{submitted['research_id']}/cancel")

        assert response.status_code == 200
        assert response.json()["cancelled"] is True

        queue = api._transport.app.state.queue  # type: ignore[attr-defined]
        job = await queue.get(submitted["job_id"])
        assert job.status.value == "cancelled"

    async def test_cancelling_an_unknown_run_is_a_404(self, api: AsyncClient) -> None:
        response = await api.post("/research/res_nothing/cancel")

        assert response.status_code == 404


class TestTheErrorContract:
    async def test_every_failure_uses_the_same_envelope(self, api: AsyncClient) -> None:
        """Two shapes means a client writes two handlers and gets the third
        one wrong."""
        not_found = await api.get("/research/res_nothing")
        invalid = await api.post("/research", json={"question": "no"})

        for response in (not_found, invalid):
            body = response.json()
            assert set(body) == {"error"}
            assert set(body["error"]) >= {"code", "message", "details"}

    async def test_a_dependency_being_down_is_reported_as_retryable(self) -> None:
        """503, not 500. One is worth retrying and the other is not, and a
        client cannot tell them apart from a message."""
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            google_api_key="k",
            tavily_api_key="k",
        )
        app = create_app(settings)
        app.state.session_factory = None
        app.state.queue = None

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/research", json={"question": "How does Kafka order records?"}
            )

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "unavailable"


class TestHealth:
    async def test_health_reports_each_dependency(self, api: AsyncClient) -> None:
        response = await api.get("/health")

        body = response.json()
        assert response.status_code == 200
        assert body["database"] is True
        assert body["queue"] is True
        assert body["status"] == "ok"

    async def test_health_answers_even_when_a_dependency_is_down(self) -> None:
        """A health endpoint that returns 503 during an incident is one an
        operator cannot read during exactly the incident they need it for."""
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        app = create_app(settings)
        app.state.session_factory = None
        app.state.queue = None

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health")

        assert response.status_code == 200
        assert response.json()["status"] == "degraded"
        assert response.json()["database"] is False


class TestTheDocumentation:
    async def test_the_openapi_schema_describes_every_endpoint(self, api: AsyncClient) -> None:
        response = await api.get("/openapi.json")

        paths = response.json()["paths"]
        assert set(paths) >= {
            "/research",
            "/research/{research_id}",
            "/research/{research_id}/report",
            "/research/{research_id}/claims",
            "/research/{research_id}/evidence",
            "/research/{research_id}/sources",
            "/research/{research_id}/trace",
            "/research/{research_id}/cancel",
            "/health",
        }
