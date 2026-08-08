"""Add one-time browser extension pairing keys.

Revision ID: 054_capture_pairing_keys
Revises: 053_mcp_tenant_shared_reads
"""

from alembic import op
import sqlalchemy as sa


revision = "054_capture_pairing_keys"
down_revision = "053_mcp_tenant_shared_reads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "browser_extension_pairing_keys",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("credential_hash", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), server_default="browser_extension_token", nullable=False),
        sa.Column("issued_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("purpose = 'browser_extension_token'", name="ck_browser_extension_pairing_keys_purpose"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("credential_hash", name="uq_browser_extension_pairing_keys_credential_hash"),
    )
    op.create_index(
        "ix_browser_extension_pairing_keys_tenant_created_at",
        "browser_extension_pairing_keys",
        ["tenant_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_browser_extension_pairing_keys_tenant_created_at", table_name="browser_extension_pairing_keys")
    op.drop_table("browser_extension_pairing_keys")
