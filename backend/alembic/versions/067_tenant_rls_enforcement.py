"""Validate tenant constraints and enforce forced row-level security.

Revision ID: 067_tenant_rls_enforcement
Revises: 066_tenant_constraints
"""

from alembic import op
import os
import sqlalchemy as sa


revision = "067_tenant_rls_enforcement"
down_revision = "066_tenant_constraints"
branch_labels = None
depends_on = None


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
    "sync_source_files", "sync_sources", "temporal_facts", "web_saves", "wings",
)

NOT_NULL_COLUMNS = {
    "items": ("metadata", "tags", "categories", "status", "created_at", "updated_at"),
    "jobs": ("status", "progress", "created_at"),
    "embeddings": ("created_at", "tenant_id"),
    "embedding_profile_vectors": ("created_at", "tenant_id"),
    "item_relationships": ("confidence", "created_at", "tenant_id"),
}

FOREIGN_KEYS = (
    ("fk_embeddings_tenant_item", "embeddings"),
    ("fk_embedding_profile_vectors_tenant_item", "embedding_profile_vectors"),
    ("fk_item_relationships_tenant_source", "item_relationships"),
    ("fk_item_relationships_tenant_target", "item_relationships"),
)

# Revision 066 still carries these compatibility defaults for old writers.
# Revision 067 removes them after the tenant-aware application is available.
LEGACY_DEFAULT_TABLES = (
    "candidate_curation_artifact_events", "candidate_curation_artifacts",
    "memory_entries", "memory_scope_profiles", "palace_dirty_items",
    "palace_room_events", "palace_runs", "retrieval_hint_artifacts",
    "room_closet_artifacts", "room_memberships", "room_snapshots",
    "room_tunnels", "rooms", "source_subscription_entries",
    "source_subscriptions", "sync_runs", "sync_source_files", "sync_sources",
    "temporal_facts", "wings",
)


def _enable_rls(table: str) -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
    op.execute(sa.text(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY'))
    op.execute(sa.text(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"'))
    op.execute(sa.text(f'''
        CREATE POLICY tenant_isolation ON "{table}"
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
    '''))


def upgrade() -> None:
    # These scans use ShareUpdateExclusive locks, which permit normal DML.
    for name, table in FOREIGN_KEYS:
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(f'ALTER TABLE "{table}" VALIDATE CONSTRAINT "{name}"')

    # Validated guards let PostgreSQL change nullability without rescanning.
    # These final metadata and RLS operations are kept together and are brief.
    for table, columns in NOT_NULL_COLUMNS.items():
        for column in columns:
            op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
            op.alter_column(table, column, nullable=False)
            op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
            op.drop_constraint(f"ck_{table}_{column}_not_null_063", table, type_="check")
    for table in TENANT_TABLES:
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(sa.text(f'ALTER TABLE "{table}" ALTER COLUMN tenant_id DROP DEFAULT'))
    # A Helm pre-upgrade hook runs while old replicas are still serving. Those
    # replicas do not set the new transaction context, so the chart defers only
    # RLS activation to a post-upgrade hook after tenant-aware writers roll out.
    if os.getenv("DEFER_TENANT_RLS_ENFORCEMENT", "").lower() in {"1", "true", "yes"}:
        return
    # Direct Alembic callers keep revision 067 atomic. SET LOCAL therefore
    # applies to every RLS lock, and any failure rolls back the complete
    # revision so Alembic can retry it. The chart uses the deferred branch and
    # app.enforce_tenant_rls for independent per-table transactions after the
    # tenant-aware application rollout.
    for table in TENANT_TABLES:
        _enable_rls(table)


def downgrade() -> None:
    for table in reversed(TENANT_TABLES):
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(sa.text(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"'))
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(sa.text(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY'))
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(sa.text(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY'))
    for table in LEGACY_DEFAULT_TABLES:
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(
            sa.text(
                f'ALTER TABLE "{table}" ALTER COLUMN tenant_id SET DEFAULT \'default\''
            )
        )
    # Restore revision 066 exactly: nullable columns protected by validated
    # check constraints. A later upgrade can remove each guard again.
    for table, columns in NOT_NULL_COLUMNS.items():
        for column in columns:
            name = f"ck_{table}_{column}_not_null_063"
            op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
            op.create_check_constraint(name, table, f'"{column}" IS NOT NULL')
            op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
            op.alter_column(table, column, nullable=True)
