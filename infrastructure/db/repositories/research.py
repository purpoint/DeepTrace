"""Repository for research sessions and everything they produce.

Agents never touch a database session. They produce domain objects, and this
layer translates those into rows. The separation is what lets the entire
research engine be tested without PostgreSQL running, and it keeps SQL out of
agent code where it would be untestable and easy to get wrong.

Writes are ordered by dependency: the session, then sources, then evidence.
Evidence has a real foreign key to its source, so writing it first would be
rejected -- correctly, because evidence that cannot reach a source is exactly
what the schema refuses to hold.

Every read is scoped to a :class:`Viewer`, supplied when the repository is
built. That is where "user A cannot read user B's research" is actually
enforced: not in a route that remembers to compare two ids, but in the ``WHERE``
clause of each query, so a route that forgets returns nothing rather than
everything. The child tables -- sources, evidence, claims, the trace -- are
filtered by a subquery against their run's owner even though the route already
checked the run, because the whole point of moving the check down here is that
it stops depending on the route having done it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, delete, func, select, true
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from core.logging import get_logger
from core.models.evidence import Evidence
from core.models.report import render_markdown
from core.models.run import ResearchRun
from core.models.source import Source
from core.tools.search import canonical_url
from infrastructure.db.models import (
    AgentRunRow,
    ClaimEvidenceRow,
    ClaimRow,
    EvidenceRow,
    ResearchSession,
    ResearchTaskRow,
    SourceRow,
    ToolCallRow,
)
from infrastructure.db.repositories.scope import Viewer

log = get_logger(__name__)


class ResearchRepository:
    """Persists and loads research runs, on behalf of one viewer."""

    def __init__(self, session: AsyncSession, viewer: Viewer) -> None:
        self.session = session
        self.viewer = viewer

    # -- ownership ---------------------------------------------------------

    def _visible(self) -> list[ColumnElement[bool]]:
        """The ownership predicate for a query over research_sessions.

        A list rather than a single clause so that the system viewer
        contributes nothing at all, instead of contributing ``TRUE`` -- which
        would read, in every query, as though a filter were being applied.
        """
        if self.viewer.is_system:
            return []
        return [ResearchSession.user_id == self.viewer.user_id]

    def _belongs_to_viewer(
        self, research_id_column: InstrumentedAttribute[Any]
    ) -> ColumnElement[bool]:
        """True when a child row's run is one this viewer may see.

        An EXISTS against the parent, correlated on the run id. It costs one
        indexed lookup, and it is what makes ``get_evidence`` safe to call with
        an arbitrary id from a URL: the answer to "give me the evidence for
        someone else's run" is an empty list, decided by PostgreSQL rather than
        by whoever wrote the endpoint.
        """
        if self.viewer.is_system:
            return true()
        return (
            select(ResearchSession.id)
            .where(
                ResearchSession.id == research_id_column,
                ResearchSession.user_id == self.viewer.user_id,
            )
            .exists()
        )

    # -- writing -----------------------------------------------------------

    async def save_run(self, run: ResearchRun, *, user_id: str | None = None) -> None:
        """Persist a completed or failed run.

        A failed run is saved too. It is the record of what was attempted, and
        discarding it would leave a user's history with an unexplained gap where
        a research request used to be.

        ``user_id`` is who the run belongs to. A system viewer -- the worker,
        saving on behalf of whoever queued the job -- passes it explicitly; any
        other viewer can only write runs it owns, so its own id is used and the
        argument is ignored. Letting a request-scoped repository choose an
        arbitrary owner would be a way to write rows into someone else's
        history.
        """
        owner = user_id if self.viewer.is_system else self.viewer.user_id
        await self._upsert_session(run, user_id=owner)
        await self._save_tasks(run)
        sources = await self._save_sources(run)
        kept = await self._save_evidence(run, known_sources=sources)
        await self._save_claims(run, known_evidence=kept)
        await self.session.flush()

        log.info(
            "research.persisted",
            research_id=run.research_id,
            sources=len(sources),
            evidence=len(run.evidence),
            findings=len(run.analysis.findings) if run.analysis else 0,
            claims=len(run.claims),
            failed=run.error is not None,
        )

    async def _upsert_session(self, run: ResearchRun, *, user_id: str | None) -> None:
        """Insert or update the session row.

        An upsert rather than an insert because a run is written once while in
        progress and again when it completes, and the second write must not
        fail on a duplicate key.
        """
        now = datetime.now(UTC)
        values: dict[str, Any] = {
            "id": run.research_id,
            "user_id": user_id,
            "question": run.question,
            "normalized_question": run.spec.normalized_question if run.spec else None,
            "research_type": run.spec.research_type.value if run.spec else None,
            "depth": run.depth.value,
            "status": "failed" if run.error else ("completed" if run.succeeded else "partial"),
            "error": run.error,
            "spec": run.spec.model_dump(mode="json") if run.spec else None,
            "plan": run.plan.model_dump(mode="json") if run.plan else None,
            "analysis": (
                run.analysis_report.model_dump(mode="json") if run.analysis_report else None
            ),
            "report": run.report.model_dump(mode="json") if run.report else None,
            "report_markdown": render_markdown(run.report) if run.report else None,
            "completed_at": now,
        }
        statement = insert(ResearchSession).values(values)
        updates: dict[str, Any] = {
            key: statement.excluded[key] for key in values if key not in ("id", "created_at")
        }
        # A run is written once while in progress and again when it finishes,
        # and the second write may come from a context that does not know who
        # owns it. Taking the incoming value unconditionally would let that
        # second write set user_id back to NULL, which does not fail, does not
        # log, and makes the run disappear from the history of the person who
        # asked for it. COALESCE keeps an owner once one is known.
        updates["user_id"] = func.coalesce(
            statement.excluded.user_id, ResearchSession.__table__.c.user_id
        )
        await self.session.execute(
            statement.on_conflict_do_update(index_elements=["id"], set_=updates)
        )

    async def _save_tasks(self, run: ResearchRun) -> None:
        if not run.plan:
            return

        results = {result.task_id: result for result in run.task_results}
        rows = []
        for task in run.plan.tasks:
            result = results.get(task.id)
            rows.append(
                {
                    "id": f"{run.research_id}:{task.id}",
                    "research_id": run.research_id,
                    "task_key": task.id,
                    "question": task.question,
                    "priority": task.priority.value,
                    "parallelizable": task.parallelizable,
                    "dependencies": {"depends_on": task.dependencies},
                    "status": "completed" if result else "skipped",
                    "verdict": result.verdict.value if result else None,
                    "stop_reason": result.stop_reason if result else None,
                    "rounds": result.rounds if result else 0,
                    "completed_at": datetime.now(UTC) if result else None,
                }
            )

        statement = insert(ResearchTaskRow).values(rows)
        await self.session.execute(
            statement.on_conflict_do_update(
                index_elements=["id"],
                set_={
                    key: statement.excluded[key]
                    for key in rows[0]
                    if key not in ("id", "research_id")
                },
            )
        )

    async def _save_sources(self, run: ResearchRun) -> list[Source]:
        """Write sources, collapsing any that are the same page.

        A source can be discovered by two tasks in one run. The in-memory
        deduplication is per task, so the same page can arrive here twice with
        different ids -- and inserting both would make a single page look like
        two independent corroborating sources, which is exactly the kind of
        false weight the evidence layer must not be given.
        """
        seen: dict[str, Source] = {}
        for source in run.sources:
            seen.setdefault(canonical_url(source.url), source)

        sources = list(seen.values())
        if not sources:
            return []

        rows = [
            {
                "id": source.id,
                "research_id": run.research_id,
                "task_id": source.task_id,
                "url": source.url,
                "canonical_url": canonical_url(source.url),
                "title": source.title,
                "domain": source.domain,
                "source_type": source.source_type.value,
                "quality_score": source.quality_score,
                "content": source.content,
                "word_count": source.word_count,
                "search_query": source.search_query,
                "fetch_failed": source.fetch_failed,
                "fetch_error": source.fetch_error,
                "published_at": source.published_at,
                "retrieved_at": source.retrieved_at,
            }
            for source in sources
        ]
        await self.session.execute(
            insert(SourceRow).values(rows).on_conflict_do_nothing(index_elements=["id"])
        )
        return sources

    async def _save_evidence(self, run: ResearchRun, *, known_sources: list[Source]) -> set[str]:
        """Write evidence, dropping any whose source was not persisted.

        Deduplicating sources can leave a piece of evidence pointing at an id
        that was collapsed away. The foreign key would reject it, so it is
        filtered here with a warning rather than failing the whole save -- and
        it is logged rather than dropped quietly, because losing evidence
        silently is the failure this project is built to avoid.
        """
        if not run.evidence:
            return set()

        valid_ids = {source.id for source in known_sources}
        keepable: list[Evidence] = []
        for item in run.evidence:
            if item.source_id in valid_ids:
                keepable.append(item)
            else:
                log.warning(
                    "research.evidence_orphaned",
                    research_id=run.research_id,
                    evidence_id=item.id,
                    source_id=item.source_id,
                )

        if not keepable:
            return set()

        rows = [
            {
                "id": item.id,
                "research_id": run.research_id,
                "source_id": item.source_id,
                "task_id": item.task_id,
                "claim": item.claim,
                "supporting_text": item.supporting_text,
                "location": item.location,
                "support_strength": item.support_strength.value,
                "quote_status": item.verification.status.value if item.verification else "verbatim",
                "quote_similarity": item.verification.similarity if item.verification else 1.0,
                "source_quality": item.source_quality,
                "weight": item.weight,
                "extracted_at": item.extracted_at,
            }
            for item in keepable
        ]
        await self.session.execute(
            insert(EvidenceRow).values(rows).on_conflict_do_nothing(index_elements=["id"])
        )
        return {item.id for item in keepable}

    async def _save_claims(self, run: ResearchRun, *, known_evidence: set[str]) -> None:
        """Write claims and their links to evidence.

        Links are filtered to evidence that was actually written, for the same
        reason evidence is filtered to persisted sources: the foreign key would
        reject the row, and a claim whose support cannot be reached is exactly
        what this schema refuses to hold.

        A claim left with no links is dropped and logged rather than stored
        unsupported. An unsupported claim in the database is one query away from
        appearing in a report as though it were checked.
        """
        if not run.claims:
            return

        verdicts = run.verification.verdicts if run.verification else {}
        rows: list[dict[str, Any]] = []
        link_rows: list[dict[str, Any]] = []
        for claim in run.claims:
            links = [link for link in claim.evidence if link.evidence_id in known_evidence]
            if not links:
                log.warning(
                    "research.claim_unsupported",
                    research_id=run.research_id,
                    claim_id=claim.id,
                    evidence=len(claim.evidence),
                )
                continue

            verdict = verdicts.get(claim.id)
            rows.append(
                {
                    "id": claim.id,
                    "research_id": run.research_id,
                    "text": claim.text,
                    "kind": claim.kind.value,
                    "status": claim.status.value,
                    "confidence": claim.confidence.value,
                    "condition": claim.condition,
                    "merged_from": claim.merged_from,
                    "strength": claim.strength,
                    "conflicts_with": {"claims": claim.conflicts_with},
                    "disposition": verdict.disposition.value if verdict else None,
                    "verification_reasoning": verdict.reasoning if verdict else None,
                    "overgeneralization": verdict.overgeneralization if verdict else None,
                    "suggested_revision": verdict.suggested_revision if verdict else None,
                    "follow_up_question": verdict.follow_up_question if verdict else None,
                    "contradicted_by": {
                        "evidence": verdict.contradicting_evidence_ids if verdict else []
                    },
                }
            )
            link_rows.extend(
                {
                    "claim_id": claim.id,
                    "evidence_id": link.evidence_id,
                    "source_id": link.source_id,
                    "weight": link.weight,
                    "verbatim": link.verbatim,
                }
                for link in links
            )

        if not rows:
            return

        statement = insert(ClaimRow).values(rows)
        await self.session.execute(
            statement.on_conflict_do_update(
                index_elements=["id"],
                set_={
                    key: statement.excluded[key]
                    for key in rows[0]
                    if key not in ("id", "research_id", "created_at")
                },
            )
        )
        await self.session.execute(
            insert(ClaimEvidenceRow)
            .values(link_rows)
            .on_conflict_do_nothing(index_elements=["claim_id", "evidence_id"])
        )

    # -- reading -----------------------------------------------------------

    async def get_session(self, research_id: str) -> ResearchSession | None:
        """One run, if this viewer may see it.

        A run owned by someone else returns ``None``, indistinguishable from a
        run that does not exist -- which is what the caller should turn into a
        404. A 403 would confirm the id is real, and an id a stranger can
        confirm is an id a stranger can enumerate.
        """
        result = await self.session.execute(
            select(ResearchSession).where(ResearchSession.id == research_id, *self._visible())
        )
        return result.scalar_one_or_none()

    async def list_sessions(self, *, limit: int = 20) -> list[ResearchSession]:
        """This viewer's research history, newest first.

        The user filter used to be an optional argument, which meant the default
        behaviour of this method was to list everyone's. It now comes from the
        viewer, so there is no call that accidentally omits it.
        """
        query = (
            select(ResearchSession)
            .where(*self._visible())
            .order_by(ResearchSession.created_at.desc())
            .limit(limit)
        )
        return list((await self.session.execute(query)).scalars().all())

    async def get_sources(self, research_id: str) -> list[SourceRow]:
        result = await self.session.execute(
            select(SourceRow)
            .where(
                SourceRow.research_id == research_id,
                self._belongs_to_viewer(SourceRow.research_id),
            )
            .order_by(SourceRow.quality_score.desc())
        )
        return list(result.scalars().all())

    async def get_evidence(self, research_id: str) -> list[EvidenceRow]:
        """Evidence for a run, strongest first."""
        result = await self.session.execute(
            select(EvidenceRow)
            .where(
                EvidenceRow.research_id == research_id,
                self._belongs_to_viewer(EvidenceRow.research_id),
            )
            .order_by(EvidenceRow.weight.desc())
        )
        return list(result.scalars().all())

    async def get_claims(self, research_id: str, *, status: str | None = None) -> list[ClaimRow]:
        """Claims for a run, strongest first.

        The status filter is what a report uses: it publishes what survived
        verification and states plainly that the rest did not.
        """
        query = (
            select(ClaimRow)
            .where(
                ClaimRow.research_id == research_id,
                self._belongs_to_viewer(ClaimRow.research_id),
            )
            .order_by(ClaimRow.strength.desc())
        )
        if status is not None:
            query = query.where(ClaimRow.status == status)
        return list((await self.session.execute(query)).scalars().all())

    async def claims_resting_on(self, evidence_id: str) -> list[ClaimRow]:
        """Every claim built on one piece of evidence.

        The direction that matters when a source turns out to be wrong. Without
        the join table this question could only be answered by loading every
        claim and inspecting it.
        """
        result = await self.session.execute(
            select(ClaimRow)
            .join(ClaimEvidenceRow, ClaimEvidenceRow.claim_id == ClaimRow.id)
            .where(
                ClaimEvidenceRow.evidence_id == evidence_id,
                self._belongs_to_viewer(ClaimRow.research_id),
            )
            .order_by(ClaimRow.strength.desc())
        )
        return list(result.scalars().all())

    async def get_trace(self, research_id: str) -> list[AgentRunRow | ToolCallRow]:
        """Every model and tool call for a run, in the order they happened.

        This is what the trace view reads. Merging the two tables in Python
        rather than in SQL keeps the query simple and the row count is bounded
        by the run's own budget.
        """
        runs = (
            (
                await self.session.execute(
                    select(AgentRunRow).where(
                        AgentRunRow.research_id == research_id,
                        self._belongs_to_viewer(AgentRunRow.research_id),
                    )
                )
            )
            .scalars()
            .all()
        )
        calls = (
            (
                await self.session.execute(
                    select(ToolCallRow).where(
                        ToolCallRow.research_id == research_id,
                        self._belongs_to_viewer(ToolCallRow.research_id),
                    )
                )
            )
            .scalars()
            .all()
        )

        merged: list[AgentRunRow | ToolCallRow] = [*runs, *calls]
        return sorted(merged, key=lambda row: row.started_at)

    async def total_cost(self, research_id: str) -> float | None:
        """Total cost of a run, or None if any call had unknown pricing.

        SUM() over a column with NULLs ignores them, which would understate the
        total while looking authoritative. Counting the NULLs separately is what
        keeps "free" distinguishable from "unmeasured".
        """
        result = await self.session.execute(
            select(
                func.sum(AgentRunRow.cost_usd),
                func.count(AgentRunRow.run_id).filter(AgentRunRow.cost_usd.is_(None)),
            ).where(
                AgentRunRow.research_id == research_id,
                self._belongs_to_viewer(AgentRunRow.research_id),
            )
        )
        total, unpriced = result.one()
        if unpriced or total is None:
            return None
        return float(total)

    async def cost_breakdown(self, research_id: str) -> list[dict[str, Any]]:
        """What each agent spent on one run: calls, tokens, and cost.

        Grouped in SQL rather than by loading the trace and summing in Python.
        The trace is bounded by the depth budget so either would work today, but
        the aggregate is the whole answer here and shipping the rows to compute
        it is work done twice.

        ``unpriced`` is counted per group and returned alongside the sum, for
        the same reason ``total_cost`` counts it: SUM() skips NULLs, so a group
        containing one unpriced call produces a total that looks authoritative
        and understates. A caller that can see the count can say "at least
        this much" instead of stating a number it cannot support.
        """
        result = await self.session.execute(
            select(
                AgentRunRow.agent,
                AgentRunRow.model,
                func.count(AgentRunRow.run_id).label("calls"),
                func.coalesce(func.sum(AgentRunRow.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(AgentRunRow.output_tokens), 0).label("output_tokens"),
                func.coalesce(func.sum(AgentRunRow.latency_ms), 0.0).label("latency_ms"),
                func.sum(AgentRunRow.cost_usd).label("cost_usd"),
                func.count(AgentRunRow.run_id)
                .filter(AgentRunRow.cost_usd.is_(None))
                .label("unpriced"),
                func.count(AgentRunRow.run_id)
                .filter(AgentRunRow.status != "success")
                .label("failed"),
            )
            .where(
                AgentRunRow.research_id == research_id,
                self._belongs_to_viewer(AgentRunRow.research_id),
            )
            .group_by(AgentRunRow.agent, AgentRunRow.model)
            .order_by(func.sum(AgentRunRow.cost_usd).desc().nullslast())
        )
        return [dict(row._mapping) for row in result]

    async def tool_breakdown(self, research_id: str) -> list[dict[str, Any]]:
        """What each tool cost in time, which is the other half of a run's bill.

        Search credits and fetch latency do not appear in a token total, and on
        a rate-limited provider the wall clock is dominated by waiting rather
        than by spending. A cost view that shows only model spend explains the
        invoice and not the nine minutes.
        """
        result = await self.session.execute(
            select(
                ToolCallRow.tool,
                func.count(ToolCallRow.call_id).label("calls"),
                func.coalesce(func.sum(ToolCallRow.latency_ms), 0.0).label("latency_ms"),
                func.count(ToolCallRow.call_id)
                .filter(ToolCallRow.status != "success")
                .label("failed"),
            )
            .where(
                ToolCallRow.research_id == research_id,
                self._belongs_to_viewer(ToolCallRow.research_id),
            )
            .group_by(ToolCallRow.tool)
            .order_by(func.sum(ToolCallRow.latency_ms).desc())
        )
        return [dict(row._mapping) for row in result]

    async def delete_session(self, research_id: str) -> None:
        """Delete a run and everything it produced, via cascade.

        Scoped like every read. A delete that is not scoped is worse than a read
        that is not scoped: the damage is someone else's and it is permanent.
        """
        await self.session.execute(
            delete(ResearchSession).where(ResearchSession.id == research_id, *self._visible())
        )
