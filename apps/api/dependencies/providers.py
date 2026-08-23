"""What a request needs, supplied once per request.

Connections are opened at startup and closed at shutdown, not per request. A
database engine created inside a handler builds a fresh pool for every call, and
the cost of that is invisible until load arrives and the connection count is
what fails.

The queue and the engine live on the application, which is what makes tests
straightforward: a test builds the app with its own connections rather than
patching module globals.

Only connections live here. Who is making the request, and the repository
scoped to them, live in :mod:`apps.api.dependencies.identity` -- which imports
this module and is never imported by it. Splitting them by that direction is
what keeps the two from forming an import cycle.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.errors import ApiError
from core.config import Settings
from infrastructure.auth.sessions import SessionStore
from infrastructure.queue.redis_queue import RedisJobQueue
from infrastructure.rate_limit import RateLimiter


async def get_settings_from(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """A database session, committed on success and rolled back on failure.

    The rollback matters more than the commit: a request that fails partway
    must not leave half its writes behind, because a partial write is
    indistinguishable from a complete one when it is read back.
    """
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise ApiError.unavailable("The database")

    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_queue(request: Request) -> RedisJobQueue:
    queue = getattr(request.app.state, "queue", None)
    if queue is None:
        raise ApiError.unavailable("The job queue")
    return queue  # type: ignore[no-any-return]


async def get_sessions(request: Request) -> SessionStore:
    """The refresh-token and ticket store.

    Unavailable when Redis is. That is survivable in one direction and not the
    other: an access token is verified by signature alone, so reading research
    keeps working through a Redis outage, while signing in and refreshing do
    not. The service degrades to "everyone already signed in can keep reading",
    which is the right shape for a read-heavy system.
    """
    store = getattr(request.app.state, "sessions", None)
    if store is None:
        raise ApiError.unavailable("Sign-in")
    return store  # type: ignore[no-any-return]


async def get_limiter(request: Request) -> RateLimiter:
    limiter = getattr(request.app.state, "limiter", None)
    if limiter is None:
        # Deliberately not a silent pass-through. A limiter that fails open
        # turns a Redis outage into an unmetered API, which is the moment the
        # limits mattered most -- and nothing in the response would say so.
        raise ApiError.unavailable("Rate limiting")
    return limiter  # type: ignore[no-any-return]


def client_identity(request: Request) -> str:
    """Who to count a rate-limited request against, before anyone has signed in.

    The socket's peer address, not ``X-Forwarded-For``. That header is written
    by the client and rewritten by every proxy in the path, so trusting it
    unconditionally lets an attacker send a different value on each request and
    have their own limit bucket every time -- which is not a rate limit.

    Behind a real proxy this is the proxy's address, and every client shares one
    bucket. The fix is uvicorn's ``--proxy-headers`` with ``--forwarded-allow-ips``
    naming the trusted hop, which is deployment configuration rather than
    something this function can decide.
    """
    return request.client.host if request.client else "unknown"


Queue = Annotated[RedisJobQueue, Depends(get_queue)]
Sessions = Annotated[SessionStore, Depends(get_sessions)]
Limiter = Annotated[RateLimiter, Depends(get_limiter)]
Config = Annotated[Settings, Depends(get_settings_from)]
ClientAddress = Annotated[str, Depends(client_identity)]
