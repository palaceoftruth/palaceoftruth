"""Add durable job attempt lifecycle records.

Revision ID: 043_job_attempts
Revises: 042_resource_refresh_leases
Create Date: 2026-07-11
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "043_job_attempts"
down_revision = "042_resource_refresh_leases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("trigger", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="queued", nullable=False),
        sa.Column("failure_kind", sa.String(length=48), nullable=True),
        sa.Column("error_summary", sa.String(length=500), nullable=True),
        sa.Column("arq_job_id", sa.String(length=255), nullable=True),
        sa.Column("job_try", sa.Integer(), nullable=True),
        sa.Column("recovered_from_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("failed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("dead_lettered_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recovered_from_id"], ["job_attempts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "attempt_number", name="uq_job_attempts_job_attempt"),
    )
    op.create_index("ix_job_attempts_tenant_created", "job_attempts", ["tenant_id", "created_at"])
    op.create_index("ix_job_attempts_job_status", "job_attempts", ["job_id", "status"])
    op.create_index("ix_job_attempts_arq_job_id", "job_attempts", ["arq_job_id"])
    op.create_index(
        "uq_job_attempts_active_job",
        "job_attempts",
        ["job_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'processing')"),
    )


def downgrade() -> None:
    op.drop_index("uq_job_attempts_active_job", table_name="job_attempts")
    op.drop_index("ix_job_attempts_arq_job_id", table_name="job_attempts")
    op.drop_index("ix_job_attempts_job_status", table_name="job_attempts")
    op.drop_index("ix_job_attempts_tenant_created", table_name="job_attempts")
    op.drop_table("job_attempts")
