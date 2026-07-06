import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_api_capability, verify_capture_job_read_auth
from app.database import get_db
from app.models.item import Item
from app.models.web_save import WebSave
from app.schemas.web_save import (
    WebSaveItemSummary,
    WebSaveListResponse,
    WebSaveResponse,
    WebSaveUpdate,
)

router = APIRouter(prefix="/web-saves", tags=["web-saves"])

_SORT_ORDERS = frozenset({"asc", "desc"})
_CAPTURE_KINDS = frozenset({"webpage", "social_post", "media", "selection_note"})


def _to_response(web_save: WebSave, item: Item) -> WebSaveResponse:
    return WebSaveResponse(
        id=web_save.id,
        item_id=web_save.item_id,
        original_url=web_save.original_url,
        normalized_url=web_save.normalized_url,
        source_title=web_save.source_title,
        source_domain=web_save.source_domain,
        capture_kind=web_save.capture_kind,  # type: ignore[arg-type]
        user_tags=web_save.user_tags or [],
        saved_at=web_save.saved_at,
        archived_at=web_save.archived_at,
        extension_version=web_save.extension_version,
        metadata=web_save.metadata_ or {},
        item=WebSaveItemSummary(
            id=item.id,
            title=item.title,
            source_type=item.source_type,
            status=item.status,
            summary=item.summary,
            tags=item.tags or [],
            metadata=item.metadata_ or {},
            created_at=item.created_at,
            updated_at=item.updated_at,
        ),
    )


@router.get("", response_model=WebSaveListResponse, dependencies=[Depends(verify_capture_job_read_auth)])
async def list_web_saves(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    active_only: bool = Query(True),
    q: str | None = Query(None, description="Search title, URL, domain, summary, or user tags."),
    capture_kind: str | None = Query(None),
    tag: str | None = Query(None),
    sort: str = Query("saved_at"),
    order: str = Query("desc"),
    db: AsyncSession = Depends(get_db),
) -> WebSaveListResponse:
    order_key = order.strip().lower()
    if order_key not in _SORT_ORDERS:
        raise HTTPException(status_code=422, detail=f"Unsupported sort order: {order}")
    if capture_kind and capture_kind not in _CAPTURE_KINDS:
        raise HTTPException(status_code=422, detail=f"Unsupported capture kind: {capture_kind}")

    query = (
        select(WebSave, Item)
        .join(Item, Item.id == WebSave.item_id)
        .where(WebSave.tenant_id == request.state.tenant_id)
        .where(Item.tenant_id == request.state.tenant_id)
        .where(Item.deleted_at.is_(None))
        .where(Item.status != "deleted")
    )
    if active_only:
        query = query.where(WebSave.archived_at.is_(None))
    if capture_kind:
        query = query.where(WebSave.capture_kind == capture_kind)
    if tag:
        query = query.where(WebSave.user_tags.contains([tag]))
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        query = query.where(
            or_(
                WebSave.source_title.ilike(pattern),
                WebSave.original_url.ilike(pattern),
                WebSave.normalized_url.ilike(pattern),
                WebSave.source_domain.ilike(pattern),
                func.array_to_string(WebSave.user_tags, " ").ilike(pattern),
                Item.title.ilike(pattern),
                Item.summary.ilike(pattern),
            )
        )

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()

    if sort == "title":
        sort_column = func.lower(func.coalesce(WebSave.source_title, Item.title))
    elif sort == "saved_at":
        sort_column = WebSave.saved_at
    else:
        raise HTTPException(status_code=422, detail=f"Unsupported sort field: {sort}")

    primary_order = sort_column.asc() if order_key == "asc" else sort_column.desc()
    rows = (
        await db.execute(
            query.order_by(primary_order, WebSave.id.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
    ).all()

    return WebSaveListResponse(
        web_saves=[_to_response(web_save, item) for web_save, item in rows],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.patch("/{web_save_id}", response_model=WebSaveResponse, dependencies=[Depends(require_api_capability("write"))])
async def update_web_save(
    web_save_id: uuid.UUID,
    body: WebSaveUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> WebSaveResponse:
    row = (
        await db.execute(
            select(WebSave, Item)
            .join(Item, Item.id == WebSave.item_id)
            .where(WebSave.id == web_save_id)
            .where(WebSave.tenant_id == request.state.tenant_id)
            .where(Item.tenant_id == request.state.tenant_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Web save not found")

    web_save, item = row
    web_save.archived_at = datetime.now(timezone.utc) if body.archived else None
    await db.commit()
    await db.refresh(web_save)
    return _to_response(web_save, item)
