"""Prepare tenant lifecycle tables and nullable tenant vector keys.

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
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("subject_tenant_id"),
    )
    for table in ("embeddings", "embedding_profile_vectors", "item_relationships"):
        op.add_column(table, sa.Column("tenant_id", sa.Text(), nullable=True))
    # Old replicas can keep inserting while later revisions backfill and
    # validate. These triggers fill the new keys until every writer is updated.
    op.execute(sa.text("""
        CREATE FUNCTION palace_fill_item_tenant_id() RETURNS trigger AS $$
        BEGIN
            IF NEW.tenant_id IS NULL THEN
                SELECT tenant_id INTO NEW.tenant_id FROM items WHERE id = NEW.item_id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """))
    for table in ("embeddings", "embedding_profile_vectors"):
        op.execute(sa.text(f'''
            CREATE TRIGGER trg_{table}_fill_tenant
            BEFORE INSERT OR UPDATE OF item_id, tenant_id ON "{table}"
            FOR EACH ROW EXECUTE FUNCTION palace_fill_item_tenant_id()
        '''))
    op.execute(sa.text("""
        CREATE FUNCTION palace_fill_relationship_tenant_id() RETURNS trigger AS $$
        BEGIN
            IF NEW.tenant_id IS NULL THEN
                SELECT tenant_id INTO NEW.tenant_id FROM items WHERE id = NEW.source_item_id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """))
    op.execute(sa.text("""
        CREATE TRIGGER trg_item_relationships_fill_tenant
        BEFORE INSERT OR UPDATE OF source_item_id, tenant_id ON item_relationships
        FOR EACH ROW EXECUTE FUNCTION palace_fill_relationship_tenant_id()
    """))


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_item_relationships_fill_tenant ON item_relationships")
    op.execute("DROP FUNCTION IF EXISTS palace_fill_relationship_tenant_id()")
    for table in reversed(("embeddings", "embedding_profile_vectors")):
        op.execute(f'DROP TRIGGER IF EXISTS "trg_{table}_fill_tenant" ON "{table}"')
    op.execute("DROP FUNCTION IF EXISTS palace_fill_item_tenant_id()")
    op.drop_column("item_relationships", "tenant_id")
    op.drop_column("embedding_profile_vectors", "tenant_id")
    op.drop_column("embeddings", "tenant_id")
    op.drop_index(
        "ix_data_lifecycle_audit_events_subject_tenant_id",
        table_name="data_lifecycle_audit_events",
    )
    op.drop_table("data_lifecycle_audit_events")
    op.drop_table("tenant_erasure_states")
