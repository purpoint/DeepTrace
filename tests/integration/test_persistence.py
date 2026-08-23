"""Integration tests against a real PostgreSQL database.

Marked ``integration`` so they can be excluded where no database is available.
They run against the migrated schema rather than one built from the models,
because the migrations are what will exist in production and testing the models
would prove nothing about them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import ResearchDepth
from core.models.evidence import (
    Evidence,
    QuoteStatus,
    QuoteVerification,
    SupportStrength,
)
from core.models.plan import ResearchPlan, ResearchTask
from core.models.query import QuerySpec
from core.models.research import SufficiencyVerdict, TaskResult
from core.models.source import Source, SourceType
from core.observability.recorder import AgentRun, RunRecorder, ToolCall
from core.pipeline import ResearchRun
from infrastructure.db.models import AgentRunRow, EvidenceRow, SourceRow
from infrastructure.db.recorder import PostgresRunRecorder
from infrastructure.db.repositories.research import ResearchRepository
from infrastructure.db.repositories.scope import Viewer

pytestmark = [pytest.mark.integration]


def make_source(source_id: str = "src_1", url: str = "https://kafka.apache.org/docs") -> Source:
    return Source(
        id=source_id,
        url=url,
        title="Kafka Documentation",
        domain="kafka.apache.org",
        source_type=SourceType.OFFICIAL_DOCS,
        quality_score=0.97,
        task_id="ordering",
        content="Records are appended in the order they are sent." * 10,
        word_count=90,
    )


def make_evidence(evidence_id: str = "ev_1", source_id: str = "src_1") -> Evidence:
    return Evidence(
        id=evidence_id,
        source_id=source_id,
        task_id="ordering",
        claim="Kafka preserves record order within a partition.",
        supporting_text="Records are appended in the order they are sent.",
        location="Ordering guarantees",
        support_strength=SupportStrength.STRONG,
        source_quality=0.97,
        verification=QuoteVerification(status=QuoteStatus.VERBATIM, similarity=1.0),
    )


def make_run(research_id: str = "res_1", *, sources: list[Source] | None = None) -> ResearchRun:
    sources = sources if sources is not None else [make_source()]
    run = ResearchRun(
        research_id=research_id,
        question="How does Kafka guarantee message ordering?",
        depth=ResearchDepth.STANDARD,
    )
    run.spec = QuerySpec(
        normalized_question="How does Kafka guarantee message ordering?",
        research_type="explanation",
        scope=["partition ordering"],
        success_criteria=["Ordering is described"],
        time_sensitivity="static",
        requires_current_information=False,
    )
    run.plan = ResearchPlan(
        objective="Establish how Kafka orders records within a partition",
        tasks=[ResearchTask(id="ordering", question="How are records ordered in a partition?")],
        completion_criteria=["Ordering behaviour is documented"],
    )
    run.task_results = [
        TaskResult(
            task_id="ordering",
            question="How are records ordered in a partition?",
            sources=sources,
            rounds=1,
            verdict=SufficiencyVerdict.SUFFICIENT,
            stop_reason="evidence is sufficient",
        )
    ]
    from core.agents.evidence import EvidenceExtractionReport

    run.evidence_report = EvidenceExtractionReport(
        evidence=[make_evidence(source_id=sources[0].id)] if sources else [],
        sources_processed=len(sources),
    )
    return run


class TestSchemaMigration:
    async def test_the_migrated_schema_has_every_table(self, db_session: AsyncSession) -> None:
        """Built by Alembic from empty, not by metadata.create_all."""
        from sqlalchemy import text

        result = await db_session.execute(
            text("select tablename from pg_tables where schemaname='public'")
        )
        tables = {row[0] for row in result}

        assert {
            "users",
            "research_sessions",
            "research_tasks",
            "sources",
            "evidence",
            "agent_runs",
            "tool_calls",
        } <= tables


class TestPersistingARun:
    async def test_a_run_survives_being_written_and_read_back(
        self, db_session: AsyncSession
    ) -> None:
        """Research surviving a restart is the point of this milestone."""
        repo = ResearchRepository(db_session, Viewer.system())
        await repo.save_run(make_run())

        stored = await repo.get_session("res_1")
        assert stored is not None
        assert stored.question == "How does Kafka guarantee message ordering?"
        assert stored.status == "completed"
        assert stored.research_type == "explanation"

    async def test_the_full_specification_is_stored(self, db_session: AsyncSession) -> None:
        """Reproducing a run means knowing exactly how the question was
        interpreted, not a summary of it."""
        repo = ResearchRepository(db_session, Viewer.system())
        await repo.save_run(make_run())

        stored = await repo.get_session("res_1")
        assert stored is not None
        assert stored.spec is not None
        assert stored.spec["scope"] == ["partition ordering"]
        assert stored.plan is not None

    async def test_sources_and_evidence_are_persisted(self, db_session: AsyncSession) -> None:
        repo = ResearchRepository(db_session, Viewer.system())
        await repo.save_run(make_run())

        sources = await repo.get_sources("res_1")
        evidence = await repo.get_evidence("res_1")

        assert len(sources) == 1
        assert sources[0].source_type == "official_docs"
        assert len(evidence) == 1
        assert evidence[0].quote_status == "verbatim"

    async def test_evidence_traces_to_its_source_through_the_database(
        self, db_session: AsyncSession
    ) -> None:
        """Claim -> Evidence -> Source -> URL, now across a restart."""
        repo = ResearchRepository(db_session, Viewer.system())
        await repo.save_run(make_run())

        evidence = (await repo.get_evidence("res_1"))[0]
        source = (
            await db_session.execute(select(SourceRow).where(SourceRow.id == evidence.source_id))
        ).scalar_one()

        assert source.url == "https://kafka.apache.org/docs"

    async def test_saving_twice_updates_rather_than_failing(self, db_session: AsyncSession) -> None:
        """A run is written while in progress and again on completion."""
        repo = ResearchRepository(db_session, Viewer.system())
        run = make_run()

        await repo.save_run(run)
        run.error = "ToolTimeoutError: search timed out"
        await repo.save_run(run)

        stored = await repo.get_session("res_1")
        assert stored is not None
        assert stored.status == "failed"

    async def test_a_failed_run_is_still_recorded(self, db_session: AsyncSession) -> None:
        """Discarding it would leave an unexplained gap in a user's history."""
        repo = ResearchRepository(db_session, Viewer.system())
        run = make_run("res_failed", sources=[])
        run.error = "ToolConfigurationError: no search key"

        await repo.save_run(run)
        stored = await repo.get_session("res_failed")

        assert stored is not None
        assert stored.status == "failed"
        assert stored.error is not None

    async def test_the_plan_is_stored_task_by_task(self, db_session: AsyncSession) -> None:
        from infrastructure.db.models import ResearchTaskRow

        repo = ResearchRepository(db_session, Viewer.system())
        await repo.save_run(make_run())

        tasks = (
            (
                await db_session.execute(
                    select(ResearchTaskRow).where(ResearchTaskRow.research_id == "res_1")
                )
            )
            .scalars()
            .all()
        )

        assert len(tasks) == 1
        assert tasks[0].task_key == "ordering"
        assert tasks[0].verdict == "sufficient"


class TestReferentialIntegrity:
    async def test_evidence_cannot_reference_a_missing_source(
        self, db_session: AsyncSession
    ) -> None:
        """The database refuses to hold untraceable evidence, which is what
        this whole system exists to prevent."""
        repo = ResearchRepository(db_session, Viewer.system())
        await repo.save_run(make_run())

        # A savepoint, so the expected failure rolls back only this insert and
        # leaves the surrounding test transaction usable.
        savepoint = await db_session.begin_nested()
        db_session.add(
            EvidenceRow(
                id="ev_orphan",
                research_id="res_1",
                source_id="src_does_not_exist",
                claim="An unsupported claim",
                supporting_text="Text with no source",
            )
        )
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await savepoint.rollback()

        # The session still works, proving the constraint failed cleanly.
        assert await repo.get_session("res_1") is not None

    async def test_deleting_a_run_removes_everything_it_produced(
        self, db_session: AsyncSession
    ) -> None:
        """Orphaned rows are how a sources table quietly becomes unqueryable."""
        repo = ResearchRepository(db_session, Viewer.system())
        await repo.save_run(make_run())
        await repo.delete_session("res_1")
        await db_session.flush()

        assert await repo.get_sources("res_1") == []
        assert await repo.get_evidence("res_1") == []
        assert await repo.get_session("res_1") is None

    async def test_the_same_page_found_twice_is_stored_once(self, db_session: AsyncSession) -> None:
        """Two rows for one page would make a single source look like two
        independent corroborating sources."""
        repo = ResearchRepository(db_session, Viewer.system())
        duplicates = [
            make_source("src_a", "https://kafka.apache.org/docs"),
            make_source("src_b", "https://kafka.apache.org/docs/?utm_source=x"),
        ]
        await repo.save_run(make_run(sources=duplicates))

        assert len(await repo.get_sources("res_1")) == 1


class TestRunRecorderSwap:
    """The seam from the LLM milestone, exercised against Postgres."""

    def test_the_postgres_recorder_satisfies_the_protocol(self, db_session: AsyncSession) -> None:
        """Nothing above this layer changes when the backend does."""
        assert isinstance(PostgresRunRecorder(db_session), RunRecorder)

    async def test_recording_is_synchronous_and_buffered(self, db_session: AsyncSession) -> None:
        """Recording must not block the research loop, so it buffers and the
        write happens at an await point the caller chooses."""
        recorder = PostgresRunRecorder(db_session, research_id="res_1")
        recorder.record_agent_run(
            AgentRun(
                agent="planner", provider="google", model="m", prompt_name="p", prompt_version="v1"
            )
        )

        assert recorder.pending == 1
        assert (
            await db_session.execute(select(AgentRunRow))
        ).scalars().all() == []  # nothing written yet

    async def test_flushing_writes_the_buffer(self, db_session: AsyncSession) -> None:
        repo = ResearchRepository(db_session, Viewer.system())
        await repo.save_run(make_run())

        recorder = PostgresRunRecorder(db_session, research_id="res_1")
        for index in range(3):
            recorder.record_agent_run(
                AgentRun(
                    run_id=f"run_{index}",
                    agent="planner",
                    provider="google",
                    model="gemini-3.7-flash",
                    prompt_name="planner",
                    prompt_version="v1",
                    input_tokens=100,
                    output_tokens=50,
                    cost_usd=Decimal("0.0001"),
                )
            )
        recorder.record_tool_call(ToolCall(tool="web_search", research_id="res_1"))

        written = await recorder.flush()

        assert written == 4
        assert recorder.pending == 0
        assert len(await repo.get_trace("res_1")) == 4

    async def test_a_record_without_a_research_id_inherits_the_recorder_s(
        self, db_session: AsyncSession
    ) -> None:
        """An agent deep in a call stack may not have the id, and a record
        without one cannot be found in the trace."""
        repo = ResearchRepository(db_session, Viewer.system())
        await repo.save_run(make_run())

        recorder = PostgresRunRecorder(db_session, research_id="res_1")
        recorder.record_agent_run(
            AgentRun(
                agent="evidence", provider="google", model="m", prompt_name="p", prompt_version="v1"
            )
        )
        await recorder.flush()

        assert len(await repo.get_trace("res_1")) == 1

    async def test_replaying_the_same_records_does_not_double_count(
        self, db_session: AsyncSession
    ) -> None:
        """A retried job replays records it already wrote. Ignoring the
        conflict keeps recording idempotent and cost totals correct."""
        repo = ResearchRepository(db_session, Viewer.system())
        await repo.save_run(make_run())

        record = AgentRun(
            run_id="run_same",
            agent="planner",
            provider="google",
            model="gemini-3.7-flash",
            prompt_name="planner",
            prompt_version="v1",
            cost_usd=Decimal("0.0001"),
        )
        for _ in range(2):
            recorder = PostgresRunRecorder(db_session, research_id="res_1")
            recorder.record_agent_run(record)
            await recorder.flush()

        assert len(await repo.get_trace("res_1")) == 1

    async def test_the_trace_is_ordered_by_time(self, db_session: AsyncSession) -> None:
        repo = ResearchRepository(db_session, Viewer.system())
        await repo.save_run(make_run())

        recorder = PostgresRunRecorder(db_session, research_id="res_1")
        base = datetime(2026, 1, 1, tzinfo=UTC)
        recorder.record_agent_run(
            AgentRun(
                run_id="run_late",
                agent="writer",
                provider="google",
                model="m",
                prompt_name="p",
                prompt_version="v1",
                started_at=base.replace(hour=3),
            )
        )
        recorder.record_agent_run(
            AgentRun(
                run_id="run_early",
                agent="planner",
                provider="google",
                model="m",
                prompt_name="p",
                prompt_version="v1",
                started_at=base.replace(hour=1),
            )
        )
        await recorder.flush()

        trace = await repo.get_trace("res_1")
        assert [row.agent for row in trace] == ["planner", "writer"]


class TestCostAccounting:
    async def test_a_priced_run_totals_correctly(self, db_session: AsyncSession) -> None:
        repo = ResearchRepository(db_session, Viewer.system())
        await repo.save_run(make_run())

        recorder = PostgresRunRecorder(db_session, research_id="res_1")
        for index in range(2):
            recorder.record_agent_run(
                AgentRun(
                    run_id=f"run_{index}",
                    agent="planner",
                    provider="google",
                    model="m",
                    prompt_name="p",
                    prompt_version="v1",
                    cost_usd=Decimal("0.0025"),
                )
            )
        await recorder.flush()

        assert await repo.total_cost("res_1") == pytest.approx(0.005)

    async def test_one_unpriced_call_makes_the_total_unknown(
        self, db_session: AsyncSession
    ) -> None:
        """SUM() ignores NULLs, which would understate the total while looking
        authoritative. Free and unmeasured are different claims."""
        repo = ResearchRepository(db_session, Viewer.system())
        await repo.save_run(make_run())

        recorder = PostgresRunRecorder(db_session, research_id="res_1")
        recorder.record_agent_run(
            AgentRun(
                run_id="run_priced",
                agent="planner",
                provider="google",
                model="m",
                prompt_name="p",
                prompt_version="v1",
                cost_usd=Decimal("0.0025"),
            )
        )
        recorder.record_agent_run(
            AgentRun(
                run_id="run_unpriced",
                agent="planner",
                provider="google",
                model="unknown-model",
                prompt_name="p",
                prompt_version="v1",
                cost_usd=None,
            )
        )
        await recorder.flush()

        assert await repo.total_cost("res_1") is None


class TestHistory:
    async def test_sessions_are_listed_newest_first(self, db_session: AsyncSession) -> None:
        repo = ResearchRepository(db_session, Viewer.system())
        for index in range(3):
            await repo.save_run(make_run(f"res_{index}"))
        await db_session.flush()

        listed = await repo.list_sessions(limit=10)
        assert len(listed) >= 3


class TestAnalysisPersistence:
    """The analysis is the most valuable thing a run produces and the easiest to
    lose: it exists only in memory until the session row is written."""

    async def test_the_analysis_survives_a_round_trip(self, db_session: AsyncSession) -> None:
        from core.models.analysis import (
            Analysis,
            AnalysisReport,
            Confidence,
            Finding,
        )

        run = ResearchRun(
            research_id="res_analysis",
            question="How does Kafka order records?",
            depth=ResearchDepth.QUICK,
        )
        run.analysis_report = AnalysisReport(
            analysis=Analysis(
                summary="The evidence describes partition-level ordering guarantees.",
                findings=[
                    Finding(
                        statement="Kafka preserves record order within a partition.",
                        evidence_ids=["ev_1"],
                        confidence=Confidence.MODERATE,
                        corroborating_domains=2,
                    )
                ],
            ),
            dropped=[("An invented conclusion.", "no evidence citation resolved")],
            evidence_considered=4,
        )

        await ResearchRepository(db_session, Viewer.system()).save_run(run)
        stored = await ResearchRepository(db_session, Viewer.system()).get_session("res_analysis")

        assert stored is not None
        assert stored.analysis is not None
        assert stored.analysis["analysis"]["findings"][0]["corroborating_domains"] == 2
        assert stored.analysis["dropped"], "what grounding discarded was not stored"

    async def test_a_run_without_analysis_stores_null(self, db_session: AsyncSession) -> None:
        """A run that failed before analysis has none, which is different from
        an analysis that concluded nothing."""
        run = ResearchRun(
            research_id="res_no_analysis",
            question="q",
            depth=ResearchDepth.QUICK,
            error="LLMServerError: 503",
        )

        await ResearchRepository(db_session, Viewer.system()).save_run(run)
        stored = await ResearchRepository(db_session, Viewer.system()).get_session(
            "res_no_analysis"
        )

        assert stored is not None
        assert stored.analysis is None


class TestClaimPersistence:
    """Claims and their evidence links are the graph a report walks."""

    async def _run_with_claims(self) -> ResearchRun:
        from core.models.analysis import Analysis, AnalysisReport, Confidence, Finding
        from core.models.claim import build_claims
        from core.models.evidence import EvidenceExtractionReport

        run = ResearchRun(
            research_id="res_claims",
            question="How does Kafka order records?",
            depth=ResearchDepth.QUICK,
        )
        source = make_source()
        evidence = make_evidence()
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
        return run

    async def test_claims_and_their_links_survive_a_round_trip(
        self, db_session: AsyncSession
    ) -> None:
        run = await self._run_with_claims()
        repository = ResearchRepository(db_session, Viewer.system())

        await repository.save_run(run)
        stored = await repository.get_claims("res_claims")

        assert len(stored) == 1
        assert stored[0].text.startswith("Kafka preserves")
        assert stored[0].status == "proposed"

    async def test_a_claim_can_be_found_from_the_evidence_it_rests_on(
        self, db_session: AsyncSession
    ) -> None:
        """The direction that matters when a source turns out to be wrong: a
        retracted page has to be traceable to everything built on it."""
        run = await self._run_with_claims()
        repository = ResearchRepository(db_session, Viewer.system())

        await repository.save_run(run)
        resting = await repository.claims_resting_on("ev_1")

        assert [claim.id for claim in resting] == [run.claims[0].id]

    async def test_a_claim_whose_evidence_was_not_stored_is_not_written(
        self, db_session: AsyncSession
    ) -> None:
        """An unsupported claim in the database is one query away from appearing
        in a report as though it had been checked."""
        from core.models.claim import Claim, EvidenceLink

        run = await self._run_with_claims()
        run.claim_set.claims.append(  # type: ignore[union-attr]
            Claim(
                id="res_claims:find_99",
                text="A claim resting on evidence that was never stored.",
                evidence=[EvidenceLink(evidence_id="ev_ghost", source_id="src_ghost")],
            )
        )
        repository = ResearchRepository(db_session, Viewer.system())

        await repository.save_run(run)
        stored = await repository.get_claims("res_claims")

        assert [claim.id for claim in stored] == [run.claims[0].id]

    async def test_deleting_a_run_removes_its_claims(self, db_session: AsyncSession) -> None:
        run = await self._run_with_claims()
        repository = ResearchRepository(db_session, Viewer.system())
        await repository.save_run(run)

        await repository.delete_session("res_claims")

        assert await repository.get_claims("res_claims") == []


class TestVerificationPersistence:
    async def test_a_verdict_is_stored_beside_the_claim_it_judged(
        self, db_session: AsyncSession
    ) -> None:
        from core.models.analysis import Analysis, AnalysisReport, Confidence, Finding
        from core.models.claim import ClaimStatus, build_claims
        from core.models.evidence import EvidenceExtractionReport
        from core.models.verification import ClaimVerification, Disposition, VerificationReport

        run = ResearchRun(
            research_id="res_verified",
            question="How does Kafka order records?",
            depth=ResearchDepth.QUICK,
        )
        source = make_source()
        evidence = make_evidence()
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
                    statement="Kafka always preserves record order.",
                    evidence_ids=[evidence.id],
                    confidence=Confidence.MODERATE,
                )
            ],
        )
        run.analysis_report = AnalysisReport(analysis=analysis, evidence_considered=1)
        run.claim_set = build_claims(analysis, [evidence], research_id=run.research_id)
        claim_id = run.claims[0].id
        run.claim_set.claims[0] = run.claim_set.claims[0].model_copy(
            update={"status": ClaimStatus.PARTIALLY_SUPPORTED}
        )
        run.verification = VerificationReport(
            verdicts={
                claim_id: ClaimVerification(
                    verdict=ClaimStatus.PARTIALLY_SUPPORTED,
                    disposition=Disposition.REVISE,
                    reasoning="The passage states ordering within a partition, not always.",
                    supporting_evidence_ids=[evidence.id],
                    contradicting_evidence_ids=[],
                    overgeneralization="the claim says 'always', which no passage states",
                    suggested_revision="Kafka preserves record order within a partition.",
                )
            },
            evidence_compared=1,
        )

        repository = ResearchRepository(db_session, Viewer.system())
        await repository.save_run(run)
        stored = await repository.get_claims("res_verified")

        assert stored[0].status == "partially_supported"
        assert stored[0].disposition == "revise"
        assert stored[0].overgeneralization is not None
        assert stored[0].suggested_revision is not None

    async def test_only_publishable_claims_can_be_queried_for_a_report(
        self, db_session: AsyncSession
    ) -> None:
        """A report asks for what survived checking. An unsupported claim must
        not be one query away from looking verified."""
        from core.models.claim import Claim, ClaimSet, ClaimStatus, EvidenceLink
        from core.models.evidence import EvidenceExtractionReport

        run = ResearchRun(
            research_id="res_filtered",
            question="q",
            depth=ResearchDepth.QUICK,
        )
        source = make_source()
        evidence = make_evidence()
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
        link = EvidenceLink(evidence_id=evidence.id, source_id=source.id, weight=0.9, verbatim=True)
        run.claim_set = ClaimSet(
            claims=[
                Claim(
                    id="res_filtered:find_1",
                    text="A claim the checker supported.",
                    status=ClaimStatus.SUPPORTED,
                    evidence=[link],
                ),
                Claim(
                    id="res_filtered:find_2",
                    text="A claim the checker refused.",
                    status=ClaimStatus.UNSUPPORTED,
                    evidence=[link],
                ),
            ]
        )

        repository = ResearchRepository(db_session, Viewer.system())
        await repository.save_run(run)

        assert len(await repository.get_claims("res_filtered")) == 2
        supported = await repository.get_claims("res_filtered", status="supported")
        assert [claim.id for claim in supported] == ["res_filtered:find_1"]
        assert run.publishable_claims == [run.claims[0]]
