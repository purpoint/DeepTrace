"""Alembic environment.

The database URL comes from application settings rather than alembic.ini, so
there is exactly one place a connection string is configured. Duplicating it
means the application and its migrations can point at different databases, and
that failure is invisible until a migration appears not to have run.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from alembic.runtime.environment import NameFilterParentNames, NameFilterType
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from core.config import get_settings
from infrastructure.db.engine import normalise_database_url
from infrastructure.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

settings = get_settings()
if settings.database_url:
    config.set_main_option("sqlalchemy.url", normalise_database_url(settings.database_url))


def include_name(
    name: str | None,
    type_: NameFilterType,
    parent_names: NameFilterParentNames,  # noqa: ARG001 - Alembic's callback signature
) -> bool:
    """Consider only the tables this project's models declare.

    Autogenerate compares the models against everything in the database, so a
    table it does not own reads as a table that should not exist. LangGraph's
    checkpointer creates and manages its own tables, and the first autogenerate
    after they appeared produced a migration whose upgrade dropped all four --
    which is every checkpoint, and every run that could have been resumed.

    Caught by reading the generated migration. A migration is code, generated or
    not, and this is the failure mode that makes reading it non-optional: the
    change asked for was one added column, and the file also contained four
    silent drops.
    """
    if type_ == "table":
        return name in target_metadata.tables
    return True


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Detects column type changes, which autogenerate ignores by default.
        # A silently unmigrated type change is worse than a noisy diff.
        compare_type=True,
        compare_server_default=True,
        include_name=include_name,
    )


def run_migrations_offline() -> None:
    """Generate SQL without connecting. Used to review a migration before it runs."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_name=include_name,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
