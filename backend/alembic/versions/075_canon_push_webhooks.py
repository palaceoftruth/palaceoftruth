"""Let a repository push tell Palace to sync.

Revision ID: 075_canon_push_webhooks
Revises: 074_sync_source_file_actions
Create Date: 2026-11-04

A canon binding polled on an interval, so a push waited up to fifteen minutes to
reach canon. These two columns let the repository say when it changed.

``webhook_token`` identifies the binding in the hook URL. It is not a secret by
itself: it is a name, and a leaked token can only reach a handler that still
refuses a request without a valid signature. ``webhook_secret_ciphertext`` holds
the signing secret under the same key as the repo credential, so a database copy
does not carry a usable credential.

Both are nullable. A binding with no token has no hook, which is the state every
binding is in until someone asks for one.
"""

from alembic import op
import sqlalchemy as sa


revision = "075_canon_push_webhooks"
down_revision = "074_sync_source_file_actions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sync_sources", sa.Column("webhook_token", sa.Text(), nullable=True))
    op.add_column(
        "sync_sources", sa.Column("webhook_secret_ciphertext", sa.Text(), nullable=True)
    )
    # The token is looked up on every delivery, and two bindings must never answer
    # to the same name.
    op.create_index(
        "uq_sync_sources_webhook_token",
        "sync_sources",
        ["webhook_token"],
        unique=True,
        postgresql_where=sa.text("webhook_token IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_sync_sources_webhook_token", table_name="sync_sources")
    op.drop_column("sync_sources", "webhook_secret_ciphertext")
    op.drop_column("sync_sources", "webhook_token")
