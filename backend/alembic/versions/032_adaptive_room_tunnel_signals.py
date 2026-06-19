"""Add adaptive room tunnel signal columns.

Revision ID: 032_adaptive_room_tunnel_signals
Revises: 031_candidate_curation_artifacts
Create Date: 2026-05-26
"""

from alembic import op


revision = "032_adaptive_room_tunnel_signals"
down_revision = "031_candidate_curation_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE room_tunnels
            ADD COLUMN activation_count integer NOT NULL DEFAULT 0,
            ADD COLUMN stability double precision NOT NULL DEFAULT 1.0,
            ADD COLUMN last_activated_at timestamptz,
            ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now()
        """
    )
    op.execute(
        """
        CREATE INDEX ix_room_tunnels_tenant_stability
        ON room_tunnels (tenant_id, stability DESC, activation_count DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_room_tunnels_tenant_stability")
    op.execute(
        """
        ALTER TABLE room_tunnels
            DROP COLUMN IF EXISTS updated_at,
            DROP COLUMN IF EXISTS last_activated_at,
            DROP COLUMN IF EXISTS stability,
            DROP COLUMN IF EXISTS activation_count
        """
    )
