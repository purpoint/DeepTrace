"""The HTTP service.

The API never runs research. It validates a question, queues it, and returns --
because a run takes minutes, and a request that waits for one has already
failed: the client times out, retries, and now two runs are in flight for the
same question.

What it does own is connections. The database pool and the Redis client are
opened at startup and closed at shutdown, not per request, because a pool
created inside a handler is a new pool per call and the cost of that is
invisible until load arrives and the connection count is what breaks.

Both dependencies are optional at startup. A service that refuses to start
without Redis cannot serve finished research during a Redis outage, and one that
refuses without PostgreSQL cannot report *why* it is unhealthy. Each is
attempted, each failure is recorded, and ``/health`` says which is missing --
which is the difference between a service that is down and a service that can
tell you what is down.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from apps.api.errors import install_error_handlers
from apps.api.routes import auth, events, research
from apps.api.schemas import HealthResponse
from core.config import Settings, get_settings
from core.logging import configure_logging, get_logger

log = get_logger(__name__)

__version__ = "0.1.0"

DESCRIPTION = """\
Autonomous research with a traceable answer.

Ask a question and the service decomposes it, searches, extracts passages,
verifies every quotation against the page it came from, checks each claim
against the evidence, and writes a report citing only what survived.

Research is asynchronous. `POST /research` returns immediately with a research
id; poll `GET /research/{id}` for progress, and read the result from the report,
claims, evidence, sources, and trace endpoints.

Every research endpoint requires a bearer token from `POST /auth/login`, and
answers only for the research belonging to that account. A run someone else owns
is reported as though it does not exist.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open connections once, and close them even if startup went badly."""
    settings: Settings = app.state.settings

    app.state.session_factory = None
    app.state.queue = None

    try:
        from infrastructure.db.engine import get_session_factory

        app.state.session_factory = get_session_factory(settings)
        log.info("api.database_ready")
    except Exception as exc:
        log.error("api.database_unavailable", error_type=type(exc).__name__, error=str(exc))

    app.state.events = None
    app.state.sessions = None
    app.state.limiter = None

    try:
        from infrastructure.auth.sessions import SessionStore
        from infrastructure.queue.events import RedisProgressStream
        from infrastructure.queue.redis_queue import RedisJobQueue
        from infrastructure.rate_limit import RateLimiter

        queue = RedisJobQueue.from_settings(settings)
        await queue.client.ping()
        app.state.queue = queue

        # Sessions and rate limits share the queue's connection. Both issue
        # ordinary commands and neither holds one open, so a third client would
        # be three connection pools where one does.
        app.state.sessions = SessionStore(queue.client)
        app.state.limiter = RateLimiter(queue.client)

        # A separate client for the event stream. A connection running a
        # pub/sub subscription cannot serve ordinary commands, and sharing one
        # would mean a single WebSocket client blocking every queue operation
        # the API makes.
        app.state.events = RedisProgressStream.from_settings(settings)
        log.info("api.queue_ready")
    except Exception as exc:
        log.error("api.queue_unavailable", error_type=type(exc).__name__, error=str(exc))

    try:
        yield
    finally:
        if app.state.queue is not None:
            await app.state.queue.close()
        if app.state.events is not None:
            await app.state.events.close()
        from infrastructure.db.engine import dispose_engine

        await dispose_engine()
        log.info("api.stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    A factory rather than a module-level instance, so a test constructs one with
    its own settings instead of importing whatever the environment happened to
    configure. That is also what makes two apps in one process possible, which
    every integration test needs.
    """
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title="DeepTrace",
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    app.state.settings = settings

    # The browser client is served from a different origin in development, so
    # it needs CORS. Origins are configured rather than wildcarded: a wildcard
    # is convenient until credentials are involved, at which point it is a
    # vulnerability that was already shipped.
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["*"],
        )

    install_error_handlers(app)
    app.include_router(auth.router)
    app.include_router(research.router)
    app.include_router(events.router)

    @app.get("/health", response_model=HealthResponse, tags=["service"])
    async def health() -> HealthResponse:
        """Whether the service can work, dependency by dependency.

        Reports each separately because they fail independently and mean
        different things: without PostgreSQL nothing can be read, and without
        Redis nothing new can be accepted while finished research still can.

        Always 200. A health endpoint that returns 503 when a dependency is down
        is one an operator cannot read during exactly the incident they need it
        for -- the body carries the state, and the status says the service is
        answering.
        """
        database = False
        if app.state.session_factory is not None:
            try:
                from sqlalchemy import text

                async with app.state.session_factory() as session:
                    await session.execute(text("SELECT 1"))
                database = True
            except Exception as exc:
                log.warning("api.health_database_failed", error=str(exc))

        queue = False
        if app.state.queue is not None:
            try:
                await app.state.queue.client.ping()
                queue = True
            except Exception as exc:
                log.warning("api.health_queue_failed", error=str(exc))

        return HealthResponse(
            status="ok" if (database and queue) else "degraded",
            database=database,
            queue=queue,
            version=__version__,
        )

    return app


# There is deliberately no module-level ``app``. Run it with
#
#     uvicorn apps.api.main:create_app --factory
#
# A module-level instance would connect to whatever the environment resolved at
# import time, which makes importing this module for any other reason -- a test,
# a CLI, a documentation build -- open a database pool as a side effect.
