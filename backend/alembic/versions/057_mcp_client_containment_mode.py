"""Make agent containment a server-owned column instead of a name prefix.

Revision ID: 057_mcp_containment_mode
Revises: 056_capture_live_streams

Containment for memory-plugin agents was decided by testing whether the
caller-chosen ``client_key`` started with ``hermes-``. The registrant picks that
string, so registering as ``hermes_prod`` turned every containment guard off.
``containment_mode`` moves the decision onto a column the registration path
owns. Existing ``hermes``-named rows are backfilled so their guards keep
firing.
"""

from alembic import op
import sqlalchemy as sa


revision = "057_mcp_containment_mode"
down_revision = "056_capture_live_streams"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "mcp_clients",
        sa.Column(
            "containment_mode",
            sa.Text(),
            server_default="standard",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_mcp_clients_containment_mode",
        "mcp_clients",
        "containment_mode IN ('standard', 'hermes_agent')",
    )
    # Match the runtime normalizer exactly: fold one or more non-alphanumeric
    # separators to '-', trim separators, then reserve only 'hermes' and the
    # 'hermes-' namespace. A key such as 'hermesprod' is not reserved.
    op.execute(
        """
        UPDATE mcp_clients
        SET containment_mode = 'hermes_agent'
        WHERE trim(both '-' from regexp_replace(lower(trim(client_key)), '[^a-z0-9]+', '-', 'g')) = 'hermes'
           OR trim(both '-' from regexp_replace(lower(trim(client_key)), '[^a-z0-9]+', '-', 'g')) LIKE 'hermes-%'
        """
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_mcp_clients_containment_mode",
        "mcp_clients",
        type_="check",
    )
    op.drop_column("mcp_clients", "containment_mode")
