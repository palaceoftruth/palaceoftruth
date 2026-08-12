"""Store server-owned curation creator and approver principals.

Revision ID: 068_curation_principals
Revises: 067_tenant_rls_enforcement
"""

from alembic import op
import sqlalchemy as sa


revision = "068_curation_principals"
down_revision = "067_tenant_rls_enforcement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "candidate_curation_artifacts",
        sa.Column(
            "created_by_principal",
            sa.Text(),
            nullable=True,
            server_default="legacy:unknown",
        ),
    )
    op.add_column(
        "candidate_curation_artifacts",
        sa.Column("approved_by_principal", sa.Text(), nullable=True),
    )
    op.add_column(
        "candidate_curation_artifacts",
        sa.Column("approval_decided_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE candidate_curation_artifacts "
        "SET created_by_principal = 'legacy:unknown' "
        "WHERE created_by_principal IS NULL"
    )
    op.alter_column("candidate_curation_artifacts", "created_by_principal", nullable=False)
    op.create_table(
        "tenant_llm_daily_usage",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("usage_day", sa.Date(), nullable=False),
        sa.Column("used_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("tenant_id", "usage_day"),
        sa.CheckConstraint("used_tokens >= 0", name="ck_tenant_llm_daily_usage_nonnegative"),
    )
    op.execute("ALTER TABLE tenant_llm_daily_usage ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenant_llm_daily_usage FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON tenant_llm_daily_usage "
        "USING (current_setting('app.system_access', true) = 'true' "
        "OR tenant_id = current_setting('app.tenant_id', true)) "
        "WITH CHECK ((current_setting('app.system_access', true) = 'true' "
        "OR tenant_id = current_setting('app.tenant_id', true)) "
        "AND NOT EXISTS (SELECT 1 FROM tenant_erasure_states AS erasure "
        "WHERE erasure.subject_tenant_id = tenant_id))"
    )


def downgrade() -> None:
    op.drop_table("tenant_llm_daily_usage")
    op.drop_column("candidate_curation_artifacts", "approval_decided_at")
    op.drop_column("candidate_curation_artifacts", "approved_by_principal")
    op.drop_column("candidate_curation_artifacts", "created_by_principal")
