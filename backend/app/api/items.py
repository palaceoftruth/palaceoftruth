import base64
import binascii
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, HTTPException, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy import ARRAY, Text, bindparam, delete, select, func, text as sa_text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_auth_context, require_api_capability
from app.config import settings
from app.database import get_db
from app.models.embedding import Embedding
from app.models.item import Item
from app.schemas.item import ItemResponse, ItemListResponse, ItemUpdate, ItemCreate, ItemCreateResponse, BatchActionRequest, BatchActionResponse, ItemDeleteResponse, ItemRestoreResponse
from app.schemas.relationship import RelatedItemResponse, RelatedItemsResponse
from app.services.item_dates import apply_effective_date
from app.services.data_lifecycle import hard_delete_item
from app.utils.file_type import SNIFF_BYTES, matches_extension, safe_media_type
from app.workers.queues import enqueue_palace_job, enqueue_tenant_job

router = APIRouter(prefix="/items", tags=["items"])

_SORT_FIELDS = {
    "created_at": Item.created_at,
    "updated_at": Item.updated_at,
    # Use a normalized title sort so A/a do not paginate inconsistently.
    "title": func.lower(Item.title),
}
_SORT_ORDERS = frozenset({"asc", "desc"})


def _is_deleted(row: Item) -> bool:
    return row.deleted_at is not None or row.status == "deleted"


def _encode_items_cursor(row: Item) -> str:
    payload = {
        "created_at": row.created_at.isoformat(),
        "id": str(row.id),
    }
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")


def _decode_items_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        payload: Any = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("cursor payload must be an object")
        created_at = payload.get("created_at")
        item_id = payload.get("id")
        if not isinstance(created_at, str) or not isinstance(item_id, str):
            raise ValueError("cursor payload is missing created_at or id")
        return datetime.fromisoformat(created_at.replace("Z", "+00:00")), uuid.UUID(item_id)
    except (binascii.Error, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail="cursor must be a valid item listing cursor") from exc


def _tombstone_item(row: Item, *, actor_id: str | None, deleted_via: str) -> datetime:
    # M-10: `actor_id` must be a stable, non-secret identifier (an api_keys.id
    # or MCP client/token id — see AuthContext.subject_id), never a credential
    # verifier hash. This metadata blob is tenant-readable through the API, so
    # anything written here is effectively published back to the caller.
    deleted_at = datetime.now(timezone.utc)
    metadata = dict(row.metadata_ or {})
    metadata["deleted_at"] = deleted_at.isoformat()
    metadata["deleted_by"] = actor_id
    metadata["deleted_via"] = deleted_via
    metadata["previous_status"] = row.status
    row.metadata_ = metadata
    row.deleted_at = deleted_at
    row.status = "deleted"
    row.updated_at = deleted_at
    return deleted_at


def _restore_item(row: Item) -> None:
    metadata = dict(row.metadata_ or {})
    previous_status = metadata.pop("previous_status", None)
    metadata.pop("deleted_at", None)
    metadata.pop("deleted_by", None)
    metadata.pop("deleted_via", None)
    row.metadata_ = metadata
    row.deleted_at = None
    row.status = previous_status if previous_status in {"ready", "processing", "failed"} else "ready"
    row.updated_at = datetime.now(timezone.utc)


def _image_artifact_storage_path(row: Item) -> Path | None:
    metadata = row.metadata_ or {}
    allowed_root = Path(settings.upload_artifact_dir).expanduser().resolve()
    for owner_key in ("image_analysis", "browser_capture_image"):
        artifact_owner = metadata.get(owner_key)
        if not isinstance(artifact_owner, dict):
            continue
        artifact = artifact_owner.get("artifact")
        if not isinstance(artifact, dict):
            continue
        storage_path = artifact.get("storage_path")
        if not isinstance(storage_path, str) or not storage_path.strip():
            continue
        resolved = Path(storage_path).expanduser().resolve()
        try:
            resolved.relative_to(allowed_root)
        except ValueError:
            continue
        return resolved
    return None


async def _schedule_palace_dirty(request: Request, item_ids: list[uuid.UUID], reason: str) -> None:
    if not item_ids:
        return
    await enqueue_palace_job(
        request.app.state.arq_pool,
        "mark_items_dirty_and_schedule",
        item_ids=[str(item_id) for item_id in item_ids],
        tenant_id=request.state.tenant_id,
        reason=reason,
    )


@router.get("", response_model=ItemListResponse, dependencies=[Depends(require_api_capability("read"))])
async def list_items(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    source_type: str | None = Query(None),
    tags: str | None = Query(None, description="Comma-separated tags (any-match OR)"),
    sort: str = Query("created_at"),
    order: str = Query("desc"),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    source_url: str | None = Query(None, description="Exact source_url match for audit verification"),
    cursor: str | None = Query(None, description="Stable cursor returned by a previous created_at-sorted listing"),
    db: AsyncSession = Depends(get_db),
):
    sort_key = sort.strip().lower()
    order_key = order.strip().lower()
    sort_column = _SORT_FIELDS.get(sort_key)
    if sort_column is None:
        raise HTTPException(status_code=422, detail=f"Unsupported sort field: {sort}")
    if order_key not in _SORT_ORDERS:
        raise HTTPException(status_code=422, detail=f"Unsupported sort order: {order}")
    if cursor and (sort_key != "created_at" or page != 1):
        raise HTTPException(status_code=422, detail="cursor pagination requires sort=created_at and page=1")

    # Hide failed and soft-deleted items from shared library browse flows.
    q = select(Item).where(
        Item.tenant_id == request.state.tenant_id,
        Item.status != "failed",
        Item.status != "deleted",
        Item.deleted_at.is_(None),
    )
    if source_type:
        q = q.where(Item.source_type == source_type)
    if source_url is not None:
        q = q.where(Item.source_url == source_url)
    if tags:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        if tag_list:
            q = q.where(
                sa_text("tags && :tags").bindparams(
                    bindparam("tags", value=tag_list, type_=ARRAY(Text()))
                )
            )
    if date_from:
        q = q.where(Item.created_at >= date_from)
    if date_to:
        q = q.where(Item.created_at <= date_to)

    count_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(count_q)).scalar_one()

    if cursor:
        cursor_created_at, cursor_id = _decode_items_cursor(cursor)
        if order_key == "asc":
            q = q.where(
                (Item.created_at > cursor_created_at)
                | ((Item.created_at == cursor_created_at) & (Item.id > cursor_id))
            )
        else:
            q = q.where(
                (Item.created_at < cursor_created_at)
                | ((Item.created_at == cursor_created_at) & (Item.id < cursor_id))
            )

    primary_order = sort_column.asc() if order_key == "asc" else sort_column.desc()
    tie_breaker = Item.id.asc() if order_key == "asc" else Item.id.desc()
    q = q.order_by(primary_order, tie_breaker)
    q = q.offset(0 if cursor else (page - 1) * per_page).limit(per_page + 1)
    rows = (await db.execute(q)).scalars().all()
    next_cursor = None
    if len(rows) > per_page:
        rows = rows[:per_page]
        next_cursor = _encode_items_cursor(rows[-1])

    return ItemListResponse(
        items=[ItemResponse.model_validate(r) for r in rows],
        total=total,
        page=page,
        per_page=per_page,
        next_cursor=next_cursor,
    )


@router.post("", response_model=ItemCreateResponse, status_code=201, dependencies=[Depends(require_api_capability("write"))])
async def create_item(
    body: ItemCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    item = Item(
        source_type=body.source_type,
        source_url=body.source_url,
        title=body.title,
        raw_content=body.raw_content,
        summary=body.summary,
        tags=body.tags,
        metadata_=body.metadata,
        effective_date=body.effective_date,
        effective_date_source=body.effective_date_source,
        effective_date_quality=body.effective_date_quality,
        status="processing" if body.raw_content else "ready",
        tenant_id=request.state.tenant_id,
    )
    if body.effective_date is None:
        apply_effective_date(item, metadata=body.metadata)
    db.add(item)
    await db.commit()
    await db.refresh(item)

    embedding_queued = False
    if body.raw_content:
        await enqueue_tenant_job(
            request.app.state.arq_pool,
            "embed_item",
            item_id=str(item.id),
            skip_ai_enrichment=body.skip_ai_enrichment,
            tenant_id=request.state.tenant_id,
        )
        embedding_queued = True

    return ItemCreateResponse(
        item_id=item.id,
        status=item.status,
        embedding_queued=embedding_queued,
    )


@router.post("/batch", response_model=BatchActionResponse, dependencies=[Depends(require_api_capability("write"))])
async def batch_items(
    body: BatchActionRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    tid = request.state.tenant_id

    if body.action == "delete":
        rows = (
            await db.execute(
                select(Item)
                .where(Item.tenant_id == tid)
                .where(Item.id.in_(body.ids))
                .where(Item.deleted_at.is_(None))
                .where(Item.status != "deleted")
            )
        ).scalars().all()
        for row in rows:
            _tombstone_item(row, actor_id=get_auth_context(request).subject_id, deleted_via="items.batch")
        await db.commit()
        await _schedule_palace_dirty(request, [row.id for row in rows], "item-soft-delete")
        return BatchActionResponse(affected=len(rows), action="delete")

    if not body.tags:
        raise HTTPException(status_code=422, detail="tags required for tag/untag action")
    if body.action == "tag":
        statement = sa_text("""
                UPDATE items
                SET tags = (
                    SELECT array_agg(DISTINCT t)
                    FROM unnest(tags || :new_tags) AS t
                )
                WHERE id = ANY(:ids) AND tenant_id = :tid
            """).bindparams(
            bindparam("new_tags", type_=ARRAY(Text())),
            bindparam("ids", type_=ARRAY(PG_UUID(as_uuid=True))),
        )
        result = await db.execute(statement, {"new_tags": body.tags, "ids": body.ids, "tid": tid})
    else:  # untag
        statement = sa_text("""
                UPDATE items
                SET tags = array(
                    SELECT unnest(tags)
                    EXCEPT SELECT unnest(:remove_tags)
                )
                WHERE id = ANY(:ids) AND tenant_id = :tid
            """).bindparams(
            bindparam("remove_tags", type_=ARRAY(Text())),
            bindparam("ids", type_=ARRAY(PG_UUID(as_uuid=True))),
        )
        result = await db.execute(statement, {"remove_tags": body.tags, "ids": body.ids, "tid": tid})

    await db.commit()
    return BatchActionResponse(affected=result.rowcount, action=body.action)


@router.get("/{item_id}", response_model=ItemResponse, dependencies=[Depends(require_api_capability("read"))])
async def get_item(
    item_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(Item, item_id)
    if not row or _is_deleted(row):
        raise HTTPException(status_code=404, detail="Item not found")
    if str(row.tenant_id) != request.state.tenant_id:
        raise HTTPException(status_code=404, detail="Item not found")
    return ItemResponse.model_validate(row)


@router.get("/{item_id}/artifact", dependencies=[Depends(require_api_capability("read"))])
async def get_item_artifact(
    item_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(Item, item_id)
    if not row or _is_deleted(row):
        raise HTTPException(status_code=404, detail="Item artifact not found")
    if str(row.tenant_id) != request.state.tenant_id:
        raise HTTPException(status_code=404, detail="Item artifact not found")

    storage_path = _image_artifact_storage_path(row)
    if storage_path is None or not storage_path.is_file():
        metadata = row.metadata_ if isinstance(row.metadata_, dict) else {}
        browser_image = metadata.get("browser_capture_image")
        if not isinstance(browser_image, dict):
            raise HTTPException(status_code=404, detail="Item artifact not found")
        image_url = browser_image.get("final_url") or browser_image.get("candidate_url")
        source_url = browser_image.get("source_post_url")
        if not isinstance(image_url, str) or not isinstance(source_url, str):
            raise HTTPException(status_code=404, detail="Item artifact not found")

        # Reuse the capture path's host relationship, DNS, redirect, media-type,
        # timeout, and size checks. The browser sees only this same-origin URL.
        from app.api.capture import download_browser_image_for_proxy

        downloaded = await download_browser_image_for_proxy(
            image_url=image_url,
            source_url=source_url,
        )
        expected_hash = browser_image.get("byte_hash")
        if isinstance(expected_hash, str) and expected_hash and downloaded.byte_hash != expected_hash:
            raise HTTPException(status_code=409, detail="Captured image bytes no longer match stored evidence")
        from app.services.bundle import persist_upload_artifact_bytes

        storage_path_value = persist_upload_artifact_bytes(
            downloaded.content,
            tenant_id=str(row.tenant_id),
            item_id=row.id,
            extension=downloaded.extension,
        )
        browser_image = dict(browser_image)
        browser_image["artifact"] = {
            "filename": f"{row.id}{downloaded.extension}",
            "media_type": downloaded.media_type,
            "storage_path": storage_path_value,
        }
        row.metadata_ = {**metadata, "browser_capture_image": browser_image}
        await db.commit()
        return Response(
            content=downloaded.content,
            media_type=downloaded.media_type,
            headers={
                "Cache-Control": "private, max-age=300",
                "X-Content-Type-Options": "nosniff",
            },
        )

    image_analysis = row.metadata_.get("image_analysis") if isinstance(row.metadata_, dict) else {}
    artifact = image_analysis.get("artifact") if isinstance(image_analysis, dict) else {}
    filename = artifact.get("filename") if isinstance(artifact, dict) else None

    # The artifact is served inline, so the media type decides what a browser
    # executes. Derive it from the stored bytes and an allowlist instead of the
    # recorded upload metadata, and forbid sniffing past that decision.
    media_type = _artifact_media_type(storage_path)
    return FileResponse(
        storage_path,
        media_type=media_type,
        filename=filename if isinstance(filename, str) else storage_path.name,
        content_disposition_type="inline",
        headers={"X-Content-Type-Options": "nosniff"},
    )


def _artifact_media_type(storage_path: Path) -> str:
    """Return an allowlisted media type that the file's own bytes support."""

    extension = storage_path.suffix.lower()
    try:
        with open(storage_path, "rb") as handle:
            head = handle.read(SNIFF_BYTES)
    except OSError:
        return "application/octet-stream"

    if not matches_extension(head, extension):
        # Stored bytes and stored name disagree: serve something inert.
        return "application/octet-stream"
    return safe_media_type(extension)


@router.patch("/{item_id}", response_model=ItemResponse, dependencies=[Depends(require_api_capability("write"))])
async def update_item(
    item_id: uuid.UUID,
    body: ItemUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(Item, item_id)
    if not row or _is_deleted(row):
        raise HTTPException(status_code=404, detail="Item not found")
    if str(row.tenant_id) != request.state.tenant_id:
        raise HTTPException(status_code=404, detail="Item not found")
    palace_relevant_change = False
    reindex_requested = False
    if body.title is not None:
        row.title = body.title
        palace_relevant_change = True
    if body.tags is not None:
        row.tags = body.tags
        palace_relevant_change = True
    if body.summary is not None:
        row.summary = body.summary
    if body.categories is not None:
        row.categories = body.categories
        palace_relevant_change = True
    if body.raw_content is not None:
        raw_content_changed = body.raw_content != row.raw_content
        row.raw_content = body.raw_content
        if raw_content_changed:
            await db.execute(
                delete(Embedding).where(
                    Embedding.item_id == row.id,
                    Embedding.tenant_id == request.state.tenant_id,
                )
            )
            row.content_chunks = None
            row.content_hash = None
            if body.summary is None:
                row.summary = None
            if body.raw_content:
                row.status = "processing"
                reindex_requested = True
            else:
                row.status = "ready"
                palace_relevant_change = True
    if body.metadata is not None:
        row.metadata_ = {**row.metadata_, **body.metadata}
        apply_effective_date(row)
    await db.commit()
    await db.refresh(row)
    if reindex_requested:
        await enqueue_tenant_job(
            request.app.state.arq_pool,
            "embed_item",
            item_id=str(row.id),
            skip_ai_enrichment=False,
            tenant_id=request.state.tenant_id,
        )
    elif palace_relevant_change:
        await enqueue_palace_job(
            request.app.state.arq_pool,
            "mark_item_dirty_and_schedule",
            item_id=str(row.id),
            tenant_id=request.state.tenant_id,
            reason="item-update",
        )
    return ItemResponse.model_validate(row)


@router.delete("/{item_id}", response_model=ItemDeleteResponse, dependencies=[Depends(require_api_capability("write"))])
async def delete_item(
    item_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(Item, item_id)
    if not row or _is_deleted(row):
        raise HTTPException(status_code=404, detail="Item not found")
    if str(row.tenant_id) != request.state.tenant_id:
        raise HTTPException(status_code=404, detail="Item not found")
    deleted_at = _tombstone_item(row, actor_id=get_auth_context(request).subject_id, deleted_via="items.delete")
    await db.commit()
    await _schedule_palace_dirty(request, [row.id], "item-soft-delete")
    return ItemDeleteResponse(deleted=True, item_id=row.id, status=row.status, deleted_at=deleted_at)


@router.post("/{item_id}/restore", response_model=ItemRestoreResponse, dependencies=[Depends(require_api_capability("write"))])
async def restore_item(
    item_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(Item, item_id)
    if not row:
        raise HTTPException(status_code=404, detail="Item not found")
    if str(row.tenant_id) != request.state.tenant_id:
        raise HTTPException(status_code=404, detail="Item not found")
    if _is_deleted(row):
        _restore_item(row)
        await db.commit()
        await db.refresh(row)
        await _schedule_palace_dirty(request, [row.id], "item-restore")
    return ItemRestoreResponse(restored=True, item=ItemResponse.model_validate(row))


@router.delete("/{item_id}/hard", dependencies=[Depends(require_api_capability("write"))])
async def hard_delete_item_route(
    item_id: uuid.UUID,
    request: Request,
    confirmation: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Permanently delete one tenant-owned item and its dependent rows."""
    if confirmation != str(item_id):
        raise HTTPException(status_code=422, detail="confirmation must equal item_id")
    deleted = await hard_delete_item(
        db,
        arq_pool=request.app.state.arq_pool,
        tenant_id=request.state.tenant_id,
        item_id=item_id,
        actor_id=get_auth_context(request).subject_id or "unknown",
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"deleted": True, "item_id": item_id, "permanent": True}


@router.get("/{item_id}/related", response_model=RelatedItemsResponse, dependencies=[Depends(require_api_capability("read"))])
async def get_related(
    item_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    # Verify the originating item belongs to this tenant before returning relationships
    row = await db.get(Item, item_id)
    if not row or _is_deleted(row):
        raise HTTPException(status_code=404, detail="Item not found")
    if str(row.tenant_id) != request.state.tenant_id:
        raise HTTPException(status_code=404, detail="Item not found")

    sql = sa_text("""
        SELECT r.target_item_id AS item_id, i.title, i.source_type,
               r.relationship, r.confidence
        FROM item_relationships r
        JOIN items i ON r.target_item_id = i.id
        WHERE r.source_item_id = CAST(:item_id AS uuid)
          AND r.tenant_id = :tenant_id
          AND i.tenant_id = :tenant_id
          AND i.status != 'deleted'
          AND i.deleted_at IS NULL

        UNION

        SELECT r.source_item_id AS item_id, i.title, i.source_type,
               r.relationship, r.confidence
        FROM item_relationships r
        JOIN items i ON r.source_item_id = i.id
        WHERE r.target_item_id = CAST(:item_id AS uuid)
          AND r.tenant_id = :tenant_id
          AND i.tenant_id = :tenant_id
          AND i.status != 'deleted'
          AND i.deleted_at IS NULL

        ORDER BY confidence DESC
    """)
    rows = (
        await db.execute(
            sql,
            {"item_id": str(item_id), "tenant_id": request.state.tenant_id},
        )
    ).fetchall()
    return RelatedItemsResponse(
        relationships=[
            RelatedItemResponse(
                item_id=row.item_id,
                title=row.title,
                source_type=row.source_type,
                relationship=row.relationship,
                confidence=row.confidence,
            )
            for row in rows
        ]
    )
