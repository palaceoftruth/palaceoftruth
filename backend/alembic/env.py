import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Import settings and models so Base.metadata is populated
from app.config import settings
from app.database import Base
from app.logging_config import is_configured as logging_is_configured
import app.models  # noqa: F401 — registers all ORM models

config = context.config

# Override sqlalchemy.url from app settings
config.set_main_option("sqlalchemy.url", settings.database_url)

# Only take over logging when Alembic is the entrypoint (the `alembic` CLI and
# the migration Job). The API runs migrations in-process during lifespan, and
# fileConfig() would reset the root logger to alembic.ini's WARN and disable
# every existing app.* logger, silencing all application logs from that point
# on — including "Database migrations complete" on the very next line.
if config.config_file_name is not None and not logging_is_configured():
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


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
    context.configure(connection=connection, target_metadata=target_metadata)
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
