"""Read committed checkpoint evidence, independent of queue/index readiness."""
import hashlib

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.item import Item
from app.models.job import Job
from app.models.palace import MemoryEntry


async def read_checkpoint_receipt(db: AsyncSession, *, job: Job, tenant_id: str) -> dict:
    """All three records must exist in this fresh read transaction and agree."""
    payload = job.payload or {}
    if job.tenant_id != tenant_id or not job.item_id:
        raise HTTPException(status_code=404, detail="Checkpoint not found")
    item = await db.get(Item, job.item_id)
    try:
        import uuid
        entry_id = uuid.UUID(payload["memory_entry_id"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(status_code=409, detail="Checkpoint canonical pointer missing")
    entry = await db.get(MemoryEntry, entry_id)
    if (not item or not entry or item.tenant_id != tenant_id or entry.tenant_id != tenant_id
            or entry.item_id != item.id or item.deleted_at is not None
            or item.status == "deleted" or not isinstance(item.raw_content, str)
            or not item.raw_content or entry.idempotency_key != payload.get("idempotency_key")
            or entry.scope_type != payload.get("scope_type")
            or entry.scope_key != payload.get("scope_key")):
        raise HTTPException(status_code=409, detail="Checkpoint durable evidence mismatch")
    scope = {"type": entry.scope_type}
    if entry.scope_key is not None:
        scope["key"] = entry.scope_key
    return {"durable": True, "tenant_id": tenant_id, "scope": scope,
            "job_id": str(job.id), "source_item_id": str(item.id),
            "idempotency_key": entry.idempotency_key,
            "body_sha256": hashlib.sha256(item.raw_content.encode("utf-8")).hexdigest()}
