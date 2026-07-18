"""Persist one-use refresh-token history for replay-safe rotation."""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "050_refresh_rotation_history"
down_revision: Union[str, None] = "049_oauth_code_bindings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "mcp_oauth_refresh_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("family_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("issued_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.UniqueConstraint("token_hash", name="uq_mcp_oauth_refresh_tokens_hash"),
        sa.ForeignKeyConstraint(["family_id"], ["mcp_oauth_refresh_token_families.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_mcp_oauth_refresh_tokens_family", "mcp_oauth_refresh_tokens", ["family_id"])
    # Existing pre-refresh access tokens remain valid; newly minted delegated
    # tokens carry this nullable family link for targeted reuse revocation.
    op.add_column("mcp_oauth_access_tokens", sa.Column("refresh_token_family_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_mcp_oauth_access_tokens_refresh_family", "mcp_oauth_access_tokens", "mcp_oauth_refresh_token_families", ["refresh_token_family_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    op.drop_constraint("fk_mcp_oauth_access_tokens_refresh_family", "mcp_oauth_access_tokens", type_="foreignkey")
    op.drop_column("mcp_oauth_access_tokens", "refresh_token_family_id")
    op.drop_index("ix_mcp_oauth_refresh_tokens_family", table_name="mcp_oauth_refresh_tokens")
    op.drop_table("mcp_oauth_refresh_tokens")
