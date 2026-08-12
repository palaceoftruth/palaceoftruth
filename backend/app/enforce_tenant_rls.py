"""Idempotently enforce tenant RLS after tenant-aware replicas are ready."""

from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.database import _database_url, _engine_options


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

REQUIRED_ALEMBIC_REVISION = "068_curation_principals"

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
                for table in TENANT_TABLES:
                    quoted = f'"{table}"'
                    async with connection.begin():
                        await connection.execute(text("SET LOCAL lock_timeout = '5s'"))
                        await connection.execute(text(f"ALTER TABLE {quoted} ENABLE ROW LEVEL SECURITY"))
                        await connection.execute(text(f"ALTER TABLE {quoted} FORCE ROW LEVEL SECURITY"))
                        await connection.execute(text(f"DROP POLICY IF EXISTS tenant_isolation ON {quoted}"))
                        await connection.execute(text(POLICY_SQL.format(table=quoted)))
            finally:
                await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
                await connection.commit()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(enforce_tenant_rls())
