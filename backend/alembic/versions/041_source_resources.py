"""Add tenant-scoped HTTP source resources and refresh audit history.

Revision ID: 041_source_resources
Revises: 040_semantic_memory_reflection
Create Date: 2026-07-10
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "041_source_resources"
down_revision = "040_semantic_memory_reflection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing source_records predate tenant-qualified resource pointers. The
    # redundant pair lets PostgreSQL reject cross-tenant version references.
    op.create_unique_constraint("uq_source_records_tenant_id_id", "source_records", ["tenant_id", "id"])
    op.create_table(
        "source_resources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="http"),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("canonical_identity", sa.Text(), nullable=False),
        sa.Column("refresh_policy", sa.String(length=20), nullable=False, server_default="interval"),
        sa.Column("refresh_slo_seconds", sa.Integer(), nullable=False, server_default="86400"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("validator_etag", sa.Text(), nullable=True),
        sa.Column("validator_last_modified", sa.Text(), nullable=True),
        sa.Column("content_digest", sa.String(length=128), nullable=True),
        sa.Column("robots_allowed", sa.Boolean(), nullable=True),
        sa.Column("robots_decision", sa.String(length=20), nullable=True),
        sa.Column("robots_cached_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_http_status", sa.Integer(), nullable=True),
        sa.Column("last_failure_reason", sa.Text(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("published_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("captured_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_verified_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("content_changed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_checked_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_success_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("next_due_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("backoff_until", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("current_source_record_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("last_successful_source_record_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "current_source_record_id"], ["source_records.tenant_id", "source_records.id"],
            ondelete="RESTRICT", name="fk_source_resources_current_record_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "last_successful_source_record_id"], ["source_records.tenant_id", "source_records.id"],
            ondelete="RESTRICT", name="fk_source_resources_last_success_record_tenant",
        ),
        sa.UniqueConstraint(
            "tenant_id", "kind", "canonical_identity", name="uq_source_resources_tenant_kind_identity"
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_source_resources_tenant_id_id"),
        sa.CheckConstraint("kind IN ('http')", name="ck_source_resources_kind"),
        sa.CheckConstraint(
            "status IN ('active', 'unreachable', 'gone', 'paused')", name="ck_source_resources_status"
        ),
        sa.CheckConstraint(
            "refresh_policy IN ('manual', 'interval', 'adaptive')", name="ck_source_resources_refresh_policy"
        ),
        sa.CheckConstraint("refresh_slo_seconds > 0", name="ck_source_resources_refresh_slo"),
        sa.CheckConstraint("consecutive_failures >= 0", name="ck_source_resources_failure_count"),
    )
    op.create_index("ix_source_resources_tenant_next_due", "source_resources", ["tenant_id", "next_due_at"])
    op.create_index("ix_source_resources_tenant_status", "source_resources", ["tenant_id", "status"])

    op.create_table(
        "source_resource_aliases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("submitted_url", sa.Text(), nullable=False),
        sa.Column("final_url", sa.Text(), nullable=True),
        sa.Column("canonical_signal_url", sa.Text(), nullable=True),
        sa.Column("normalized_url", sa.Text(), nullable=False),
        sa.Column("signal", sa.String(length=20), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("observed_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "resource_id"], ["source_resources.tenant_id", "source_resources.id"],
            ondelete="RESTRICT", name="fk_source_resource_aliases_resource_tenant",
        ),
        sa.UniqueConstraint(
            "tenant_id", "resource_id", "signal", "normalized_url", name="uq_source_resource_alias_signal_url"
        ),
        sa.CheckConstraint("signal IN ('submitted', 'final', 'canonical')", name="ck_source_resource_alias_signal"),
        sa.CheckConstraint(
            "decision IN ('accepted', 'rejected', 'conflict')", name="ck_source_resource_alias_decision"
        ),
    )
    op.create_index(
        "ix_source_resource_aliases_tenant_url", "source_resource_aliases", ["tenant_id", "normalized_url"]
    )

    op.create_table(
        "source_resource_audit_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_kind", sa.String(length=40), nullable=False),
        sa.Column("previous_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("next_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("recorded_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "resource_id"], ["source_resources.tenant_id", "source_resources.id"],
            ondelete="RESTRICT", name="fk_source_resource_audit_resource_tenant",
        ),
    )
    op.create_index(
        "ix_source_resource_audit_resource_recorded",
        "source_resource_audit_snapshots",
        ["resource_id", "recorded_at"],
    )
    # Database enforcement keeps audit history immutable even if a future caller
    # bypasses the service layer.
    op.execute(
        """
        CREATE FUNCTION reject_source_resource_audit_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'source resource audit snapshots are append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER source_resource_audit_append_only
        BEFORE UPDATE OR DELETE ON source_resource_audit_snapshots
        FOR EACH ROW EXECUTE FUNCTION reject_source_resource_audit_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS source_resource_audit_append_only ON source_resource_audit_snapshots")
    op.execute("DROP FUNCTION IF EXISTS reject_source_resource_audit_mutation()")
    op.drop_index("ix_source_resource_audit_resource_recorded", table_name="source_resource_audit_snapshots")
    op.drop_table("source_resource_audit_snapshots")
    op.drop_index("ix_source_resource_aliases_tenant_url", table_name="source_resource_aliases")
    op.drop_table("source_resource_aliases")
    op.drop_index("ix_source_resources_tenant_status", table_name="source_resources")
    op.drop_index("ix_source_resources_tenant_next_due", table_name="source_resources")
    op.drop_table("source_resources")
    op.drop_constraint("uq_source_records_tenant_id_id", "source_records", type_="unique")
