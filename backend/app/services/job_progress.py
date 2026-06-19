from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.job import Job, JobProgressEvent

logger = logging.getLogger(__name__)

MAX_JOB_PROGRESS_EVENTS_PER_JOB = 100
MAX_JOB_PROGRESS_MESSAGE_LENGTH = 500
SENSITIVE_METADATA_KEYS = {
    "content",
    "raw_content",
    "text",
    "extracted_text",
    "description",
    "api_key",
    "authorization",
    "token",
    "secret",
    "signing_key",
}


def _safe_metadata_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return _safe_metadata(value)
    if isinstance(value, list):
        return [_safe_metadata_value(item) for item in value]
    return str(value)


def _safe_metadata(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    if not metadata:
        return None
    safe: dict[str, Any] = {}
    for key, value in metadata.items():
        if key.lower() in SENSITIVE_METADATA_KEYS:
            safe[key] = "[redacted]"
        else:
            safe[key] = _safe_metadata_value(value)
    return safe


def _trim_message(message: str | None) -> str | None:
    if message is None:
        return None
    return str(message)[:MAX_JOB_PROGRESS_MESSAGE_LENGTH]


async def record_job_progress_event(
    db: AsyncSession,
    *,
    job: Job,
    phase: str,
    status: str,
    progress: int | None = None,
    message: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    if not hasattr(db, "add"):
        logger.debug("skipped job progress event for %s; session does not support add()", job.id)
        return
    db.add(
        JobProgressEvent(
            job_id=job.id,
            tenant_id=job.tenant_id,
            phase=phase,
            status=status,
            progress=progress,
            message=_trim_message(message),
            metadata_=_safe_metadata(metadata),
            created_at=datetime.now(timezone.utc),
        )
    )
    await _compact_job_progress_events(db, job_id=job.id)


async def _compact_job_progress_events(db: AsyncSession, *, job_id: uuid.UUID) -> None:
    keep_ids = (
        await db.execute(
            select(JobProgressEvent.id)
            .where(JobProgressEvent.job_id == job_id)
            .order_by(JobProgressEvent.created_at.desc(), JobProgressEvent.id.desc())
            .limit(MAX_JOB_PROGRESS_EVENTS_PER_JOB)
        )
    ).scalars().all()
    keep_ids = [event_id for event_id in keep_ids if isinstance(event_id, uuid.UUID)]
    if not keep_ids:
        return
    await db.execute(
        delete(JobProgressEvent)
        .where(JobProgressEvent.job_id == job_id)
        .where(JobProgressEvent.id.not_in(keep_ids))
    )


async def record_job_progress_event_best_effort(
    *,
    job_id: uuid.UUID | str,
    phase: str,
    status: str,
    progress: int | None = None,
    message: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    try:
        parsed_job_id = job_id if isinstance(job_id, uuid.UUID) else uuid.UUID(str(job_id))
        async with async_session() as db:
            job = await db.get(Job, parsed_job_id)
            if job is None:
                return
            await record_job_progress_event(
                db,
                job=job,
                phase=phase,
                status=status,
                progress=progress,
                message=message,
                metadata=metadata,
            )
            await db.commit()
    except Exception as exc:
        logger.warning("failed to record job progress event for %s: %s", job_id, exc)


def job_event_status_for_job_status(status: str | None) -> str:
    if status in {"completed", "duplicate"}:
        return "completed"
    if status in {"failed", "cancelled"}:
        return "failed"
    return status or "updated"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
