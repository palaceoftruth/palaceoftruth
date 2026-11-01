"""Bind a sync source to the tenant canon.

Revision ID: 073_repo_canon_bindings
Revises: 072_tenant_canon_pages
Create Date: 2026-11-01

Purely additive. A sync source gains a target: ``rooms`` (the behaviour every
existing row keeps) or ``canon`` (a repo whose markdown becomes canon pages in
packet P8). The file row records the slug a page was derived from, and the run
records the commit it read plus per-page counters beside the existing per-file
ones.

Every column added here carries a server default or is nullable, so the upgrade
rewrites existing rows as ``target_kind = 'rooms'`` without a backfill statement.

The section literals are repeated here rather than imported from
``app.models.canon`` on purpose: a migration must keep applying the values that
existed when it was written, even after the application tuples move on.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "073_repo_canon_bindings"
down_revision = "072_tenant_canon_pages"
branch_labels = None
depends_on = None


TARGET_KIND_SQL = "target_kind IN ('rooms', 'canon')"
CANON_SECTION_SQL = (
    "canon_section_default IN "
    "('identity', 'rules', 'glossary', 'runbook', 'charter', 'faq', 'reference')"
)
PAGE_COUNTS_SQL = (
    "pages_created >= 0 AND pages_updated >= 0 "
    "AND pages_archived >= 0 AND pages_conflicted >= 0"
)

CANON_ROOM_FK = "fk_sync_sources_canon_room_id"


def upgrade() -> None:
    op.add_column(
        "sync_sources",
        sa.Column(
            "target_kind",
            sa.Text(),
            nullable=False,
            server_default="rooms",
        ),
    )
    op.add_column(
        "sync_sources",
        sa.Column(
            "canon_section_default",
            sa.Text(),
            nullable=False,
            server_default="reference",
        ),
    )
    op.add_column(
        "sync_sources",
        sa.Column("canon_room_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "sync_sources",
        sa.Column(
            "canon_owns_pages",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.create_foreign_key(
        CANON_ROOM_FK,
        "sync_sources",
        "rooms",
        ["canon_room_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column(
        "sync_source_files",
        sa.Column("page_slug", sa.Text(), nullable=True),
    )

    op.add_column(
        "sync_runs",
        sa.Column("commit_sha", sa.Text(), nullable=True),
    )
    for counter in ("pages_created", "pages_updated", "pages_archived", "pages_conflicted"):
        op.add_column(
            "sync_runs",
            sa.Column(counter, sa.Integer(), nullable=False, server_default="0"),
        )

    op.create_check_constraint(
        "ck_sync_sources_target_kind",
        "sync_sources",
        TARGET_KIND_SQL,
    )
    op.create_check_constraint(
        "ck_sync_sources_canon_section",
        "sync_sources",
        CANON_SECTION_SQL,
    )
    op.create_check_constraint(
        "ck_sync_runs_page_counts",
        "sync_runs",
        PAGE_COUNTS_SQL,
    )


def downgrade() -> None:
    op.drop_constraint("ck_sync_runs_page_counts", "sync_runs", type_="check")
    op.drop_constraint("ck_sync_sources_canon_section", "sync_sources", type_="check")
    op.drop_constraint("ck_sync_sources_target_kind", "sync_sources", type_="check")

    for counter in ("pages_conflicted", "pages_archived", "pages_updated", "pages_created"):
        op.drop_column("sync_runs", counter)
    op.drop_column("sync_runs", "commit_sha")

    op.drop_column("sync_source_files", "page_slug")

    op.drop_constraint(CANON_ROOM_FK, "sync_sources", type_="foreignkey")
    op.drop_column("sync_sources", "canon_owns_pages")
    op.drop_column("sync_sources", "canon_room_id")
    op.drop_column("sync_sources", "canon_section_default")
    op.drop_column("sync_sources", "target_kind")
