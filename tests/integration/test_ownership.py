"""The acceptance criterion: user A cannot read user B's research.

Asserted twice, at two levels, and the first is the one that matters.

The **query-layer** tests below use the repository directly, with no HTTP and no
routes in the picture at all. They are what proves the isolation is a property
of the queries this system issues rather than of the checks its endpoints
happen to make -- the difference between an invariant and a convention. A
future endpoint written by someone who has never read this file inherits it.

The **endpoint** tests then confirm the routes actually get a scoped repository,
which is the other half: a perfect repository handed to nobody protects nothing.

They also pin the answer's *shape*. A stranger's run is reported as absent, not
as forbidden. 403 would confirm the id is real, and an id a stranger can confirm
is an id a stranger can enumerate -- so the endpoints say 404 and mean "not for
you", which is the one case where being less informative is being more correct.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from decimal import Decimal
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apps.api.main import create_app
from core.config import ResearchDepth, Settings
from core.models.evidence import Evidence, QuoteStatus, QuoteVerification, SupportStrength
from core.models.research import SufficiencyVerdict, TaskResult
from core.models.run import ResearchRun
from core.models.source import Source, SourceType
from infrastructure.auth.sessions import ReusedToken, SessionStore
from infrastructure.db.models import AgentRunRow
from infrastructure.db.repositories.research import ResearchRepository
from infrastructure.db.repositories.scope import Viewer
from infrastructure.db.repositories.users import UserRepository
from infrastructure.queue.redis_queue import RedisJobQueue
from infrastructure.rate_limit import RateLimiter

pytestmark = [pytest.mark.integration]

TEST_REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://localhost:6379/15")
TEST_JWT_SECRET = "a-test-signing-key-long-enough-to-pass-validation"
PASSWORD = "a-long-enough-password"


def a_run(research_id: str, source_id: str) -> ResearchRun:
    """A run with one source and one piece of evidence.

    Enough to exercise every read path -- the session, its sources, its
    evidence, its trace -- because the isolation has to hold on all of them and
    not merely on the one an endpoint happens to call first.
    """
    source = Source(
        id=source_id,
        url="https://kafka.apache.org/documentation",
        title="Kafka Documentation",
        domain="kafka.apache.org",
        source_type=SourceType.OFFICIAL_DOCS,
        quality_score=0.97,
        task_id="ordering",
        content="Records are appended in the order they are sent." * 10,
        word_count=90,
    )
    run = ResearchRun(
        research_id=research_id,
        question="How does Kafka guarantee message ordering?",
        depth=ResearchDepth.QUICK,
    )
    run.task_results = [
        TaskResult(
            task_id="ordering",
            question="How are records ordered?",
            sources=[source],
            rounds=1,
            verdict=SufficiencyVerdict.SUFFICIENT,
            stop_reason="sufficient",
        )
    ]

    from core.agents.evidence import EvidenceExtractionReport

    run.evidence_report = EvidenceExtractionReport(
        evidence=[
            Evidence(
                id=f"ev_{source_id}",
                source_id=source.id,
                task_id="ordering",
                claim="Kafka preserves order within a partition.",
                supporting_text="Records are appended in the order they are sent.",
                location="Ordering guarantees",
                support_strength=SupportStrength.STRONG,
                source_quality=0.97,
                verification=QuoteVerification(status=QuoteStatus.VERBATIM, similarity=1.0),
            )
        ],
        sources_processed=1,
    )
    return run


class TestTheQueryLayer:
    """Isolation asserted with no HTTP layer present.

    Every test here builds a repository directly and asks it for something it
    must not have. If these pass, no route can leak the data by forgetting a
    check, because there is no check to forget.
    """

    @pytest.fixture
    async def two_accounts(self, db_session: AsyncSession) -> tuple[str, str]:
        """Two users, each with one saved run. Returns their ids."""
        users = UserRepository(db_session)
        alice = await users.create(f"alice-{uuid4().hex[:8]}@example.com", password_hash="x")
        bob = await users.create(f"bob-{uuid4().hex[:8]}@example.com", password_hash="x")

        system = ResearchRepository(db_session, Viewer.system())
        await system.save_run(a_run("res_alice", "src_alice"), user_id=alice.id)
        await system.save_run(a_run("res_bob", "src_bob"), user_id=bob.id)
        await db_session.flush()

        return alice.id, bob.id

    async def test_a_run_belonging_to_someone_else_does_not_exist(
        self, db_session: AsyncSession, two_accounts: tuple[str, str]
    ) -> None:
        """The whole criterion, in its smallest form."""
        alice, _ = two_accounts
        repository = ResearchRepository(db_session, Viewer.user(alice))

        assert await repository.get_session("res_alice") is not None
        assert await repository.get_session("res_bob") is None

    async def test_history_contains_only_the_viewer_s_runs(
        self, db_session: AsyncSession, two_accounts: tuple[str, str]
    ) -> None:
        alice, _ = two_accounts
        repository = ResearchRepository(db_session, Viewer.user(alice))

        listed = await repository.list_sessions(limit=50)

        assert [row.id for row in listed] == ["res_alice"]

    async def test_the_child_tables_are_filtered_too(
        self, db_session: AsyncSession, two_accounts: tuple[str, str]
    ) -> None:
        """Not merely the session row. Sources, evidence, claims and the trace
        are queried by research id, and an id from a URL is a stranger's id as
        easily as it is your own -- so the ownership predicate is in those
        queries as well, not only in the lookup that precedes them.
        """
        alice, _ = two_accounts
        repository = ResearchRepository(db_session, Viewer.user(alice))

        assert await repository.get_sources("res_alice") != []
        assert await repository.get_sources("res_bob") == []
        assert await repository.get_evidence("res_bob") == []
        assert await repository.get_claims("res_bob") == []
        assert await repository.get_trace("res_bob") == []

    async def test_the_cost_of_someone_else_s_run_is_not_reported(
        self, db_session: AsyncSession, two_accounts: tuple[str, str]
    ) -> None:
        """Cost is an aggregate, and aggregates leak differently: nobody thinks
        of a sum as data, and a sum over rows you may not read is still an
        answer about them.

        Bob's run is given a priced call first. Without one, ``total_cost``
        returns None for a run nobody owns and a run everybody owns alike -- and
        the test would pass with the filtering removed, which is a test that
        fails before reaching its subject and looks like coverage.
        """
        alice, _ = two_accounts
        db_session.add(
            AgentRunRow(
                run_id="run_bob_1",
                research_id="res_bob",
                agent="planner",
                provider="google",
                model="gemini-3.7-flash",
                prompt_name="planner",
                prompt_version="v1",
                cost_usd=Decimal("0.01234567"),
            )
        )
        await db_session.flush()

        system = ResearchRepository(db_session, Viewer.system())
        assert await system.total_cost("res_bob") == pytest.approx(0.01234567)

        repository = ResearchRepository(db_session, Viewer.user(alice))
        assert await repository.total_cost("res_bob") is None

    async def test_a_run_cannot_be_deleted_by_someone_who_does_not_own_it(
        self, db_session: AsyncSession, two_accounts: tuple[str, str]
    ) -> None:
        """A delete that is not scoped is worse than a read that is not scoped:
        the damage is someone else's and it does not come back."""
        alice, _ = two_accounts
        await ResearchRepository(db_session, Viewer.user(alice)).delete_session("res_bob")
        await db_session.flush()

        system = ResearchRepository(db_session, Viewer.system())
        assert await system.get_session("res_bob") is not None

    async def test_a_run_with_no_owner_belongs_to_nobody(
        self, db_session: AsyncSession, two_accounts: tuple[str, str]
    ) -> None:
        """Research made by the CLI, or made before accounts existed. It is
        visible to the system and to no user -- which matters because ``NULL``
        must not compare equal to a viewer whose id is somehow absent.
        """
        alice, _ = two_accounts
        system = ResearchRepository(db_session, Viewer.system())
        await system.save_run(a_run("res_orphan", "src_orphan"), user_id=None)
        await db_session.flush()

        assert (
            await ResearchRepository(db_session, Viewer.user(alice)).get_session("res_orphan")
            is None
        )
        assert await system.get_session("res_orphan") is not None

    async def test_a_repository_cannot_write_a_run_into_another_account(
        self, db_session: AsyncSession, two_accounts: tuple[str, str]
    ) -> None:
        """The write direction. Reading someone else's history is a leak;
        writing into it is a way to put words in their mouth."""
        alice, bob = two_accounts
        as_alice = ResearchRepository(db_session, Viewer.user(alice))

        await as_alice.save_run(a_run("res_planted", "src_planted"), user_id=bob)
        await db_session.flush()

        system = ResearchRepository(db_session, Viewer.system())
        planted = await system.get_session("res_planted")
        assert planted is not None
        assert planted.user_id == alice

    async def test_an_owner_is_not_erased_by_a_later_save(
        self, db_session: AsyncSession, two_accounts: tuple[str, str]
    ) -> None:
        """A run is written once in progress and again when it finishes. If the
        second write can set the owner back to NULL, the run silently vanishes
        from the history of the person who asked for it -- no error, no log."""
        alice, _ = two_accounts
        system = ResearchRepository(db_session, Viewer.system())

        await system.save_run(a_run("res_alice", "src_alice"), user_id=None)
        await db_session.flush()

        still_hers = await ResearchRepository(db_session, Viewer.user(alice)).get_session(
            "res_alice"
        )
        assert still_hers is not None


@pytest.fixture
async def anonymous(migrated_database: str) -> AsyncIterator[AsyncClient]:
    """The application, wired to test infrastructure, with nobody signed in."""
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=migrated_database,
        redis_url=TEST_REDIS_URL,
        jwt_secret=TEST_JWT_SECRET,
    )
    app = create_app(settings)

    engine = create_async_engine(migrated_database)
    app.state.session_factory = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    queue = RedisJobQueue.from_settings(settings)
    await queue.client.flushdb()
    app.state.queue = queue
    app.state.sessions = SessionStore(queue.client)
    app.state.limiter = RateLimiter(queue.client)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    await queue.client.flushdb()
    await queue.close()
    await engine.dispose()


async def sign_up(client: AsyncClient) -> tuple[dict[str, str], str, str]:
    """Register a fresh account. Returns its headers, id, and refresh token."""
    response = await client.post(
        "/auth/register",
        json={"email": f"user-{uuid4().hex[:12]}@example.com", "password": PASSWORD},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    user_id = (await client.get("/auth/me", headers=headers)).json()["id"]
    return headers, user_id, body["refresh_token"]


class TestTheEndpoints:
    async def test_every_research_endpoint_refuses_an_anonymous_caller(
        self, anonymous: AsyncClient
    ) -> None:
        """Enumerated rather than sampled. A single spot-check would pass while
        one endpoint quietly stayed open, and the open one is the whole story.
        """
        paths = [
            "/research",
            "/research/res_x",
            "/research/res_x/report",
            "/research/res_x/claims",
            "/research/res_x/evidence",
            "/research/res_x/sources",
            "/research/res_x/trace",
        ]
        for path in paths:
            response = await anonymous.get(path)
            assert response.status_code == 401, f"{path} answered {response.status_code}"
            assert response.headers["WWW-Authenticate"] == "Bearer"

        for path, body in (
            ("/research", {"question": "How does Kafka order records?"}),
            ("/research/res_x/cancel", None),
        ):
            response = await anonymous.post(path, json=body)
            assert response.status_code == 401, f"{path} answered {response.status_code}"

    async def test_a_stranger_s_run_reads_as_absent_rather_than_forbidden(
        self, anonymous: AsyncClient
    ) -> None:
        """404, not 403. A 403 confirms the id exists, and an id that can be
        confirmed can be enumerated."""
        owner, _, _ = await sign_up(anonymous)
        submitted = await anonymous.post(
            "/research",
            json={"question": "How does Kafka guarantee message ordering?"},
            headers=owner,
        )
        research_id = submitted.json()["research_id"]

        intruder, _, _ = await sign_up(anonymous)
        response = await anonymous.get(f"/research/{research_id}", headers=intruder)

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    async def test_a_queued_run_is_hidden_before_it_has_a_database_row(
        self, anonymous: AsyncClient
    ) -> None:
        """The window the query layer cannot cover. A run's row is written when
        it finishes, so for the whole time it is queued the only record of it is
        the job in Redis -- and that lookup is the one ownership check written
        by hand. Without it, a guessed id reads back somebody's question.
        """
        owner, _, _ = await sign_up(anonymous)
        submitted = await anonymous.post(
            "/research",
            json={"question": "A question that should stay private"},
            headers=owner,
        )
        research_id = submitted.json()["research_id"]

        # The owner can see it immediately -- that is the fallback's purpose.
        assert (await anonymous.get(f"/research/{research_id}", headers=owner)).status_code == 200

        intruder, _, _ = await sign_up(anonymous)
        response = await anonymous.get(f"/research/{research_id}", headers=intruder)

        assert response.status_code == 404
        assert "should stay private" not in response.text

    async def test_a_stranger_cannot_cancel_a_run(self, anonymous: AsyncClient) -> None:
        """Worse than reading. Cancelling stops work someone else is paying
        for, and the queue is where the job lives -- outside the scoped
        repository entirely."""
        owner, _, _ = await sign_up(anonymous)
        submitted = await anonymous.post(
            "/research", json={"question": "How does Kafka order records?"}, headers=owner
        )
        research_id = submitted.json()["research_id"]

        intruder, _, _ = await sign_up(anonymous)
        response = await anonymous.post(f"/research/{research_id}/cancel", headers=intruder)

        assert response.status_code == 404

    async def test_history_shows_only_your_own_runs(self, anonymous: AsyncClient) -> None:
        """Against a *persisted* run, not a queued one.

        A queued run has no database row, so listing would return nothing for
        everybody -- including for an intruder -- and the test would pass with
        the filtering removed. The run is written directly so that there is
        something real for the query to have to exclude.
        """
        owner, owner_id, _ = await sign_up(anonymous)
        factory = anonymous._transport.app.state.session_factory  # type: ignore[attr-defined]
        async with factory() as session:
            repository = ResearchRepository(session, Viewer.system())
            await repository.save_run(a_run("res_listed", "src_listed"), user_id=owner_id)
            await session.commit()

        assert [
            row["research_id"] for row in (await anonymous.get("/research", headers=owner)).json()
        ] == ["res_listed"]

        intruder, _, _ = await sign_up(anonymous)
        listed = await anonymous.get("/research", headers=intruder)

        assert listed.status_code == 200
        assert listed.json() == []

    async def test_a_submitted_run_is_attributed_to_its_submitter(
        self, anonymous: AsyncClient
    ) -> None:
        """The chain that makes ownership real: the request knows who asked, the
        job carries it, and the worker writes it onto the run."""
        headers, user_id, _ = await sign_up(anonymous)
        submitted = await anonymous.post(
            "/research", json={"question": "How does Kafka order records?"}, headers=headers
        )
        research_id = submitted.json()["research_id"]

        queue = RedisJobQueue.from_settings(
            Settings(_env_file=None, redis_url=TEST_REDIS_URL)  # type: ignore[call-arg]
        )
        job = await queue.get_by_research(research_id)
        await queue.close()

        assert job is not None
        assert job.user_id == user_id


class TestSigningIn:
    async def test_an_account_can_be_created_and_signed_into(self, anonymous: AsyncClient) -> None:
        email = f"user-{uuid4().hex[:12]}@example.com"
        created = await anonymous.post(
            "/auth/register", json={"email": email, "password": PASSWORD}
        )
        assert created.status_code == 201

        signed_in = await anonymous.post("/auth/login", json={"email": email, "password": PASSWORD})

        assert signed_in.status_code == 200
        assert signed_in.json()["token_type"] == "Bearer"
        assert signed_in.json()["expires_in"] > 0

    async def test_an_email_is_the_same_account_in_any_case(self, anonymous: AsyncClient) -> None:
        """Normalised on the way in, which is what makes the unique index a real
        constraint rather than a suggestion. Otherwise one person registers
        twice and neither account is the one they meant."""
        email = f"Mixed-{uuid4().hex[:8]}@Example.COM"
        await anonymous.post("/auth/register", json={"email": email, "password": PASSWORD})

        signed_in = await anonymous.post(
            "/auth/login", json={"email": email.lower(), "password": PASSWORD}
        )

        assert signed_in.status_code == 200

    async def test_a_wrong_password_and_a_missing_account_are_indistinguishable(
        self, anonymous: AsyncClient
    ) -> None:
        """Same status, same code, same sentence. Any difference here turns the
        endpoint into a service that reports whether an address is
        registered."""
        email = f"user-{uuid4().hex[:12]}@example.com"
        await anonymous.post("/auth/register", json={"email": email, "password": PASSWORD})

        wrong = await anonymous.post(
            "/auth/login", json={"email": email, "password": "the-wrong-password"}
        )
        missing = await anonymous.post(
            "/auth/login",
            json={"email": f"nobody-{uuid4().hex[:8]}@example.com", "password": PASSWORD},
        )

        assert wrong.status_code == missing.status_code == 401
        assert wrong.json() == missing.json()

    async def test_a_password_below_the_minimum_is_refused(self, anonymous: AsyncClient) -> None:
        response = await anonymous.post(
            "/auth/register",
            json={"email": f"user-{uuid4().hex[:8]}@example.com", "password": "short"},
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request"

    async def test_a_duplicate_registration_is_a_conflict(self, anonymous: AsyncClient) -> None:
        email = f"user-{uuid4().hex[:12]}@example.com"
        await anonymous.post("/auth/register", json={"email": email, "password": PASSWORD})

        again = await anonymous.post("/auth/register", json={"email": email, "password": PASSWORD})

        assert again.status_code == 409

    async def test_the_password_is_never_sent_back(self, anonymous: AsyncClient) -> None:
        """Not in the tokens, not in /auth/me, not in an error. The response
        model has nowhere to put one, which is the point of having one."""
        headers, _, _ = await sign_up(anonymous)

        profile = await anonymous.get("/auth/me", headers=headers)

        assert PASSWORD not in profile.text
        assert "password" not in profile.json()


class TestSessions:
    async def test_a_refresh_token_buys_a_new_pair(self, anonymous: AsyncClient) -> None:
        _, _, refresh_token = await sign_up(anonymous)

        renewed = await anonymous.post("/auth/refresh", json={"refresh_token": refresh_token})

        assert renewed.status_code == 200
        assert renewed.json()["refresh_token"] != refresh_token, "the token was not rotated"

    async def test_an_access_token_cannot_be_used_to_refresh(self, anonymous: AsyncClient) -> None:
        headers, _, _ = await sign_up(anonymous)
        access = headers["Authorization"].removeprefix("Bearer ")

        response = await anonymous.post("/auth/refresh", json={"refresh_token": access})

        assert response.status_code == 401

    async def test_reusing_a_spent_refresh_token_ends_every_session(
        self, anonymous: AsyncClient
    ) -> None:
        """Theft detection. A spent token presented again means two parties hold
        it, and there is no way to tell which is the owner -- so both are signed
        out and the password becomes the only way back in.

        The grace window that forgives a double-submit is thirty seconds, so
        this test waits it out rather than racing it: the first rotation is
        spent twice over, with a third presentation proving the family is gone.
        """
        from infrastructure.auth import sessions as sessions_module

        _, _, first = await sign_up(anonymous)
        second = (await anonymous.post("/auth/refresh", json={"refresh_token": first})).json()[
            "refresh_token"
        ]

        # Expire the grace window without waiting for it.
        store: SessionStore = anonymous._transport.app.state.sessions  # type: ignore[attr-defined]
        await store.client.delete(
            sessions_module.GRACE_KEY.format(
                token_id=__import__("jwt").decode(first, options={"verify_signature": False})["jti"]
            )
        )

        replayed = await anonymous.post("/auth/refresh", json={"refresh_token": first})

        assert replayed.status_code == 401
        # And the token that was legitimately issued is now dead too.
        after = await anonymous.post("/auth/refresh", json={"refresh_token": second})
        assert after.status_code == 401

    async def test_a_double_submit_inside_the_grace_window_is_not_treated_as_theft(
        self, anonymous: AsyncClient
    ) -> None:
        """Two tabs, or a component that mounts twice. The loser of the race
        looks exactly like a thief, and signing people out for opening a second
        tab is a worse bug than a thirty-second detection gap."""
        _, _, first = await sign_up(anonymous)
        good = await anonymous.post("/auth/refresh", json={"refresh_token": first})
        still_valid = good.json()["refresh_token"]

        raced = await anonymous.post("/auth/refresh", json={"refresh_token": first})

        assert raced.status_code == 401  # refused
        # but the session it produced still works, which is the actual claim
        assert (
            await anonymous.post("/auth/refresh", json={"refresh_token": still_valid})
        ).status_code == 200

    async def test_signing_out_revokes_the_refresh_token(self, anonymous: AsyncClient) -> None:
        """A logout that only clears the client's storage leaves a working
        credential in anything that copied it."""
        headers, _, refresh_token = await sign_up(anonymous)

        out = await anonymous.post(
            "/auth/logout", json={"refresh_token": refresh_token}, headers=headers
        )

        assert out.status_code == 204
        assert (
            await anonymous.post("/auth/refresh", json={"refresh_token": refresh_token})
        ).status_code == 401

    async def test_signing_out_everywhere_revokes_every_session(
        self, anonymous: AsyncClient
    ) -> None:
        headers, _, first = await sign_up(anonymous)
        second = (await anonymous.post("/auth/refresh", json={"refresh_token": first})).json()[
            "refresh_token"
        ]

        await anonymous.post("/auth/logout-everywhere", headers=headers)

        assert (
            await anonymous.post("/auth/refresh", json={"refresh_token": second})
        ).status_code == 401

    async def test_a_token_signed_with_another_key_is_refused(self, anonymous: AsyncClient) -> None:
        import jwt

        forged = jwt.encode(
            {
                "sub": "usr_whoever",
                "jti": "forged",
                "typ": "access",
                "iss": "deeptrace",
                "iat": 0,
                "exp": 2**31,
            },
            "a-completely-different-key-of-sufficient-length",
            algorithm="HS256",
        )

        response = await anonymous.get("/research", headers={"Authorization": f"Bearer {forged}"})

        assert response.status_code == 401

    async def test_a_valid_token_for_a_deleted_account_stops_working(
        self, anonymous: AsyncClient, migrated_database: str
    ) -> None:
        """The reason the token is not trusted on its own. Verifying by
        signature alone would let a disabled account keep working until its
        access token expired, which is a strange thing to say to someone who
        just reported a stolen laptop."""
        from sqlalchemy import update

        from infrastructure.db.models import User

        headers, user_id, _ = await sign_up(anonymous)
        assert (await anonymous.get("/auth/me", headers=headers)).status_code == 200

        factory = anonymous._transport.app.state.session_factory  # type: ignore[attr-defined]
        async with factory() as session:
            await session.execute(update(User).where(User.id == user_id).values(is_active=False))
            await session.commit()

        assert (await anonymous.get("/auth/me", headers=headers)).status_code == 401


class TestRateLimits:
    async def test_repeated_sign_in_attempts_are_eventually_refused(
        self, anonymous: AsyncClient
    ) -> None:
        """Ten per fifteen minutes per address. Without it, a password is only
        as strong as how fast the attacker's connection is."""
        email = f"user-{uuid4().hex[:12]}@example.com"
        await anonymous.post("/auth/register", json={"email": email, "password": PASSWORD})

        statuses = [
            (
                await anonymous.post(
                    "/auth/login", json={"email": email, "password": "wrong-password"}
                )
            ).status_code
            for _ in range(12)
        ]

        assert 429 in statuses

    async def test_a_refusal_says_how_long_to_wait(self, anonymous: AsyncClient) -> None:
        """A 429 without Retry-After leaves the client guessing, and it guesses
        wrong in the direction that hurts."""
        for _ in range(12):
            response = await anonymous.post(
                "/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}
            )
            if response.status_code == 429:
                break

        assert response.status_code == 429
        assert int(response.headers["Retry-After"]) > 0
        assert response.json()["error"]["code"] == "rate_limited"

    async def test_submissions_are_limited_per_account_not_per_address(
        self, anonymous: AsyncClient
    ) -> None:
        """What this limit protects is money, and money belongs to an account.
        Counting by address would charge two colleagues in one office as one
        person, and let one person on two networks spend twice.
        """
        first, _, _ = await sign_up(anonymous)
        second, _, _ = await sign_up(anonymous)

        for _ in range(21):
            spent = await anonymous.post(
                "/research", json={"question": "How does Kafka order records?"}, headers=first
            )

        assert spent.status_code == 429

        # The second account, from the same address, is unaffected.
        other = await anonymous.post(
            "/research", json={"question": "How does Kafka order records?"}, headers=second
        )
        assert other.status_code == 202


class TestTheSessionStore:
    """The Redis half, tested directly.

    The endpoint tests above prove the flow. These prove the store's own
    contract, which is easier to state and harder to see through HTTP.
    """

    @pytest.fixture
    async def store(self) -> AsyncIterator[SessionStore]:
        client: Redis = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
        await client.flushdb()
        try:
            yield SessionStore(client)
        finally:
            await client.flushdb()
            await client.aclose()

    async def test_a_token_can_be_spent_once(self, store: SessionStore) -> None:
        await store.remember("usr_1", "tok_1", 600)

        await store.spend("usr_1", "tok_1")

        with pytest.raises(LookupError):  # inside the grace window
            await store.spend("usr_1", "tok_1")

    async def test_spending_outside_the_grace_window_is_reported_as_reuse(
        self, store: SessionStore
    ) -> None:
        with pytest.raises(ReusedToken):
            await store.spend("usr_1", "never-issued")

    async def test_revoking_ends_every_session_at_once(self, store: SessionStore) -> None:
        await store.remember("usr_1", "tok_1", 600)
        await store.remember("usr_1", "tok_2", 600)

        assert await store.revoke_all("usr_1") == 2

        with pytest.raises(ReusedToken):
            await store.spend("usr_1", "tok_2")

    async def test_one_user_s_revocation_does_not_touch_another_s(
        self, store: SessionStore
    ) -> None:
        await store.remember("usr_1", "tok_1", 600)
        await store.remember("usr_2", "tok_2", 600)

        await store.revoke_all("usr_1")

        await store.spend("usr_2", "tok_2")  # must not raise

    async def test_a_token_recorded_for_one_user_cannot_be_spent_by_another(
        self, store: SessionStore
    ) -> None:
        """The signature said one user and the record says another. Not a race
        -- a token that has been tampered with, or a key reused across
        deployments."""
        await store.remember("usr_1", "tok_1", 600)

        with pytest.raises(LookupError):
            await store.spend("usr_2", "tok_1")

    async def test_a_ticket_is_good_exactly_once(self, store: SessionStore) -> None:
        ticket = await store.issue_ticket("usr_1", 30)

        assert await store.redeem_ticket(ticket) == "usr_1"
        assert await store.redeem_ticket(ticket) is None


class TestTheRateLimiter:
    @pytest.fixture
    async def limiter(self) -> AsyncIterator[RateLimiter]:
        client: Redis = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
        await client.flushdb()
        try:
            yield RateLimiter(client)
        finally:
            await client.flushdb()
            await client.aclose()

    async def test_requests_up_to_the_limit_are_allowed(self, limiter: RateLimiter) -> None:
        decisions = [await limiter.check("test", "someone", limit=3, window=60) for _ in range(3)]

        assert all(decision.allowed for decision in decisions)
        assert [decision.remaining for decision in decisions] == [2, 1, 0]

    async def test_the_next_one_is_refused_with_a_wait(self, limiter: RateLimiter) -> None:
        for _ in range(3):
            await limiter.check("test", "someone", limit=3, window=60)

        refused = await limiter.check("test", "someone", limit=3, window=60)

        assert not refused.allowed
        assert 0 < refused.retry_after <= 60

    async def test_a_refused_request_is_not_counted_against_the_window(
        self, limiter: RateLimiter
    ) -> None:
        """A limiter that records what it turned away pushes its own window
        forward every time a client retries, so a client that keeps trying can
        never return -- and every Retry-After it was sent was a lie."""
        for _ in range(3):
            await limiter.check("test", "someone", limit=3, window=60)

        first = await limiter.check("test", "someone", limit=3, window=60)
        for _ in range(10):
            await limiter.check("test", "someone", limit=3, window=60)
        last = await limiter.check("test", "someone", limit=3, window=60)

        assert last.retry_after <= first.retry_after

    async def test_two_identities_have_separate_allowances(self, limiter: RateLimiter) -> None:
        for _ in range(3):
            await limiter.check("test", "someone", limit=3, window=60)

        other = await limiter.check("test", "someone-else", limit=3, window=60)

        assert other.allowed

    async def test_two_buckets_do_not_share_an_allowance(self, limiter: RateLimiter) -> None:
        """One person's login attempts must not consume their own research
        budget."""
        for _ in range(3):
            await limiter.check("auth", "someone", limit=3, window=60)

        other = await limiter.check("submit", "someone", limit=3, window=60)

        assert other.allowed
