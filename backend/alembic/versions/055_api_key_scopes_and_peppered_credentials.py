"""Persist per-key MCP scopes and prepare peppered credential verifiers.

Revision ID: 055_api_key_scopes
Revises: 054_capture_pairing_keys

Two related changes land together because they both reshape ``api_keys``:

1. ``api_keys.scopes`` becomes the authoritative grant for a tenant API key.
   Before this, an API key had no stored scope at all: every REST capability
   gate passed unconditionally, and the MCP gate trusted the caller's
   ``X-MCP-Scope`` header. Existing rows are backfilled with the routine
   non-admin grant. Administrative authority must always be added by an
   explicit operator action.

2. Credential verifier columns move from unsalted SHA-256 to peppered
   HMAC-SHA256. The raw credential is not recoverable from the database, so
   this migration cannot rewrite the stored digests. Instead the new format is
   self-describing (``hmac-sha256$<hex>``), the application reads both formats,
   and each row is rewritten the next time its credential authenticates. The
   affected columns are all ``text`` and stay ``text``, so there is no schema
   change to make for that half — the format change is recorded here so the
   two halves of the ``api_keys`` rewrite stay in one revision.
"""

import json

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "055_api_key_scopes"
down_revision = "054_capture_pairing_keys"
branch_labels = None
depends_on = None


# Duplicated from app.mcp_scopes.DEFAULT_API_KEY_SCOPES on purpose: migrations
# must remain stable when application defaults change later.
DEFAULT_API_KEY_SCOPES = [
    "read",
    "write",
    "write:agent",
    "write:workspace",
    "write:session",
    "audit:write",
    "capture:write",
    "capture:job:read",
]


def upgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column("scopes", postgresql.JSONB(), nullable=True),
    )
    op.execute(
        sa.text("UPDATE api_keys SET scopes = CAST(:scopes AS jsonb) WHERE scopes IS NULL").bindparams(
            scopes=json.dumps(DEFAULT_API_KEY_SCOPES)
        )
    )
    op.alter_column(
        "api_keys",
        "scopes",
        existing_type=postgresql.JSONB(),
        nullable=False,
        server_default=sa.text("'[]'::jsonb"),
    )


def downgrade() -> None:
    op.drop_column("api_keys", "scopes")
