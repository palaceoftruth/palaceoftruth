"""Attach tenant constraints using short metadata locks.

Revision ID: 066_tenant_constraints
Revises: 065_tenant_indexes_online
"""

from alembic import op


revision = "066_tenant_constraints"
down_revision = "065_tenant_indexes_online"
branch_labels = None
depends_on = None


FOREIGN_KEYS = (
    ("fk_embeddings_tenant_item", "embeddings", "item_id"),
    ("fk_embedding_profile_vectors_tenant_item", "embedding_profile_vectors", "item_id"),
    ("fk_item_relationships_tenant_source", "item_relationships", "source_item_id"),
    ("fk_item_relationships_tenant_target", "item_relationships", "target_item_id"),
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE items ADD CONSTRAINT uq_items_tenant_id_id "
        "UNIQUE USING INDEX ix_items_tenant_id_id_unique"
    )
    for name, table, item_column in FOREIGN_KEYS:
        op.execute("SET LOCAL lock_timeout = '5s'")
        op.execute(
            f'ALTER TABLE "{table}" ADD CONSTRAINT "{name}" '
            f'FOREIGN KEY (tenant_id, "{item_column}") '
            "REFERENCES items (tenant_id, id) ON DELETE CASCADE NOT VALID"
        )


def downgrade() -> None:
    for name, table, _item_column in reversed(FOREIGN_KEYS):
        op.execute("SET LOCAL lock_timeout = '5s'")
        op.drop_constraint(name, table, type_="foreignkey")
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("uq_items_tenant_id_id", "items", type_="unique")
    # UNIQUE USING INDEX transfers index ownership to the constraint. Restore
    # revision 065's standalone index so a later upgrade can attach it again.
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_items_tenant_id_id_unique",
            "items",
            ["tenant_id", "id"],
            unique=True,
            postgresql_concurrently=True,
        )
