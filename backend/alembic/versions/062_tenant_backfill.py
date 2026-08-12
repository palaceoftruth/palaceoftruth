"""Backfill tenant isolation data without schema locks.

Revision ID: 062_tenant_backfill
Revises: 061_tenant_rls
"""

from alembic import op
import sqlalchemy as sa


revision = "062_tenant_backfill"
down_revision = "061_tenant_rls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("""
        UPDATE items SET
            metadata = COALESCE(metadata, '{}'::jsonb),
            tags = COALESCE(tags, '{}'::text[]),
            categories = COALESCE(categories, '{}'::text[]),
            status = COALESCE(status, 'processing'),
            created_at = COALESCE(created_at, now()),
            updated_at = COALESCE(updated_at, created_at, now())
        WHERE metadata IS NULL OR tags IS NULL OR categories IS NULL
           OR status IS NULL OR created_at IS NULL OR updated_at IS NULL
    """))
    op.execute(sa.text("""
        UPDATE jobs SET
            status = COALESCE(status, 'queued'),
            progress = COALESCE(progress, 0),
            created_at = COALESCE(created_at, now())
        WHERE status IS NULL OR progress IS NULL OR created_at IS NULL
    """))
    op.execute(sa.text(
        "UPDATE embeddings SET created_at = now() WHERE created_at IS NULL"
    ))
    op.execute(sa.text(
        "UPDATE embedding_profile_vectors SET created_at = now() WHERE created_at IS NULL"
    ))
    op.execute(sa.text("""
        UPDATE item_relationships SET
            confidence = COALESCE(confidence, 0.0),
            created_at = COALESCE(created_at, now())
        WHERE confidence IS NULL OR created_at IS NULL
    """))

    for table in ("embeddings", "embedding_profile_vectors"):
        op.execute(sa.text(f'''
            UPDATE "{table}" AS child
            SET tenant_id = item.tenant_id
            FROM items AS item
            WHERE item.id = child.item_id AND child.tenant_id IS NULL
        '''))
    op.execute(sa.text("""
        UPDATE item_relationships AS relationship
        SET tenant_id = source.tenant_id
        FROM items AS source, items AS target
        WHERE source.id = relationship.source_item_id
          AND target.id = relationship.target_item_id
          AND source.tenant_id = target.tenant_id
          AND relationship.tenant_id IS NULL
    """))
    cross_tenant = op.get_bind().execute(
        sa.text("SELECT COUNT(*) FROM item_relationships WHERE tenant_id IS NULL")
    ).scalar_one()
    if cross_tenant:
        raise RuntimeError(
            "item_relationships contains missing or cross-tenant endpoints; "
            "repair those rows before applying tenant RLS"
        )

    # Do not weaken an existing containment decision here. Older deployments
    # did not record whether hermes_agent was selected explicitly or came from
    # the original broad prefix backfill. Resetting those indistinguishable rows
    # would silently remove an operator-selected security boundary. Fresh
    # installs use migration 057's exact normalized namespace predicate.


def downgrade() -> None:
    # Data repairs are safe to retain when the schema is rolled back.
    pass
