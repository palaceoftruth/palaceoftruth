"""Add tenant canon pages, revisions, and the digest cache.

Revision ID: 072_tenant_canon_pages
Revises: 071_source_drift_artifacts
Create Date: 2026-10-30

A canon page is an ``items`` row (``source_type = 'canon'``) plus the editorial
metadata in ``canon_pages``. This revision only adds tables, indexes and one
check-constraint widening on ``candidate_curation_artifacts``.

The section/status literals are repeated here rather than imported from
``app.models.canon`` on purpose: a migration must keep applying the values that
existed when it was written, even after the application tuples move on.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "072_tenant_canon_pages"
down_revision = "071_source_drift_artifacts"
branch_labels = None
depends_on = None


CANON_TABLES = ("canon_pages", "canon_page_revisions", "canon_digest_cache")

# Mirrors CANON_SECTIONS in app/models/canon.py at the time of this revision.
CANON_SECTION_SQL = (
    "section IN ('identity', 'rules', 'glossary', 'runbook', 'charter', 'faq', 'reference')"
)
CANON_STATUS_SQL = "status IN ('draft', 'published', 'archived')"

# The five kinds that existed after revision 071, copied verbatim from it.
ARTIFACT_KINDS_BEFORE = (
    "artifact_kind IN ('candidate_skill', 'candidate_routing_manifest', "
    "'candidate_prompt_guardrail', 'candidate_memory_reflection', 'candidate_source_drift')"
)
ARTIFACT_KINDS_AFTER = (
    "artifact_kind IN ('candidate_skill', 'candidate_routing_manifest', "
    "'candidate_prompt_guardrail', 'candidate_memory_reflection', 'candidate_source_drift', "
    "'candidate_canon_edit')"
)


def _enable_rls(table: str) -> None:
    """Enable forced RLS with the same policy body as revision 067."""
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
    op.execute(sa.text(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY'))
    op.execute(sa.text(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"'))
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text(f'''
        CREATE POLICY tenant_isolation ON "{table}"
        USING (
            current_setting('app.system_access', true) = 'true'
            OR tenant_id = current_setting('app.tenant_id', true)
        )
        WITH CHECK (
            (
                current_setting('app.system_access', true) = 'true'
                OR tenant_id = current_setting('app.tenant_id', true)
            )
            AND NOT EXISTS (
                SELECT 1 FROM tenant_erasure_states AS erasure
                WHERE erasure.subject_tenant_id = tenant_id
            )
        )
    '''))


def _disable_rls(table: str) -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"'))
    op.execute(sa.text(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY'))
    op.execute(sa.text(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY'))


def upgrade() -> None:
    op.create_table(
        "canon_pages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "room_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("rooms.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("section", sa.Text(), nullable=False),
        sa.Column("parent_slug", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("pinned_in_digest", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("status", sa.Text(), nullable=False, server_default="published"),
        sa.Column("last_confirmed_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_confirmed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("char_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by_subject", sa.Text(), nullable=True),
        sa.Column("updated_by_subject", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("tenant_id", "slug", name="uq_canon_pages_tenant_slug"),
        sa.CheckConstraint(CANON_SECTION_SQL, name="ck_canon_pages_section"),
        sa.CheckConstraint(CANON_STATUS_SQL, name="ck_canon_pages_status"),
        sa.CheckConstraint(
            "char_count >= 0 AND char_count <= 20000",
            name="ck_canon_pages_char_count",
        ),
        sa.CheckConstraint(
            "parent_slug IS NULL OR parent_slug <> slug",
            name="ck_canon_pages_parent_not_self",
        ),
    )
    # One published charter per room: a room may hold at most one governing
    # page, while draft/archived charters stay unconstrained for editing.
    op.create_index(
        "uq_canon_pages_tenant_room_charter",
        "canon_pages",
        ["tenant_id", "room_id"],
        unique=True,
        postgresql_where=sa.text(
            "section = 'charter' AND status = 'published' AND room_id IS NOT NULL"
        ),
    )
    op.create_index(
        "ix_canon_pages_tenant_room_published",
        "canon_pages",
        ["tenant_id", "room_id"],
        postgresql_where=sa.text("status = 'published'"),
    )
    op.create_index(
        "ix_canon_pages_tenant_status_priority",
        "canon_pages",
        ["tenant_id", "status", "priority"],
    )

    op.create_table(
        "canon_page_revisions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column(
            "page_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("canon_pages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("body_md", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("change_note", sa.Text(), nullable=True),
        sa.Column("created_by_subject", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "page_id",
            "revision",
            name="uq_canon_page_revisions_page_revision",
        ),
    )

    op.create_table(
        "canon_digest_cache",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("digest_text", sa.Text(), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column(
            "built_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.drop_constraint(
        "ck_candidate_curation_artifact_kind",
        "candidate_curation_artifacts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_candidate_curation_artifact_kind",
        "candidate_curation_artifacts",
        ARTIFACT_KINDS_AFTER,
    )

    for table in CANON_TABLES:
        _enable_rls(table)


def downgrade() -> None:
    # Canon edit artifacts cannot survive the narrower kind list; park them
    # under the reflection kind so the rows stay reviewable, as 071 does.
    op.execute(
        """
        UPDATE candidate_curation_artifacts
        SET metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
                'legacy_canon_edit',
                jsonb_build_object('artifact_kind', artifact_kind)
            ),
            artifact_kind = 'candidate_memory_reflection'
        WHERE artifact_kind = 'candidate_canon_edit'
        """
    )
    op.drop_constraint(
        "ck_candidate_curation_artifact_kind",
        "candidate_curation_artifacts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_candidate_curation_artifact_kind",
        "candidate_curation_artifacts",
        ARTIFACT_KINDS_BEFORE,
    )

    for table in reversed(CANON_TABLES):
        _disable_rls(table)

    op.drop_table("canon_digest_cache")
    op.drop_table("canon_page_revisions")
    # Indexes on canon_pages are dropped with the table.
    op.drop_table("canon_pages")
