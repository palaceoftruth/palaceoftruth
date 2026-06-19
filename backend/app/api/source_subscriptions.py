"""Source subscription management endpoints."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_api_key
from app.config import settings
from app.database import get_db
from app.models.source_subscription import SourceSubscription, SourceSubscriptionEntry
from app.schemas.source_subscription import (
    SourceSubscriptionCreate,
    SourceSubscriptionEntryRetryResponse,
    SourceSubscriptionEntryListResponse,
    SourceSubscriptionEntryOut,
    SourceSubscriptionListResponse,
    SourceSubscriptionOut,
    SourceSubscriptionPreview,
    SourceSubscriptionSyncResponse,
    SourceSubscriptionUpdate,
)
from app.services.source_subscriptions import (
    DEFAULT_SOURCE_SUBSCRIPTION_PROVIDER_REGISTRY,
    SourceSubscriptionProviderError,
    SourceSubscriptionBackfillPolicy,
    YOUTUBE_CHANNEL_PROVIDER_TYPE,
    create_source_subscription,
    enforce_source_subscription_manual_sync_cooldown,
    queue_source_subscription_entry,
    record_source_subscription_manual_sync,
    sanitize_source_subscription_error,
)

router = APIRouter(
    prefix="/source-subscriptions",
    tags=["source-subscriptions"],
    dependencies=[Depends(verify_api_key)],
)


async def _get_subscription_or_404(
    db: AsyncSession,
    *,
    subscription_id: uuid.UUID,
    tenant_id: str,
    include_deleted: bool = False,
) -> SourceSubscription:
    subscription = await db.get(SourceSubscription, subscription_id)
    if (
        subscription is None
        or subscription.tenant_id != tenant_id
        or (subscription.deleted_at is not None and not include_deleted)
    ):
        raise HTTPException(status_code=404, detail="Source subscription not found")
    return subscription


def _validate_youtube_channel_provider(provider_type: str) -> None:
    if provider_type != YOUTUBE_CHANNEL_PROVIDER_TYPE:
        raise HTTPException(status_code=422, detail="Only youtube_channel subscriptions are supported in v1")


@router.get("", response_model=SourceSubscriptionListResponse)
async def list_source_subscriptions(
    request: Request,
    include_deleted: bool = Query(False),
    db: AsyncSession = Depends(get_db),
) -> SourceSubscriptionListResponse:
    query = select(SourceSubscription).where(SourceSubscription.tenant_id == request.state.tenant_id)
    if not include_deleted:
        query = query.where(SourceSubscription.deleted_at.is_(None))
    rows = (await db.execute(query.order_by(SourceSubscription.created_at.desc()))).scalars().all()
    subscriptions = [SourceSubscriptionOut.model_validate(row) for row in rows]
    return SourceSubscriptionListResponse(subscriptions=subscriptions, total=len(subscriptions))


@router.post("/preview", response_model=SourceSubscriptionPreview)
async def preview_source_subscription(
    body: SourceSubscriptionCreate,
    request: Request,
) -> SourceSubscriptionPreview:
    _validate_youtube_channel_provider(body.provider_type)
    provider = DEFAULT_SOURCE_SUBSCRIPTION_PROVIDER_REGISTRY.create(body.provider_type)
    try:
        resolved = await provider.resolve_source(
            body.source_url,
            tenant_id=request.state.tenant_id,
            backfill_policy=SourceSubscriptionBackfillPolicy(
                enabled=body.backfill_enabled,
                limit=body.backfill_limit,
                published_after=body.backfill_published_after,
            ),
        )
    except SourceSubscriptionProviderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return SourceSubscriptionPreview(
        provider_type=resolved.provider_type,
        source_url=resolved.source_url,
        external_id=resolved.external_id,
        external_url=resolved.external_url,
        display_name=body.display_name or resolved.display_name,
        provider_metadata=resolved.metadata,
        no_backfill=bool(resolved.cursor.get("no_backfill", True)),
        backfill_enabled=body.backfill_enabled,
        backfill_limit=body.backfill_limit,
        backfill_published_after=body.backfill_published_after,
    )


@router.post("", response_model=SourceSubscriptionOut, status_code=status.HTTP_201_CREATED)
async def post_source_subscription(
    body: SourceSubscriptionCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> SourceSubscriptionOut:
    _validate_youtube_channel_provider(body.provider_type)
    try:
        subscription = await create_source_subscription(
            db,
            tenant_id=request.state.tenant_id,
            provider_type=body.provider_type,
            source_url=body.source_url,
            display_name=body.display_name,
            auto_tags=body.auto_tags,
            poll_interval_seconds=body.poll_interval_seconds,
            backfill_enabled=body.backfill_enabled,
            backfill_limit=body.backfill_limit,
            backfill_published_after=body.backfill_published_after,
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Source subscription already exists") from exc
    except SourceSubscriptionProviderError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return SourceSubscriptionOut.model_validate(subscription)


@router.get("/entries", response_model=SourceSubscriptionEntryListResponse)
async def list_recent_source_subscription_entries(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> SourceSubscriptionEntryListResponse:
    rows = (
        await db.execute(
            select(SourceSubscriptionEntry)
            .where(SourceSubscriptionEntry.tenant_id == request.state.tenant_id)
            .order_by(SourceSubscriptionEntry.discovered_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    entries = [SourceSubscriptionEntryOut.model_validate(row) for row in rows]
    return SourceSubscriptionEntryListResponse(entries=entries, total=len(entries))


@router.post("/entries/{entry_id}/retry", response_model=SourceSubscriptionEntryRetryResponse, status_code=status.HTTP_202_ACCEPTED)
async def retry_source_subscription_entry(
    entry_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> SourceSubscriptionEntryRetryResponse:
    entry = await db.get(SourceSubscriptionEntry, entry_id)
    if entry is None or entry.tenant_id != request.state.tenant_id:
        raise HTTPException(status_code=404, detail="Source subscription entry not found")
    if entry.status != "failed":
        raise HTTPException(status_code=409, detail=f"Entry is {entry.status}; only failed entries can be retried")

    subscription = await _get_subscription_or_404(
        db,
        subscription_id=entry.subscription_id,
        tenant_id=request.state.tenant_id,
    )
    if subscription.status != "active":
        raise HTTPException(status_code=409, detail="Only active source subscriptions can retry entries")

    queued = await queue_source_subscription_entry(db, request.app.state.arq_pool, subscription, entry)
    if not queued:
        await db.commit()
        raise HTTPException(status_code=409, detail="Entry retry did not queue a new ingest job")
    return SourceSubscriptionEntryRetryResponse(
        status="queued",
        subscription_id=subscription.id,
        entry_id=entry.id,
    )


@router.get("/{subscription_id}", response_model=SourceSubscriptionOut)
async def get_source_subscription(
    subscription_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> SourceSubscriptionOut:
    subscription = await _get_subscription_or_404(
        db,
        subscription_id=subscription_id,
        tenant_id=request.state.tenant_id,
    )
    return SourceSubscriptionOut.model_validate(subscription)


@router.patch("/{subscription_id}", response_model=SourceSubscriptionOut)
async def patch_source_subscription(
    subscription_id: uuid.UUID,
    body: SourceSubscriptionUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> SourceSubscriptionOut:
    subscription = await _get_subscription_or_404(
        db,
        subscription_id=subscription_id,
        tenant_id=request.state.tenant_id,
    )
    if body.display_name is not None:
        subscription.display_name = body.display_name
    if body.auto_tags is not None:
        subscription.auto_tags = body.auto_tags
    if body.poll_interval_seconds is not None:
        subscription.poll_interval_seconds = max(
            body.poll_interval_seconds,
            settings.source_subscription_poll_min_interval,
        )
    if body.paused_reason is not None and subscription.status == "paused":
        subscription.paused_reason = body.paused_reason
    await db.commit()
    return SourceSubscriptionOut.model_validate(subscription)


@router.post("/{subscription_id}/pause", response_model=SourceSubscriptionOut)
async def pause_source_subscription(
    subscription_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> SourceSubscriptionOut:
    subscription = await _get_subscription_or_404(
        db,
        subscription_id=subscription_id,
        tenant_id=request.state.tenant_id,
    )
    subscription.status = "paused"
    subscription.paused_reason = "manual_pause"
    await db.commit()
    return SourceSubscriptionOut.model_validate(subscription)


@router.post("/{subscription_id}/resume", response_model=SourceSubscriptionOut)
async def resume_source_subscription(
    subscription_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> SourceSubscriptionOut:
    subscription = await _get_subscription_or_404(
        db,
        subscription_id=subscription_id,
        tenant_id=request.state.tenant_id,
    )
    subscription.status = "active"
    subscription.paused_reason = None
    subscription.consecutive_failures = 0
    await db.commit()
    return SourceSubscriptionOut.model_validate(subscription)


@router.delete("/{subscription_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source_subscription(
    subscription_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> None:
    subscription = await _get_subscription_or_404(
        db,
        subscription_id=subscription_id,
        tenant_id=request.state.tenant_id,
    )
    subscription.status = "deleted"
    subscription.deleted_at = datetime.now(timezone.utc)
    subscription.paused_reason = "soft_deleted"
    await db.commit()


@router.post("/{subscription_id}/sync", response_model=SourceSubscriptionSyncResponse, status_code=status.HTTP_202_ACCEPTED)
async def manual_sync_source_subscription(
    subscription_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> SourceSubscriptionSyncResponse:
    subscription = await _get_subscription_or_404(
        db,
        subscription_id=subscription_id,
        tenant_id=request.state.tenant_id,
    )
    if subscription.status != "active":
        raise HTTPException(status_code=409, detail="Only active source subscriptions can be synced")
    remaining_cooldown_seconds = enforce_source_subscription_manual_sync_cooldown(subscription)
    if remaining_cooldown_seconds is not None:
        raise HTTPException(
            status_code=429,
            detail=f"Manual sync rate limited; retry after {remaining_cooldown_seconds} seconds",
        )
    try:
        await request.app.state.arq_pool.enqueue_job(
            "poll_source_subscription_task",
            subscription_id=str(subscription.id),
            tenant_id=request.state.tenant_id,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Could not enqueue source subscription sync: {sanitize_source_subscription_error(exc)}",
        ) from exc
    record_source_subscription_manual_sync(subscription)
    await db.commit()
    return SourceSubscriptionSyncResponse(status="queued", subscription_id=subscription.id)


@router.get("/{subscription_id}/entries", response_model=SourceSubscriptionEntryListResponse)
async def list_source_subscription_entries(
    subscription_id: uuid.UUID,
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> SourceSubscriptionEntryListResponse:
    await _get_subscription_or_404(
        db,
        subscription_id=subscription_id,
        tenant_id=request.state.tenant_id,
    )
    rows = (
        await db.execute(
            select(SourceSubscriptionEntry)
            .where(SourceSubscriptionEntry.tenant_id == request.state.tenant_id)
            .where(SourceSubscriptionEntry.subscription_id == subscription_id)
            .order_by(SourceSubscriptionEntry.discovered_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    entries = [SourceSubscriptionEntryOut.model_validate(row) for row in rows]
    return SourceSubscriptionEntryListResponse(entries=entries, total=len(entries))
