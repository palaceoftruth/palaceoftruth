"""Null out webhook signing keys copied from a credential verifier (H-09).

jobs.signing_key used to be populated with request.state.key_hash -- the
caller's own API-key hash, reused as the webhook HMAC key. Any row written
before the app.auth.generate_webhook_signing_key() fix is a live credential
verifier sitting in a column that was never treated as sensitive, and every
one of those webhook signatures is forgeable by anyone who already knows the
corresponding API key hash (which, pre-pepper, is close to the key itself).

There is no way to safely re-derive a correct signing key for these rows
after the fact -- the old value must be discarded, not migrated. Any
in-flight webhook subscription loses its signature verification until the
job is re-created, which is the intended outcome: a forgeable key is worse
than no key.

Revision ID: 060_null_legacy_webhook_signing_keys
Revises: 059_scope_authority_lifecycle
"""

from alembic import op


revision = "060_null_legacy_webhook_signing_keys"
down_revision = "059_scope_authority_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE jobs SET signing_key = NULL WHERE signing_key IS NOT NULL")


def downgrade() -> None:
    # The discarded values cannot be, and must not be, reconstructed -- they
    # were live credential-verifier copies. Downgrade is a structural no-op;
    # rolling back this revision does not un-null any row.
    pass
