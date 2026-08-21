"""Database engine and session management.

Async throughout, because the research workflow is async end to end and a
synchronous database call inside it would block the event loop that every
concurrent task shares.

Sessions are handed out by a context manager rather than created ad hoc. A
session that is never closed holds a pooled connection, and a research worker
running long jobs will exhaust the pool within hours of that mistake.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from core.config import MissingConfigurationError, Settings, get_settings
from core.logging import get_logger

log = get_logger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def normalise_database_url(url: str) -> str:
    """Force the async driver.

    A ``postgresql://`` URL selects the synchronous driver, which fails at
    connect time with an error that does not mention the driver. Rewriting it
    here means a copied-from-somewhere URL works instead of producing a
    confusing failure.
    """
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


def create_engine(settings: Settings | None = None, *, url: str | None = None) -> AsyncEngine:
    """Build an engine.

    Pool sizing is deliberate rather than default. A worker runs a bounded
    number of concurrent research tasks, and the pool must be able to serve all
    of them plus the API; too small and tasks queue behind each other, too large
    and Postgres runs out of backends.
    """
    settings = settings or get_settings()
    resolved = url or settings.database_url
    if not resolved:
        raise MissingConfigurationError("database_url")

    return create_async_engine(
        normalise_database_url(resolved),
        echo=False,
        pool_size=10,
        max_overflow=5,
        pool_pre_ping=True,
        # Verifies a connection before handing it out. Costs one round trip and
        # avoids the class of failure where a connection died while idle and the
        # first query after it fails for no visible reason.
        pool_recycle=1800,
    )


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    """Return the process-wide engine, creating it once."""
    global _engine, _session_factory
    if _engine is None:
        _engine = create_engine(settings)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
        # expire_on_commit=False so objects stay readable after commit. Without
        # it, touching any attribute of a committed object issues a fresh query,
        # which inside an async context means an await where none is expected.
    return _engine


def get_session_factory(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    get_engine(settings)
    assert _session_factory is not None  # noqa: S101 - set by get_engine
    return _session_factory


@asynccontextmanager
async def session_scope(
    settings: Settings | None = None,
) -> AsyncIterator[AsyncSession]:
    """A transactional session that commits on success and rolls back on error.

    The rollback matters more than the commit. A research run that fails
    partway must not leave half its sources written and its evidence missing,
    because a partial write is indistinguishable from a complete one when it is
    read back later.
    """
    factory = get_session_factory(settings)
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Close every pooled connection. Called on shutdown and between tests."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
