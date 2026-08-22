"""Fixtures for tests that need a real PostgreSQL database.

These run against ``deeptrace_test``, migrated from empty with Alembic rather
than created with ``metadata.create_all``. The difference matters: create_all
builds the schema the models describe, while the migrations build the schema
that will actually exist in production. Testing against the first proves nothing
about the second, and a broken migration would pass every test.

Each test runs inside a transaction that is rolled back afterwards, so tests
cannot see each other's rows regardless of execution order.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from infrastructure.db.engine import normalise_database_url

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql+asyncpg://localhost:5432/deeptrace_test"
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config(url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "infrastructure/db/migrations"))
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture(scope="session")
def migrated_database() -> str:
    """Migrate the test database from empty, once per session.

    Runs downgrade-then-upgrade so every session also exercises the reverse
    migration. A downgrade that is never run is a downgrade that does not work.
    """
    url = normalise_database_url(TEST_DATABASE_URL)
    config = _alembic_config(url)

    # Alembic's own engine is synchronous here; env.py handles the async driver.
    os.environ["DATABASE_URL"] = url
    with suppress(Exception):  # an already-empty database has nothing to downgrade
        command.downgrade(config, "base")
    command.upgrade(config, "head")
    return url


@pytest.fixture
async def db_session(migrated_database: str) -> AsyncIterator[AsyncSession]:
    """A session inside a transaction that is always rolled back.

    Isolation by rollback rather than by truncation: it is faster, and it means
    a test that leaves data behind cannot affect the next one even if it fails
    partway through.
    """
    engine = create_async_engine(migrated_database, poolclass=None)
    connection = await engine.connect()
    transaction = await connection.begin()
    factory = async_sessionmaker(bind=connection, expire_on_commit=False, class_=AsyncSession)

    session = factory()
    try:
        yield session
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()
        await engine.dispose()
