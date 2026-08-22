"""Durable workflow checkpoints, in PostgreSQL.

The in-process checkpointer in ``core.graph.workflow`` makes a run resumable
within one process, which is exactly as long as the process lives. That is
enough for a test and worth nothing to a worker that was killed mid-run --
precisely the case resumability exists for.

This adapter lives in ``infrastructure`` for the same reason the run recorder
does: ``core`` may not import a database driver. The graph is handed a
checkpointer that satisfies LangGraph's protocol and never learns where state
went.

Two details that are easy to get wrong and silent when you do:

*The driver.* LangGraph's Postgres saver speaks psycopg3, while the application
engine speaks asyncpg. One ``DATABASE_URL`` has to serve both, so the async
driver suffix is stripped here rather than requiring a second variable that can
drift out of agreement with the first.

*The serializer.* Built with :func:`build_serializer` so checkpoints hold
DeepTrace's domain models. A saver constructed without it appears to work and
stops loading checkpoints after a library upgrade -- correct at write time,
broken at read time, in the future.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from core.config import MissingConfigurationError, Settings, get_settings
from core.graph.serde import build_serializer
from core.logging import get_logger

log = get_logger(__name__)


def psycopg_url(url: str) -> str:
    """Rewrite a SQLAlchemy database URL for psycopg.

    ``postgresql+asyncpg://`` is a SQLAlchemy dialect string, not a libpq
    connection string. Passing it to psycopg fails with an error that names the
    scheme rather than the driver, so it is normalised here instead.
    """
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix) :]
    return url


@asynccontextmanager
async def checkpointer_scope(
    settings: Settings | None = None, *, url: str | None = None
) -> AsyncIterator[Any]:
    """A Postgres checkpointer, with its tables ensured and its pool closed.

    ``setup()`` runs on every entry. It is idempotent, and the alternative --
    a migration that has to be applied before the first resumable run -- means
    the first crash is also the run that discovers the tables were missing.

    The checkpoint tables are LangGraph's own schema, deliberately not managed
    by Alembic: they belong to the library, and a migration of ours describing
    them would be a copy that silently falls behind the version we depend on.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    settings = settings or get_settings()
    resolved = url or settings.database_url
    if not resolved:
        raise MissingConfigurationError("database_url")

    async with AsyncPostgresSaver.from_conn_string(
        psycopg_url(resolved), serde=build_serializer()
    ) as saver:
        await saver.setup()
        log.debug("checkpointer.ready")
        yield saver
