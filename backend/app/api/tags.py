from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_api_capability
from app.database import get_db
from app.schemas.tag import TagListResponse

router = APIRouter(prefix="/tags", tags=["tags"], dependencies=[Depends(require_api_capability("read"))])


@router.get("", response_model=TagListResponse)
async def list_tags(
    request: Request,
    q: str | None = Query(None, description="Optional prefix filter"),
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0, le=10_000),
    db: AsyncSession = Depends(get_db),
):
    sql = sa_text("""
        SELECT DISTINCT tag
        FROM items, unnest(tags) AS tag
        WHERE status = 'ready'
          AND deleted_at IS NULL
          AND tenant_id = :tenant_id
          AND cardinality(tags) > 0
          AND (CAST(:q AS text) IS NULL OR tag ILIKE CAST(:q AS text) || '%' ESCAPE '\\')
        ORDER BY tag
        LIMIT :limit OFFSET :offset
    """)
    escaped_q = None if q is None else q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = (
        await db.execute(
            sql,
            {
                "q": escaped_q,
                "tenant_id": request.state.tenant_id,
                "limit": limit,
                "offset": offset,
            },
        )
    ).fetchall()
    tags = [row.tag for row in rows]
    return TagListResponse(tags=tags, total=len(tags))
