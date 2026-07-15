"""Add bounded relationship graph lookup indexes.

Revision ID: 046
Revises: 045_embeddings_item_chunk_index
Create Date: 2026-07-15
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "046"
down_revision: Union[str, None] = "045_embeddings_item_chunk_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Match the two directional per-seed LATERAL lookups in SearchService.
    op.create_index(
        "ix_item_relationships_source_confidence_target",
        "item_relationships",
        ["source_item_id", sa.text("confidence DESC"), "target_item_id", "relationship"],
    )
    op.create_index(
        "ix_item_relationships_target_confidence_source",
        "item_relationships",
        ["target_item_id", sa.text("confidence DESC"), "source_item_id", "relationship"],
    )


def downgrade() -> None:
    op.drop_index("ix_item_relationships_target_confidence_source", table_name="item_relationships")
    op.drop_index("ix_item_relationships_source_confidence_target", table_name="item_relationships")
