import io
import json
import logging
import re
import tempfile
import zipfile
from contextlib import suppress
from datetime import date, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import select, text as sa_text

from app.auth import verify_api_key
from app.database import async_session
from app.models.item import Item
from app.services.bundle import build_bundle_archive

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/export",
    tags=["export"],
    dependencies=[Depends(verify_api_key)],
)


@router.get("")
async def export_library(
    request: Request,
    background_tasks: BackgroundTasks,
    format: str = Query(..., pattern="^(json|markdown|bundle)$"),
    source_type: str | None = Query(None),
    tags: str | None = Query(None),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
):
    """Stream a zip of all ready items matching the given filters.

    format: 'json' (single items.json) or 'markdown' (one .md per item).
    tags: comma-separated list; items matching ANY tag are included.
    """
    today = date.today().isoformat()
    if format == "bundle":
        if source_type or tags or date_from or date_to:
            raise HTTPException(
                status_code=422,
                detail="Bundle export only supports full-tenant export in v1",
            )
        tmp = tempfile.NamedTemporaryFile(prefix="palaceoftruth-bundle-", suffix=".zip", delete=False)
        tmp.close()
        async with async_session() as db:
            await build_bundle_archive(db, request.state.tenant_id, tmp.name)
        background_tasks.add_task(_cleanup_file, tmp.name)
        return FileResponse(
            tmp.name,
            media_type="application/zip",
            filename=f"palaceoftruth-bundle-{today}.zip",
        )

    async with async_session() as db:
        q = select(Item).where(
            Item.status == "ready",
            Item.tenant_id == request.state.tenant_id,
            Item.deleted_at.is_(None),
        )
        if source_type:
            q = q.where(Item.source_type == source_type)
        if tags:
            tag_list = [t.strip() for t in tags.split(",") if t.strip()]
            if tag_list:
                q = q.where(sa_text("tags && CAST(:tags AS text[])").bindparams(
                    tags="{" + ",".join(tag_list) + "}"
                ))
        if date_from:
            q = q.where(Item.created_at >= date_from)
        if date_to:
            q = q.where(Item.created_at <= date_to)
        q = q.order_by(Item.created_at.desc())
        result = await db.execute(q)
        items = result.scalars().all()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if format == "json":
            _write_json(zf, items)
        else:
            _write_markdown(zf, items)
    buf.seek(0)

    logger.info("Export: %d items, format=%s", len(items), format)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="palaceoftruth-export-{today}.zip"'
        },
    )


def _cleanup_file(path: str) -> None:
    import os

    with suppress(FileNotFoundError):
        os.unlink(path)


def _write_json(zf: zipfile.ZipFile, items: list[Item]) -> None:
    data = [
        {
            "id": str(item.id),
            "title": item.title,
            "source_type": item.source_type,
            "source_url": item.source_url,
            "summary": item.summary,
            "raw_content": item.raw_content,
            "tags": item.tags,
            "categories": item.categories,
            "metadata": item.metadata_,
            "created_at": item.created_at.isoformat(),
            "updated_at": item.updated_at.isoformat() if item.updated_at else None,
        }
        for item in items
    ]
    zf.writestr("items.json", json.dumps(data, ensure_ascii=False, indent=2))


def _write_markdown(zf: zipfile.ZipFile, items: list[Item]) -> None:
    seen_filenames: set[str] = set()
    for item in items:
        slug = re.sub(r"[^\w\s-]", "", item.title.lower())
        slug = re.sub(r"[\s_]+", "-", slug).strip("-")[:60]
        day = item.created_at.date().isoformat()
        filename = f"{day}-{slug}.md"
        if filename in seen_filenames:
            filename = f"{day}-{slug}-{str(item.id)[:8]}.md"
        seen_filenames.add(filename)

        frontmatter = (
            f"---\n"
            f"title: {json.dumps(item.title)}\n"
            f"source_type: {item.source_type}\n"
            f"source_url: {item.source_url or ''}\n"
            f"tags: {json.dumps(item.tags)}\n"
            f"categories: {json.dumps(item.categories)}\n"
            f"created_at: {item.created_at.isoformat()}\n"
            f"---\n\n"
        )
        body_parts = []
        if item.summary:
            body_parts.append(
                "\n".join(f"> {line}" for line in item.summary.splitlines())
            )
            body_parts.append("\n---\n")
        if item.raw_content:
            body_parts.append(item.raw_content)
        zf.writestr(filename, frontmatter + "\n".join(body_parts))
