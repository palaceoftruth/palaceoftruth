"""Bounded, no-network dispatch primitives for watched HTTP resources.

The HTTP provider, robots policy, and content activation deliberately land in
later slices.  This module only establishes durable lease ownership so those
slices cannot fan out duplicate work after concurrent dispatches or restarts.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

import trafilatura
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session
from app.models.item import Item
from app.models.palace import SourceRecord
from app.models.source_resource import SourceResource
from app.services.chunker import chunk_text
from app.services.source_compiler import backfill_source_records_and_chunks
from app.services.source_resource_fetch import fetch_http_resource
from app.services.source_resource_fairness import HostFairness
from app.services.source_resource_robots import RobotsDecision, evaluate_robots
from app.services.source_resources import (
    RefreshLease,
    RefreshObservation,
    claim_refresh_lease,
    decide_alias,
    persist_refresh_observation,
    refresh_lease_job_id,
)
from app.utils.hash import compute_content_hash

logger = logging.getLogger(__name__)

_host_fairness = HostFairness()
_ROBOTS_CACHE_TTL = timedelta(hours=1)
_MAX_REDIRECTS = 5


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
    """Refresh one leased HTTP resource without replacing its last-good version."""

    parsed_resource_id = uuid.UUID(resource_id)
    parsed_lease_token = uuid.UUID(lease_token)
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        resource = await db.scalar(
            select(SourceResource)
            .where(SourceResource.id == parsed_resource_id)
            .where(SourceResource.tenant_id == tenant_id)
            .where(SourceResource.refresh_lease_token == parsed_lease_token)
            .where(SourceResource.refresh_lease_expires_at > now)
            .with_for_update()
        )
        if resource is None:
            logger.info("source resource refresh ignored stale lease resource_id=%s", resource_id)
            return
        url = resource.canonical_url
        etag = resource.validator_etag
        last_modified = resource.validator_last_modified

    result, robots = await _fetch_with_robots(
        resource=resource,
        url=url,
        etag=etag,
        last_modified=last_modified,
        now=now,
    )

    async with async_session() as db:
        # Do not reuse the pre-fetch timestamp here: a slow robots or document
        # request may outlive the durable lease, in which case another worker
        # is entitled to retry the resource and this result must be discarded.
        result_now = datetime.now(timezone.utc)
        resource = await db.scalar(
            select(SourceResource)
            .where(SourceResource.id == parsed_resource_id)
            .where(SourceResource.tenant_id == tenant_id)
            .where(SourceResource.refresh_lease_token == parsed_lease_token)
            .where(SourceResource.refresh_lease_expires_at > result_now)
            .with_for_update()
        )
        if resource is None:
            logger.info("source resource result ignored after lease expiry resource_id=%s", resource_id)
            return

        if result is None:
            observation = RefreshObservation(
                outcome="failure",
                failure_reason=robots.decision,
                robots_allowed=False,
                robots_decision=robots.decision,
                robots_cached_at=now,
            )
        elif result.outcome == "success":
            assert result.body is not None
            digest = compute_content_hash(result.body.decode("utf-8", errors="replace"))
            if digest == resource.content_digest:
                observation = RefreshObservation(
                    outcome="not_modified",
                    http_status=result.status_code,
                    content_digest=digest,
                    validator_etag=result.etag,
                    validator_last_modified=result.last_modified,
                    robots_allowed=True,
                    robots_decision=robots.decision,
                    robots_cached_at=now,
                )
            else:
                source_record_id = await _activate_resource_content(
                    db,
                    resource=resource,
                    content=result.body.decode("utf-8", errors="replace"),
                    final_url=result.final_url or resource.canonical_url,
                )
                observation = RefreshObservation(
                    outcome="success",
                    http_status=result.status_code,
                    source_record_id=source_record_id,
                    content_digest=digest,
                    validator_etag=result.etag,
                    validator_last_modified=result.last_modified,
                    captured_at=datetime.now(timezone.utc),
                    robots_allowed=True,
                    robots_decision=robots.decision,
                    robots_cached_at=now,
                )
        elif result.outcome == "not_found":
            # A single 404 is often eventual consistency or a temporary edge
            # response.  Only a second consecutive 404 tombstones the source.
            observation = RefreshObservation(
                outcome="gone" if resource.last_failure_reason == "http_404" else "failure",
                http_status=result.status_code,
                validator_etag=result.etag,
                validator_last_modified=result.last_modified,
                failure_reason=result.failure_reason,
                retry_after_seconds=result.retry_after_seconds,
                robots_allowed=True,
                robots_decision=robots.decision,
                robots_cached_at=now,
            )
        else:
            observation = RefreshObservation(
                outcome=result.outcome,  # type: ignore[arg-type]
                http_status=result.status_code,
                validator_etag=result.etag,
                validator_last_modified=result.last_modified,
                failure_reason=result.failure_reason,
                retry_after_seconds=result.retry_after_seconds,
                robots_allowed=True,
                robots_decision=robots.decision,
                robots_cached_at=now,
            )

        await persist_refresh_observation(
            db,
            resource=resource,
            tenant_id=tenant_id,
            observation=observation,
        )
        resource.refresh_lease_token = None
        resource.refresh_lease_expires_at = None
        await db.commit()


async def _activate_resource_content(
    db: AsyncSession,
    *,
    resource: SourceResource,
    content: str,
    final_url: str,
) -> uuid.UUID:
    """Update the stable resource Item and append its SourceRecord version."""

    readable_content = trafilatura.extract(content, include_comments=False, include_tables=True, output_format="txt") or content
    current_record = None
    if resource.last_successful_source_record_id is not None:
        current_record = await db.get(SourceRecord, resource.last_successful_source_record_id)
    item = await db.get(Item, current_record.item_id) if current_record is not None else None
    if item is None:
        item = Item(
            tenant_id=resource.tenant_id,
            source_type="webpage",
            source_url=resource.canonical_url,
            title=final_url,
            status="ready",
        )
        db.add(item)
        await db.flush()

    item.raw_content = readable_content
    item.content_hash = compute_content_hash(readable_content)
    item.content_chunks = chunk_text(readable_content)
    item.metadata_ = {**(item.metadata_ or {}), "source_resource_id": str(resource.id), "final_url": final_url}
    item.status = "ready"
    await db.flush()
    await backfill_source_records_and_chunks(
        db,
        tenant_id=resource.tenant_id,
        item_ids=[item.id],
        limit=1,
        dry_run=False,
        commit=False,
    )
    record = await db.scalar(
        select(SourceRecord)
        .where(SourceRecord.tenant_id == resource.tenant_id)
        .where(SourceRecord.item_id == item.id)
        .order_by(SourceRecord.updated_at.desc(), SourceRecord.created_at.desc())
        .limit(1)
    )
    if record is None:
        raise RuntimeError("source record activation did not create a version")
    return record.id


async def _fetch_with_robots(
    *,
    resource: SourceResource,
    url: str,
    etag: str | None,
    last_modified: str | None,
    now: datetime,
):
    """Fetch only same-origin, robots-allowed redirect targets."""

    current_url = url
    cached_robots = (
        resource.robots_allowed is not None
        and resource.robots_cached_at is not None
        and resource.robots_cached_at >= now - _ROBOTS_CACHE_TTL
    )
    for hop in range(_MAX_REDIRECTS + 1):
        robots = (
            RobotsDecision(bool(resource.robots_allowed), resource.robots_decision or "robots_cached")
            if cached_robots and hop == 0
            else await evaluate_robots(current_url)
        )
        if not robots.allowed:
            return None, robots
        async with _host_fairness.acquire(current_url):
            result = await fetch_http_resource(current_url, etag=etag, last_modified=last_modified)
        if result.outcome != "redirect":
            return result, robots
        if result.redirect_url is None:
            return result, robots
        alias = decide_alias(canonical_url=resource.canonical_url, observed_url=result.redirect_url, signal="final")
        if alias.decision != "accepted":
            return (
                type(result)("failure", result.status_code, final_url=result.redirect_url, failure_reason=alias.reason),
                robots,
            )
        current_url = result.redirect_url
    return type(result)("failure", result.status_code, final_url=current_url, failure_reason="too_many_redirects"), robots
