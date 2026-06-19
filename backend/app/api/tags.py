from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_api_key
from app.database import get_db
from app.schemas.tag import TagListResponse

router = APIRouter(prefix="/tags", tags=["tags"], dependencies=[Depends(verify_api_key)])


@router.get("", response_model=TagListResponse)
async def list_tags(
    request: Request,
    q: str | None = Query(None, description="Optional prefix filter"),
    db: AsyncSession = Depends(get_db),
):
    sql = sa_text("""
        SELECT DISTINCT tag
        FROM items, unnest(tags) AS tag
        WHERE status = 'ready'
          AND deleted_at IS NULL
          AND tenant_id = :tenant_id
          AND cardinality(tags) > 0
          AND (CAST(:q AS text) IS NULL OR tag ILIKE CAST(:q AS text) || '%')
        ORDER BY tag
    """)
    rows = (await db.execute(sql, {"q": q, "tenant_id": request.state.tenant_id})).fetchall()
    tags = [row.tag for row in rows]
    return TagListResponse(tags=tags, total=len(tags))
