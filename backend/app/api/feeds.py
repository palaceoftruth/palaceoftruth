"""Feed CRUD + action endpoints."""
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_api_key
from app.config import settings
from app.database import get_db
from app.schemas.feed import FeedCreate, FeedUpdate, FeedOut, FeedListResponse, OPMLImportResponse

router = APIRouter(
    prefix="/feeds",
    tags=["feeds"],
    dependencies=[Depends(verify_api_key)],
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FEED_WITH_COUNT_SQL = """
    SELECT f.*,
           (SELECT COUNT(*) FROM items
            WHERE metadata->>'feed_id' = f.id::text
            AND tenant_id = :tenant_id
            AND status = 'ready'
            AND deleted_at IS NULL) AS item_count
    FROM feeds f
    WHERE f.tenant_id = :tenant_id
      AND f.deleted_at IS NULL
"""


def _row_to_feed_out(row: dict) -> FeedOut:
    return FeedOut.model_validate(dict(row))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("", response_model=FeedListResponse)
async def list_feeds(request: Request, db: AsyncSession = Depends(get_db)):
    tenant_id = request.state.tenant_id
    result = await db.execute(
        text(_FEED_WITH_COUNT_SQL + " ORDER BY f.created_at DESC"),
        {"tenant_id": tenant_id},
    )
    rows = result.mappings().all()
    feeds = [_row_to_feed_out(row) for row in rows]
    return {"feeds": feeds, "total": len(feeds)}


@router.post("", response_model=FeedOut, status_code=201)
async def create_feed(body: FeedCreate, request: Request, db: AsyncSession = Depends(get_db)):
    tenant_id = request.state.tenant_id
    poll_interval = max(body.poll_interval, settings.feed_poll_min_interval)

    try:
        result = await db.execute(
            text(
                "INSERT INTO feeds (url, name, auto_tags, poll_interval, tenant_id) "
                "VALUES (:url, :name, :auto_tags, :poll_interval, :tenant_id) "
                "RETURNING *"
            ),
            {
                "url": body.url,
                "name": body.name,
                "auto_tags": body.auto_tags,
                "poll_interval": poll_interval,
                "tenant_id": tenant_id,
            },
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Feed with this URL already exists")
    row = result.mappings().one()
    feed_id = str(row["id"])

    # Enrich with item_count (will be 0 for new feed)
    feed_result = await db.execute(
        text(_FEED_WITH_COUNT_SQL + " AND f.id = :id"),
        {"tenant_id": tenant_id, "id": row["id"]},
    )
    feed_row = feed_result.mappings().one()

    # Trigger immediate first poll
    await request.app.state.arq_pool.enqueue_job("poll_feed", feed_id=feed_id, tenant_id=tenant_id)

    return _row_to_feed_out(feed_row)


@router.get("/{feed_id}", response_model=FeedOut)
async def get_feed(feed_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    tenant_id = request.state.tenant_id
    result = await db.execute(
        text(_FEED_WITH_COUNT_SQL + " AND f.id = :id"),
        {"tenant_id": tenant_id, "id": feed_id},
    )
    row = result.mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Feed not found")
    return _row_to_feed_out(row)


@router.patch("/{feed_id}", response_model=FeedOut)
async def update_feed(feed_id: uuid.UUID, body: FeedUpdate, request: Request, db: AsyncSession = Depends(get_db)):
    tenant_id = request.state.tenant_id
    # Build dynamic SET clause from provided fields
    updates: dict[str, Any] = {"id": feed_id, "tenant_id": tenant_id}
    set_parts: list[str] = ["updated_at = now()"]

    if body.name is not None:
        updates["name"] = body.name
        set_parts.append("name = :name")
    if body.auto_tags is not None:
        updates["auto_tags"] = body.auto_tags
        set_parts.append("auto_tags = :auto_tags")
    if body.poll_interval is not None:
        updates["poll_interval"] = max(body.poll_interval, settings.feed_poll_min_interval)
        set_parts.append("poll_interval = :poll_interval")
    if body.enabled is not None:
        updates["enabled"] = body.enabled
        set_parts.append("enabled = :enabled")

    if len(set_parts) == 1:
        # Only updated_at — still do the update to bump timestamp
        pass

    result = await db.execute(
        text(
            f"UPDATE feeds SET {', '.join(set_parts)} "
            "WHERE id = :id AND tenant_id = :tenant_id AND deleted_at IS NULL RETURNING id"
        ),
        updates,
    )
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Feed not found")

    return await get_feed(feed_id, request, db)


@router.delete("/{feed_id}", status_code=204)
async def delete_feed(feed_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    tenant_id = request.state.tenant_id
    result = await db.execute(
        text(
            """
            UPDATE feeds
            SET deleted_at = :deleted_at,
                enabled = false,
                paused_reason = 'soft_deleted',
                updated_at = now()
            WHERE id = :id
              AND tenant_id = :tenant_id
              AND deleted_at IS NULL
            RETURNING id
            """
        ),
        {"id": feed_id, "tenant_id": tenant_id, "deleted_at": datetime.now(timezone.utc)},
    )
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Feed not found")


@router.post("/{feed_id}/restore", response_model=FeedOut)
async def restore_feed(feed_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    tenant_id = request.state.tenant_id
    result = await db.execute(
        text(
            """
            UPDATE feeds
            SET deleted_at = NULL,
                enabled = true,
                paused_reason = NULL,
                updated_at = now()
            WHERE id = :id
              AND tenant_id = :tenant_id
            RETURNING *
            """
        ),
        {"id": feed_id, "tenant_id": tenant_id},
    )
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Feed not found")
    return await get_feed(feed_id, request, db)


@router.post("/{feed_id}/poll", status_code=202)
async def force_poll(feed_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    tenant_id = request.state.tenant_id
    # Verify feed exists and belongs to this tenant
    result = await db.execute(
        text("SELECT id FROM feeds WHERE id = :id AND tenant_id = :tenant_id AND deleted_at IS NULL"),
        {"id": feed_id, "tenant_id": tenant_id},
    )
    if result.mappings().one_or_none() is None:
        raise HTTPException(status_code=404, detail="Feed not found")

    await request.app.state.arq_pool.enqueue_job("poll_feed", feed_id=str(feed_id), tenant_id=tenant_id)
    return {"status": "queued", "feed_id": str(feed_id)}


@router.post("/{feed_id}/enable", response_model=FeedOut)
async def enable_feed(feed_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    tenant_id = request.state.tenant_id
    result = await db.execute(
        text(
            "UPDATE feeds SET enabled = true, paused_reason = NULL, "
            "consecutive_failures = 0, last_error = NULL, updated_at = now() "
            "WHERE id = :id AND tenant_id = :tenant_id AND deleted_at IS NULL RETURNING id"
        ),
        {"id": feed_id, "tenant_id": tenant_id},
    )
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Feed not found")
    return await get_feed(feed_id, request, db)


@router.post("/{feed_id}/disable", response_model=FeedOut)
async def disable_feed(feed_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    tenant_id = request.state.tenant_id
    result = await db.execute(
        text(
            "UPDATE feeds SET enabled = false, paused_reason = 'manual_disable', "
            "updated_at = now() WHERE id = :id AND tenant_id = :tenant_id AND deleted_at IS NULL RETURNING id"
        ),
        {"id": feed_id, "tenant_id": tenant_id},
    )
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Feed not found")
    return await get_feed(feed_id, request, db)


@router.get("/{feed_id}/items")
async def list_feed_items(
    feed_id: uuid.UUID,
    request: Request,
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    tenant_id = request.state.tenant_id
    # Verify feed exists and belongs to this tenant
    feed_check = await db.execute(
        text("SELECT id FROM feeds WHERE id = :id AND tenant_id = :tenant_id AND deleted_at IS NULL"),
        {"id": feed_id, "tenant_id": tenant_id},
    )
    if feed_check.mappings().one_or_none() is None:
        raise HTTPException(status_code=404, detail="Feed not found")

    result = await db.execute(
        text(
            "SELECT * FROM items "
            "WHERE metadata->>'feed_id' = :feed_id AND tenant_id = :tenant_id AND status = 'ready' AND deleted_at IS NULL "
            "ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
        ),
        {"feed_id": str(feed_id), "tenant_id": tenant_id, "limit": limit, "offset": offset},
    )
    rows = result.mappings().all()

    count_result = await db.execute(
        text(
            "SELECT COUNT(*) FROM items "
            "WHERE metadata->>'feed_id' = :feed_id AND tenant_id = :tenant_id AND status = 'ready' AND deleted_at IS NULL"
        ),
        {"feed_id": str(feed_id), "tenant_id": tenant_id},
    )
    total = count_result.scalar_one()

    return {"total": total, "items": [dict(row) for row in rows]}


@router.post("/import_opml", response_model=OPMLImportResponse, status_code=202)
async def import_opml(
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    content = await file.read()
    try:
        tree = ET.fromstring(content)
    except ET.ParseError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid OPML: {exc}")

    outlines = tree.findall(".//outline[@type='rss']")
    urls = [o.get("xmlUrl") for o in outlines if o.get("xmlUrl")]

    created = 0
    skipped = 0
    feed_ids: list[uuid.UUID] = []

    tenant_id = request.state.tenant_id

    for url in urls:
        result = await db.execute(
            text(
                "INSERT INTO feeds (url, poll_interval, tenant_id) VALUES (:url, :poll_interval, :tenant_id) "
                "ON CONFLICT (url, tenant_id) DO NOTHING RETURNING id"
            ),
            {"url": url, "poll_interval": settings.feed_poll_min_interval, "tenant_id": tenant_id},
        )
        row = result.mappings().one_or_none()
        if row is not None:
            created += 1
            feed_ids.append(row["id"])
        else:
            skipped += 1

    await db.commit()

    # Trigger immediate poll for newly created feeds
    for fid in feed_ids:
        await request.app.state.arq_pool.enqueue_job(
            "poll_feed",
            feed_id=str(fid),
            tenant_id=tenant_id,
        )

    # Fetch all feed rows with item_count for response
    if feed_ids:
        placeholders = ", ".join(f":id_{i}" for i in range(len(feed_ids)))
        params: dict = {f"id_{i}": fid for i, fid in enumerate(feed_ids)}
        params["tenant_id"] = tenant_id
        feeds_result = await db.execute(
            text(
                _FEED_WITH_COUNT_SQL
                + f" AND f.id IN ({placeholders}) ORDER BY f.created_at DESC"
            ),
            params,
        )
        feed_rows = feeds_result.mappings().all()
        feeds_out = [_row_to_feed_out(row) for row in feed_rows]
    else:
        feeds_out = []

    return OPMLImportResponse(created=created, skipped=skipped, feeds=feeds_out)
