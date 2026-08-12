"""Add tenant keys to vectors and enforce tenant row-level security.

Revision ID: 061_tenant_rls
Revises: 060_null_webhook_keys
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "061_tenant_rls"
down_revision = "060_null_webhook_keys"
branch_labels = None
depends_on = None


# Keep this explicit. A new tenant-owned model must update this migration
# contract and the database-health check instead of silently missing RLS.
TENANT_TABLES = (
    "api_key_audit_events",
    "api_keys",
    "browser_extension_pairing_keys",
    "browser_sessions",
    "candidate_curation_artifact_events",
    "candidate_curation_artifacts",
    "claim_sources",
    "claims",
    "conversation_messages",
    "conversations",
    "embedding_profile_vectors",
    "embeddings",
    "feeds",
    "item_relationships",
    "items",
    "job_attempts",
    "job_progress_events",
    "jobs",
    "mcp_clients",
    "mcp_oauth_access_tokens",
    "mcp_oauth_authorization_codes",
    "mcp_oauth_authorization_interactions",
    "mcp_oauth_delegated_grants",
    "mcp_oauth_refresh_token_families",
    "mcp_oauth_refresh_tokens",
    "mcp_request_audit_events",
    "memory_entries",
    "memory_scope_profiles",
    "palace_dirty_items",
    "palace_room_events",
    "palace_runs",
    "palace_tenant_state",
    "retrieval_hint_artifacts",
    "room_closet_artifacts",
    "room_memberships",
    "room_snapshots",
    "room_tunnels",
    "rooms",
    "source_chunks",
    "source_records",
    "source_resource_aliases",
    "source_resource_audit_snapshots",
    "source_resources",
    "source_subscription_entries",
    "source_subscriptions",
    "sync_runs",
    "sync_source_files",
    "sync_sources",
    "temporal_facts",
    "web_saves",
    "wings",
)


def _enable_rls(table: str) -> None:
    # System access is a separate server-owned flag, so no tenant identifier can
    # collide with the background/admin database context.
    op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
    op.execute(sa.text(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY'))
    op.execute(
        sa.text(
            f'''
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
                    SELECT 1
                    FROM tenant_erasure_states AS erasure
                    WHERE erasure.subject_tenant_id = tenant_id
                )
            )
            '''
        )
    )


def upgrade() -> None:
    op.create_table(
        "data_lifecycle_audit_events",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("subject_tenant_id", sa.Text(), nullable=False),
        sa.Column("subject_item_id", sa.UUID(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("details", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_data_lifecycle_audit_events_subject_tenant_id",
        "data_lifecycle_audit_events",
        ["subject_tenant_id"],
    )
    op.create_table(
        "tenant_erasure_states",
        sa.Column("subject_tenant_id", sa.Text(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("subject_tenant_id"),
    )

    # Align historical nullable declarations with the ORM contract. Existing
    # rows receive the same values that inserts already receive from defaults.
    op.execute(
        sa.text(
            """
            UPDATE items SET
                metadata = COALESCE(metadata, '{}'::jsonb),
                tags = COALESCE(tags, '{}'::text[]),
                categories = COALESCE(categories, '{}'::text[]),
                status = COALESCE(status, 'processing'),
                created_at = COALESCE(created_at, now()),
                updated_at = COALESCE(updated_at, created_at, now())
            """
        )
    )
    for column in ("metadata", "tags", "categories", "status", "created_at", "updated_at"):
        op.alter_column("items", column, nullable=False)
    op.execute(
        sa.text(
            """
            UPDATE jobs SET
                status = COALESCE(status, 'queued'),
                progress = COALESCE(progress, 0),
                created_at = COALESCE(created_at, now())
            """
        )
    )
    for column in ("status", "progress", "created_at"):
        op.alter_column("jobs", column, nullable=False)
    op.execute(sa.text("UPDATE embeddings SET created_at = COALESCE(created_at, now())"))
    op.alter_column("embeddings", "created_at", nullable=False)
    op.execute(sa.text("UPDATE embedding_profile_vectors SET created_at = COALESCE(created_at, now())"))
    op.alter_column("embedding_profile_vectors", "created_at", nullable=False)
    op.execute(
        sa.text(
            """
            UPDATE item_relationships SET
                confidence = COALESCE(confidence, 0.0),
                created_at = COALESCE(created_at, now())
            """
        )
    )
    op.alter_column("item_relationships", "confidence", nullable=False)
    op.alter_column("item_relationships", "created_at", nullable=False)
    op.create_unique_constraint("uq_items_tenant_id_id", "items", ["tenant_id", "id"])

    for table in ("embeddings", "embedding_profile_vectors"):
        op.add_column(table, sa.Column("tenant_id", sa.Text(), nullable=True))
        op.execute(
            sa.text(
                f'''
                UPDATE "{table}" AS child
                SET tenant_id = item.tenant_id
                FROM items AS item
                WHERE item.id = child.item_id
                '''
            )
        )
        op.alter_column(table, "tenant_id", nullable=False)

    op.add_column("item_relationships", sa.Column("tenant_id", sa.Text(), nullable=True))
    op.execute(
        sa.text(
            """
            UPDATE item_relationships AS relationship
            SET tenant_id = source.tenant_id
            FROM items AS source, items AS target
            WHERE source.id = relationship.source_item_id
              AND target.id = relationship.target_item_id
              AND source.tenant_id = target.tenant_id
            """
        )
    )
    cross_tenant = op.get_bind().execute(
        sa.text("SELECT COUNT(*) FROM item_relationships WHERE tenant_id IS NULL")
    ).scalar_one()
    if cross_tenant:
        raise RuntimeError(
            "item_relationships contains missing or cross-tenant endpoints; "
            "repair those rows before applying 061_tenant_rls"
        )
    op.alter_column("item_relationships", "tenant_id", nullable=False)

    op.create_foreign_key(
        "fk_embeddings_tenant_item",
        "embeddings",
        "items",
        ["tenant_id", "item_id"],
        ["tenant_id", "id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_embedding_profile_vectors_tenant_item",
        "embedding_profile_vectors",
        "items",
        ["tenant_id", "item_id"],
        ["tenant_id", "id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_item_relationships_tenant_source",
        "item_relationships",
        "items",
        ["tenant_id", "source_item_id"],
        ["tenant_id", "id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_item_relationships_tenant_target",
        "item_relationships",
        "items",
        ["tenant_id", "target_item_id"],
        ["tenant_id", "id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_embeddings_tenant_item_chunk",
        "embeddings",
        ["tenant_id", "item_id", "chunk_index"],
    )
    op.create_index(
        "ix_embedding_profile_vectors_tenant_item",
        "embedding_profile_vectors",
        ["tenant_id", "item_id", "profile_name"],
    )
    op.create_index(
        "ix_item_relationships_tenant_source",
        "item_relationships",
        ["tenant_id", "source_item_id"],
    )
    op.create_index(
        "ix_item_relationships_tenant_target",
        "item_relationships",
        ["tenant_id", "target_item_id"],
    )

    # Correct rows that migration 057 over-classified with LIKE 'hermes%'.
    # Limit the repair to the exact legacy client that migration 057 misclassified.
    op.execute(
        sa.text(
            """
            UPDATE mcp_clients
            SET containment_mode = 'standard'
            WHERE containment_mode = 'hermes_agent'
              AND lower(trim(client_key)) = 'hermesprod'
            """
        )
    )

    for table in TENANT_TABLES:
        op.execute(sa.text(f'ALTER TABLE "{table}" ALTER COLUMN tenant_id DROP DEFAULT'))
        _enable_rls(table)


def downgrade() -> None:
    for table in reversed(TENANT_TABLES):
        op.execute(sa.text(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"'))
        op.execute(sa.text(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY'))

    op.drop_index("ix_item_relationships_tenant_target", table_name="item_relationships")
    op.drop_index("ix_item_relationships_tenant_source", table_name="item_relationships")
    op.drop_index(
        "ix_embedding_profile_vectors_tenant_item",
        table_name="embedding_profile_vectors",
    )
    op.drop_index("ix_embeddings_tenant_item_chunk", table_name="embeddings")
    op.drop_constraint(
        "fk_item_relationships_tenant_target", "item_relationships", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_item_relationships_tenant_source", "item_relationships", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_embedding_profile_vectors_tenant_item",
        "embedding_profile_vectors",
        type_="foreignkey",
    )
    op.drop_constraint("fk_embeddings_tenant_item", "embeddings", type_="foreignkey")
    op.drop_column("item_relationships", "tenant_id")
    op.drop_column("embedding_profile_vectors", "tenant_id")
    op.drop_column("embeddings", "tenant_id")
    op.drop_constraint("uq_items_tenant_id_id", "items", type_="unique")
    op.drop_index(
        "ix_data_lifecycle_audit_events_subject_tenant_id",
        table_name="data_lifecycle_audit_events",
    )
    op.drop_table("data_lifecycle_audit_events")
    op.drop_table("tenant_erasure_states")
