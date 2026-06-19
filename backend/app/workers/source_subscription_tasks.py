"""ARQ task functions for source subscription discovery and capture queueing."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.database import async_session
from app.models.source_subscription import SourceSubscription, SourceSubscriptionEntry
from app.services.source_subscriptions import (
    diagnose_stale_queued_source_subscription_entries,
    poll_source_subscription,
    queue_source_subscription_entry,
)

logger = logging.getLogger(__name__)


async def poll_all_source_subscriptions(ctx: dict) -> None:
    """Dispatch due active source subscriptions without doing capture inline."""
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        result = await db.execute(
            select(SourceSubscription)
            .where(SourceSubscription.status == "active")
            .where(SourceSubscription.deleted_at.is_(None))
        )
        subscriptions = result.scalars().all()

    dispatched = 0
    for subscription in subscriptions:
        last_checked_at = subscription.last_checked_at
        if last_checked_at is not None and last_checked_at.tzinfo is None:
            last_checked_at = last_checked_at.replace(tzinfo=timezone.utc)
        if last_checked_at is not None:
            due_at = last_checked_at.timestamp() + int(subscription.poll_interval_seconds or 3600)
            if due_at > now.timestamp():
                continue
        await ctx["redis"].enqueue_job(
            "poll_source_subscription_task",
            subscription_id=str(subscription.id),
            tenant_id=subscription.tenant_id,
        )
        dispatched += 1

    logger.info("poll_all_source_subscriptions dispatched=%d", dispatched)


async def poll_source_subscription_task(
    ctx: dict,
    subscription_id: str,
    tenant_id: str = "default",
) -> None:
    """Discover new source entries and enqueue newly discovered captures."""
    async with async_session() as db:
        subscription = await db.get(SourceSubscription, uuid.UUID(subscription_id))
        if (
            subscription is None
            or subscription.tenant_id != tenant_id
            or subscription.status != "active"
            or subscription.deleted_at is not None
        ):
            logger.info("poll_source_subscription skipping subscription_id=%s", subscription_id)
            return

        entries = await poll_source_subscription(db, subscription)
        await db.commit()

        queued = 0
        for entry in entries:
            if entry.status != "discovered":
                continue
            if await queue_source_subscription_entry(db, ctx["redis"], subscription, entry):
                queued += 1

    logger.info("poll_source_subscription subscription_id=%s queued=%d", subscription_id, queued)


async def queue_discovered_source_subscription_entries(ctx: dict, limit: int = 100) -> None:
    """Recover discovered entries left behind by earlier queue failures."""
    async with async_session() as db:
        result = await db.execute(
            select(SourceSubscriptionEntry.id)
            .where(SourceSubscriptionEntry.status == "discovered")
            .order_by(SourceSubscriptionEntry.discovered_at.asc())
            .limit(limit)
        )
        entry_ids = [row[0] for row in result.fetchall()]

    queued = 0
    for entry_id in entry_ids:
        async with async_session() as db:
            entry = await db.get(SourceSubscriptionEntry, entry_id)
            if entry is None or entry.status != "discovered":
                continue
            subscription = await db.get(SourceSubscription, entry.subscription_id)
            if (
                subscription is None
                or subscription.tenant_id != entry.tenant_id
                or subscription.status != "active"
                or subscription.deleted_at is not None
            ):
                continue
            if await queue_source_subscription_entry(db, ctx["redis"], subscription, entry):
                queued += 1

    logger.info("queue_discovered_source_subscription_entries queued=%d", queued)


async def diagnose_stale_queued_source_subscription_entries_task(ctx: dict, limit: int = 100) -> None:
    """Annotate or reconcile queued entries that did not reach a terminal capture state."""
    async with async_session() as db:
        diagnosed = await diagnose_stale_queued_source_subscription_entries(db, limit=limit)
    logger.info("diagnose_stale_queued_source_subscription_entries diagnosed=%d", diagnosed)
