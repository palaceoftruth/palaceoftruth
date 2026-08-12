import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context

# Import settings and models so Base.metadata is populated
from app.database import Base, _database_url, _engine_options
from app.logging_config import is_configured as logging_is_configured
import app.models  # noqa: F401 — registers all ORM models

config = context.config

# Override sqlalchemy.url from app settings
config.set_main_option("sqlalchemy.url", _database_url)

# Only take over logging when Alembic is the entrypoint (the `alembic` CLI and
# the migration Job). The API runs migrations in-process during lifespan, and
# fileConfig() would reset the root logger to alembic.ini's WARN and disable
# every existing app.* logger, silencing all application logs from that point
# on — including "Database migrations complete" on the very next line.
if config.config_file_name is not None and not logging_is_configured():
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def include_object(_object, _name, type_, _reflected, _compare_to) -> bool:
    # Legacy migrations created many performance indexes and cross-table
    # constraints with raw SQL, so they are intentionally not modeled by the
    # ORM. Alembic still checks tables, columns, nullability, and types; static
    # database health owns the named index and constraint inventory.
    return type_ not in {"index", "unique_constraint", "foreign_key_constraint"}


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    # All API replicas may start together. A session-level advisory lock makes
    # the whole Alembic run single-writer without relying on pod timing.
    lock_key = 0x50414C414345  # "PALACE"
    connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": lock_key})
    # End the implicit transaction opened by the lock query. The advisory lock
    # is session-scoped and remains held, while older migrations can now use
    # Alembic autocommit blocks for CREATE INDEX CONCURRENTLY.
    connection.commit()
    try:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()
    finally:
        connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
        connection.commit()


async def run_async_migrations() -> None:
    connectable = create_async_engine(
        _database_url,
        poolclass=pool.NullPool,
        connect_args=_engine_options["connect_args"],
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
