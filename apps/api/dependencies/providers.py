"""What a request needs, supplied once per request.

Connections are opened at startup and closed at shutdown, not per request. A
database engine created inside a handler builds a fresh pool for every call, and
the cost of that is invisible until load arrives and the connection count is
what fails.

The queue and the engine live on the application, which is what makes tests
straightforward: a test builds the app with its own connections rather than
patching module globals.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.errors import ApiError
from core.config import Settings
from infrastructure.db.repositories.research import ResearchRepository
from infrastructure.queue.redis_queue import RedisJobQueue


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


async def get_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ResearchRepository:
    return ResearchRepository(session)


async def get_queue(request: Request) -> RedisJobQueue:
    queue = getattr(request.app.state, "queue", None)
    if queue is None:
        raise ApiError.unavailable("The job queue")
    return queue  # type: ignore[no-any-return]


Repository = Annotated[ResearchRepository, Depends(get_repository)]
Queue = Annotated[RedisJobQueue, Depends(get_queue)]
Config = Annotated[Settings, Depends(get_settings_from)]
