"""Idempotently enforce tenant RLS after tenant-aware replicas are ready."""

from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database import _database_url, _engine_options

try:  # asyncpg is the only configured PostgreSQL driver; keep the import explicit
    from asyncpg.exceptions import LockNotAvailableError
except ImportError:  # pragma: no cover - asyncpg is a hard dependency
    LockNotAvailableError = ()  # type: ignore[assignment,misc]


# Keep this inventory synchronized with the current migration head. The contract test makes
# drift fail in CI before a tenant-owned table can be omitted.
TENANT_TABLES = (
    "api_key_audit_events", "api_keys", "browser_extension_pairing_keys", "browser_sessions",
    "candidate_curation_artifact_events", "candidate_curation_artifacts", "claim_sources", "claims",
    "conversation_messages", "conversations", "embedding_profile_vectors", "embeddings", "feeds",
    "item_relationships", "items", "job_attempts", "job_progress_events", "jobs", "mcp_clients",
    "mcp_oauth_access_tokens", "mcp_oauth_authorization_codes", "mcp_oauth_authorization_interactions",
    "mcp_oauth_delegated_grants", "mcp_oauth_refresh_token_families", "mcp_oauth_refresh_tokens",
    "mcp_request_audit_events", "memory_entries", "memory_scope_profiles", "palace_dirty_items",
    "palace_room_events", "palace_runs", "palace_tenant_state", "retrieval_hint_artifacts",
    "room_closet_artifacts", "room_memberships", "room_snapshots", "room_tunnels", "rooms",
    "source_chunks", "source_records", "source_resource_aliases", "source_resource_audit_snapshots",
    "source_resources", "source_subscription_entries", "source_subscriptions", "sync_runs",
    "sync_source_files", "sync_sources", "temporal_facts", "tenant_llm_daily_usage", "web_saves", "wings",
)

# Must equal the current Alembic head. The hook compares this to
# alembic_version exactly, so a new migration that leaves this behind fails the
# hook and, with it, the whole Helm upgrade.
# test_rls_inventory_matches_every_tenant_model pins this to the real head so
# the mismatch is caught in CI rather than during a release.
REQUIRED_ALEMBIC_REVISION = "071_source_drift_artifacts"

POLICY_SQL = """
    CREATE POLICY tenant_isolation ON {table}
    USING (
        current_setting('app.system_access', true) = 'true'
        OR tenant_id = current_setting('app.tenant_id', true)
    )
    WITH CHECK (
        (
            current_setting('app.system_access', true) = 'true'
            OR tenant_id = current_setting('app.tenant_id', true)
        )
        AND NOT EXISTS (
            SELECT 1 FROM tenant_erasure_states AS erasure
            WHERE erasure.subject_tenant_id = tenant_id
        )
    )
"""


async def enforce_tenant_rls() -> None:
    connect_args = dict(_engine_options["connect_args"])
    server_settings = dict(connect_args.get("server_settings", {}))
    server_settings.pop("statement_timeout", None)
    server_settings.pop("idle_in_transaction_session_timeout", None)
    connect_args["server_settings"] = server_settings
    engine = create_async_engine(
        _database_url,
        poolclass=NullPool,
        connect_args=connect_args,
    )
    lock_key = 0x50414C414345
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": lock_key})
            await connection.commit()
            try:
                revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
                if revision != REQUIRED_ALEMBIC_REVISION:
                    raise RuntimeError(
                        "Tenant RLS enforcement requires Alembic revision "
                        f"{REQUIRED_ALEMBIC_REVISION}, found {revision!r}"
                    )
                await connection.commit()
                lock_timeout_ms = settings.database_rls_lock_timeout_ms
                attempts = settings.database_rls_lock_attempts
                deadline = (
                    asyncio.get_event_loop().time() + settings.database_rls_total_budget_seconds
                )
                for table in TENANT_TABLES:
                    await _enforce_table(
                        connection, table, lock_timeout_ms, attempts, deadline
                    )
            finally:
                await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
                await connection.commit()
    finally:
        await engine.dispose()


async def _enforce_table(
    connection, table: str, lock_timeout_ms: int, attempts: int, deadline: float
) -> None:
    """Enable+force RLS and replace the tenant policy on one table.

    The DDL needs ACCESS EXCLUSIVE, which conflicts with every concurrent reader.
    Waiting once with a fixed timeout is a coin flip against long semantic-search
    scans, so each attempt waits the full lock_timeout and retries with linear
    backoff. Only the lock timeout is retried; any other error fails immediately.
    A shared monotonic ``deadline`` caps the whole hook so retries can never
    outlive the chart's migration deadline. Exhausting either bound raises with
    the table name and keeps the release visibly failed -- it never skips a table
    silently and never disables the RLS control to get a green run.
    """

    quoted = f'"{table}"'
    for attempt in range(1, attempts + 1):
        try:
            async with connection.begin():
                await connection.execute(text(f"SET LOCAL lock_timeout = '{lock_timeout_ms}ms'"))
                await connection.execute(text(f"ALTER TABLE {quoted} ENABLE ROW LEVEL SECURITY"))
                await connection.execute(text(f"ALTER TABLE {quoted} FORCE ROW LEVEL SECURITY"))
                await connection.execute(text(f"DROP POLICY IF EXISTS tenant_isolation ON {quoted}"))
                await connection.execute(text(POLICY_SQL.format(table=quoted)))
            return
        except DBAPIError as error:
            if not _is_lock_timeout(error):
                raise
            if attempt == attempts:
                raise RuntimeError(
                    f"tenant RLS enforcement could not take the ACCESS EXCLUSIVE lock on "
                    f"{quoted} after {attempts} attempts of {lock_timeout_ms}ms"
                ) from error
            if asyncio.get_event_loop().time() >= deadline:
                raise RuntimeError(
                    f"tenant RLS enforcement ran out of its total lock budget before "
                    f"{quoted}; remaining tables were not enforced"
                ) from error
            await asyncio.sleep(attempt * (lock_timeout_ms / 1000))


def _is_lock_timeout(error: BaseException) -> bool:
    """True only for PostgreSQL lock_not_available (SQLSTATE 55P03).

    sqlalchemy wraps the asyncpg error in its DBAPI adapter, so the asyncpg class
    is exposed as ``DBAPIError.orig`` rather than the exception type itself; check
    the wrapper, the wrapped ``orig``, the cause chain, and the SQLSTATE.
    """

    seen: set[int] = set()
    candidate: BaseException | None = error
    while candidate is not None and id(candidate) not in seen:
        seen.add(id(candidate))
        if getattr(candidate, "sqlstate", None) == "55P03" or getattr(candidate, "pgcode", None) == "55P03":
            return True
        if LockNotAvailableError and isinstance(candidate, LockNotAvailableError):
            return True
        wrapped = getattr(candidate, "orig", None)
        if isinstance(wrapped, BaseException) and id(wrapped) not in seen:
            candidate = wrapped
            continue
        candidate = candidate.__cause__ or candidate.__context__
    return False


if __name__ == "__main__":
    asyncio.run(enforce_tenant_rls())
