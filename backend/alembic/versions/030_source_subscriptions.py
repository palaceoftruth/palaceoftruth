"""Add source subscription foundation.

Revision ID: 030_source_subscriptions
Revises: 029_web_saves
Create Date: 2026-05-14
"""

from alembic import op


revision = "030_source_subscriptions"
down_revision = "029_web_saves"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE source_subscriptions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id text NOT NULL DEFAULT 'default',
            provider_type varchar(80) NOT NULL,
            source_url text NOT NULL,
            external_id text,
            external_url text,
            display_name text,
            status varchar(20) NOT NULL DEFAULT 'active',
            auto_tags text[] NOT NULL DEFAULT '{}',
            poll_interval_seconds integer NOT NULL DEFAULT 3600,
            cursor jsonb NOT NULL DEFAULT '{}',
            provider_metadata jsonb NOT NULL DEFAULT '{}',
            last_checked_at timestamptz,
            last_discovered_at timestamptz,
            last_error text,
            consecutive_failures integer NOT NULL DEFAULT 0,
            paused_reason text,
            deleted_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE source_subscription_entries (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id text NOT NULL DEFAULT 'default',
            subscription_id uuid NOT NULL REFERENCES source_subscriptions(id) ON DELETE CASCADE,
            provider_entry_id text,
            source_url text,
            title text,
            published_at timestamptz,
            discovered_at timestamptz NOT NULL DEFAULT now(),
            status varchar(20) NOT NULL DEFAULT 'discovered',
            skip_reason text,
            error_message text,
            item_id uuid REFERENCES items(id) ON DELETE SET NULL,
            job_id uuid REFERENCES jobs(id) ON DELETE SET NULL,
            queued_at timestamptz,
            captured_at timestamptz,
            skipped_at timestamptz,
            failed_at timestamptz,
            metadata jsonb NOT NULL DEFAULT '{}',
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_source_subscriptions_active_external
        ON source_subscriptions (tenant_id, provider_type, external_id)
        WHERE deleted_at IS NULL AND external_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_source_subscription_entries_provider_entry
        ON source_subscription_entries (tenant_id, subscription_id, provider_entry_id)
        WHERE provider_entry_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_source_subscription_entries_source_url
        ON source_subscription_entries (tenant_id, subscription_id, source_url)
        WHERE source_url IS NOT NULL
        """
    )
    op.execute("CREATE INDEX ix_source_subscriptions_tenant_status ON source_subscriptions (tenant_id, status)")
    op.execute("CREATE INDEX ix_source_subscriptions_tenant_deleted ON source_subscriptions (tenant_id, deleted_at)")
    op.execute(
        "CREATE INDEX ix_source_subscription_entries_subscription_status "
        "ON source_subscription_entries (subscription_id, status)"
    )
    op.execute(
        "CREATE INDEX ix_source_subscription_entries_tenant_status "
        "ON source_subscription_entries (tenant_id, status)"
    )
    op.execute("CREATE INDEX ix_source_subscription_entries_job_id ON source_subscription_entries (job_id)")
    op.execute("CREATE INDEX ix_source_subscription_entries_item_id ON source_subscription_entries (item_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS source_subscription_entries")
    op.execute("DROP TABLE IF EXISTS source_subscriptions")
