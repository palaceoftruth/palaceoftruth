"""Bounded, no-network dispatch primitives for watched HTTP resources.

The HTTP provider, robots policy, and content activation deliberately land in
later slices.  This module only establishes durable lease ownership so those
slices cannot fan out duplicate work after concurrent dispatches or restarts.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session
from app.models.source_resource import SourceResource
from app.services.source_resources import RefreshLease, claim_refresh_lease, refresh_lease_job_id

logger = logging.getLogger(__name__)


async def claim_due_source_resources(
    db: AsyncSession,
    *,
    now: datetime,
    limit: int,
    lease_seconds: int,
) -> list[RefreshLease]:
    """Claim a capped batch with row locks; callers commit before enqueueing."""

    if limit <= 0:
        raise ValueError("limit must be positive")
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    result = await db.execute(
        select(SourceResource)
        .where(SourceResource.kind == "http")
        .where(SourceResource.refresh_policy != "manual")
        .where(SourceResource.status.in_(("active", "unreachable")))
        .where(SourceResource.next_due_at.is_not(None))
        .where(SourceResource.next_due_at <= now)
        .where(or_(SourceResource.backoff_until.is_(None), SourceResource.backoff_until <= now))
        .where(
            or_(
                SourceResource.refresh_lease_expires_at.is_(None),
                SourceResource.refresh_lease_expires_at <= now,
            )
        )
        .order_by(SourceResource.next_due_at.asc(), SourceResource.id.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    leases: list[RefreshLease] = []
    for resource in result.scalars().all():
        lease = claim_refresh_lease(resource, now=now, lease_seconds=lease_seconds)
        if lease is not None:
            db.add(resource)
            leases.append(lease)
    await db.flush()
    return leases


async def dispatch_due_source_resources(ctx: dict) -> int:
    """Claim and enqueue a bounded refresh batch only when explicitly enabled."""

    if not settings.source_resource_refresh_dispatch_enabled:
        logger.debug("source resource refresh dispatch disabled")
        return 0

    now = datetime.now(timezone.utc)
    async with async_session() as db:
        leases = await claim_due_source_resources(
            db,
            now=now,
            limit=settings.source_resource_refresh_dispatch_batch_size,
            lease_seconds=settings.source_resource_refresh_lease_seconds,
        )
        # Persist ownership before enqueueing.  If Redis is temporarily down,
        # the bounded lease expires and a later dispatch can recover safely.
        await db.commit()

    for lease in leases:
        await ctx["redis"].enqueue_job(
            "refresh_source_resource",
            resource_id=str(lease.resource_id),
            tenant_id=lease.tenant_id,
            lease_token=str(lease.token),
            _job_id=refresh_lease_job_id(lease),
        )
    logger.info("source resource refresh dispatch claimed=%d", len(leases))
    return len(leases)


async def refresh_source_resource(
    _ctx: dict,
    resource_id: str,
    tenant_id: str,
    lease_token: str,
) -> None:
    """Reserved per-resource entrypoint; no network fetch occurs in this slice."""

    # Parse identifiers early so a malformed queued payload cannot be treated as
    # a future provider request.  The next child task owns HTTP policy.
    uuid.UUID(resource_id)
    uuid.UUID(lease_token)
    logger.info(
        "source resource refresh provider is not installed resource_id=%s tenant_id=%s",
        resource_id,
        tenant_id,
    )
