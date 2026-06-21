from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_api_key
from app.config import settings
from app.database import get_db
from app.models.item import Item
from app.models.embedding import Embedding
from app.models.job import Job
from app.models.feed import Feed
from app.services.graph_telemetry import count_orphaned_ready_items

router = APIRouter(tags=["system"])


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/version")
async def version():
    return {
        "name": "Palace of Truth",
        "version": settings.app_version or "0.1.0",
    }


async def _check_database_ready(db: AsyncSession) -> dict:
    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:
        return {"status": "unhealthy", "error_class": exc.__class__.__name__}
    return {"status": "ok"}


async def _check_queue_ready(request: Request) -> dict:
    pool = getattr(request.app.state, "arq_pool", None)
    if pool is None:
        return {"status": "degraded", "message": "ARQ pool unavailable"}
    ping = getattr(pool, "ping", None)
    if ping is None:
        return {"status": "degraded", "message": "ARQ ping unavailable"}
    try:
        result = await ping()
    except Exception as exc:
        return {"status": "unhealthy", "error_class": exc.__class__.__name__}
    return {"status": "ok", "ping": bool(result)}


@router.get("/ready")
async def readiness(request: Request, db: AsyncSession = Depends(get_db)):
    dependencies = {
        "database": await _check_database_ready(db),
        "queue": await _check_queue_ready(request),
    }
    statuses = {dependency["status"] for dependency in dependencies.values()}
    status = "unhealthy" if "unhealthy" in statuses else "degraded" if "degraded" in statuses else "ok"
    return {
        "status": status,
        "version": settings.app_version or "0.1.0",
        "dependencies": dependencies,
    }


@router.get("/stats", dependencies=[Depends(verify_api_key)])
async def stats(request: Request, db: AsyncSession = Depends(get_db)):
    tid = request.state.tenant_id

    total_items = (
        await db.execute(
            select(func.count()).select_from(Item).where(
                Item.tenant_id == tid,
                Item.status != "failed",
            )
        )
    ).scalar_one()
    ready_items = (
        await db.execute(
            select(func.count()).select_from(Item).where(
                Item.tenant_id == tid,
                Item.status == "ready",
                Item.deleted_at.is_(None),
            )
        )
    ).scalar_one()
    indexed_items = (
        await db.execute(
            select(func.count(func.distinct(Embedding.item_id)))
            .select_from(Embedding)
            .join(Item, Embedding.item_id == Item.id)
            .where(
                Item.tenant_id == tid,
                Item.status == "ready",
                Item.deleted_at.is_(None),
            )
        )
    ).scalar_one()
    embedding_chunks = (
        await db.execute(
            select(func.count()).select_from(Embedding)
            .join(Item, Embedding.item_id == Item.id)
            .where(
                Item.tenant_id == tid,
                Item.status == "ready",
                Item.deleted_at.is_(None),
            )
        )
    ).scalar_one()
    orphaned_ready_items = await count_orphaned_ready_items(db, tid)

    type_rows = (
        await db.execute(
            select(Item.source_type, func.count().label("n"))
            .where(
                Item.tenant_id == tid,
                Item.status != "failed",
                Item.status != "deleted",
                Item.deleted_at.is_(None),
            )
            .group_by(Item.source_type)
        )
    ).all()
    by_source_type = {row.source_type: row.n for row in type_rows}

    active_jobs = (await db.execute(
        select(func.count()).select_from(Job)
        .where(Job.tenant_id == tid, Job.status.in_(["queued", "processing"]))
    )).scalar_one()

    failed_memory_jobs = (await db.execute(
        select(func.count()).select_from(Job)
        .where(
            Job.tenant_id == tid,
            Job.job_type == "memory_artifact",
            Job.status == "failed",
        )
    )).scalar_one()

    active_memory_jobs = (await db.execute(
        select(func.count()).select_from(Job)
        .where(
            Job.tenant_id == tid,
            Job.job_type == "memory_artifact",
            Job.status.in_(["queued", "processing"]),
        )
    )).scalar_one()

    feed_count = (await db.execute(
        select(func.count()).select_from(Feed).where(Feed.tenant_id == tid)
    )).scalar_one()

    return {
        "total_items": total_items,
        "ready_items": ready_items,
        "by_source_type": by_source_type,
        "indexed_items": indexed_items,
        "embedding_chunks": embedding_chunks,
        # Compatibility alias for any callers still expecting the old name.
        "total_embeddings": embedding_chunks,
        "orphaned_ready_items": orphaned_ready_items,
        "active_jobs": active_jobs,
        "failed_memory_jobs": failed_memory_jobs,
        "active_memory_jobs": active_memory_jobs,
        "feed_count": feed_count,
    }
