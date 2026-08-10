"""Let a source subscription opt in to capturing YouTube live streams.

Revision ID: 056_capture_live_streams
Revises: 055_api_key_scopes

Discovery skipped every YouTube entry with a live status, so a channel that
publishes only as live streams produced nothing but ``youtube_live_unsupported``
skips. The new ``capture_live_streams`` flag makes that behaviour per
subscription. Existing rows keep the historic behaviour (``false``).
"""

from alembic import op
import sqlalchemy as sa


revision = "056_capture_live_streams"
down_revision = "055_api_key_scopes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "source_subscriptions",
        sa.Column("capture_live_streams", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("source_subscriptions", "capture_live_streams")
