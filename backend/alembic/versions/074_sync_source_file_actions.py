"""Record what a canon sync run did to each path.

Revision ID: 074_sync_source_file_actions
Revises: 073_repo_canon_bindings
Create Date: 2026-11-02

Purely additive. A per-path row already keeps the hash, the status and the run
that last saw it; ``last_action`` adds what that run *did* to the path, so the
Canon route can say a file created a page, updated one, conflicted, was skipped
because nothing changed, was archived after leaving the repo, or failed to parse.

The column is nullable with no server default: rows written before this revision
have no recorded action, and an absent value says exactly that rather than
inventing one.
"""

from alembic import op
import sqlalchemy as sa


revision = "074_sync_source_file_actions"
down_revision = "073_repo_canon_bindings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sync_source_files",
        sa.Column("last_action", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sync_source_files", "last_action")
