from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job import Job, JobAttempt

ACTIVE_ATTEMPT_STATUSES = {"queued", "processing"}
TERMINAL_ATTEMPT_STATUSES = {"completed", "failed", "dead_lettered"}
MAX_ERROR_SUMMARY_LENGTH = 500
_SECRET_VALUE = re.compile(
    r"(?i)(authorization|api[_-]?key|token|secret|password|signing[_-]?key)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)


def sanitize_error_summary(error: object | None) -> str | None:
    """Return a bounded diagnostic summary without common credential values."""
    if error is None:
        return None
    summary = " ".join(str(error).split())
    summary = _SECRET_VALUE.sub(lambda match: f"{match.group(1)}{match.group(2)}[redacted]", summary)
    return summary[:MAX_ERROR_SUMMARY_LENGTH]


async def create_job_attempt(
    db: AsyncSession,
    *,
    job_id: uuid.UUID,
    tenant_id: str,
    trigger: str,
    arq_job_id: str | None = None,
    job_try: int | None = None,
    recovered_from_id: uuid.UUID | None = None,
) -> JobAttempt:
    """Create the next attempt while serializing creators on the parent job row."""
    job = await db.get(Job, job_id, with_for_update=True)
    if job is None:
        raise ValueError(f"job {job_id} does not exist")
    if job.tenant_id != tenant_id:
        raise ValueError("job tenant does not match attempt tenant")

    active = (
        await db.execute(
            select(JobAttempt.id).where(
                JobAttempt.job_id == job_id,
                JobAttempt.status.in_(ACTIVE_ATTEMPT_STATUSES),
            )
        )
    ).scalar_one_or_none()
    if active is not None:
        raise ValueError(f"job {job_id} already has an active attempt")

    next_number = (
        await db.execute(
            select(func.coalesce(func.max(JobAttempt.attempt_number), 0) + 1).where(
                JobAttempt.job_id == job_id
            )
        )
    ).scalar_one()
    attempt = JobAttempt(
        job_id=job_id,
        tenant_id=tenant_id,
        attempt_number=next_number,
        trigger=trigger[:32],
        status="queued",
        arq_job_id=arq_job_id[:255] if arq_job_id else None,
        job_try=job_try,
        recovered_from_id=recovered_from_id,
    )
    db.add(attempt)
    await db.flush()
    return attempt


async def ensure_active_job_attempt(
    db: AsyncSession,
    *,
    job_id: uuid.UUID,
    tenant_id: str,
    trigger: str = "initial",
    arq_job_id: str | None = None,
    job_try: int | None = None,
) -> JobAttempt:
    """Return the active attempt or create one for legacy enqueue paths."""
    attempt = (
        await db.execute(
            select(JobAttempt)
            .where(JobAttempt.job_id == job_id, JobAttempt.status.in_(ACTIVE_ATTEMPT_STATUSES))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if attempt is None:
        return await create_job_attempt(
            db, job_id=job_id, tenant_id=tenant_id, trigger=trigger,
            arq_job_id=arq_job_id, job_try=job_try,
        )
    if arq_job_id and not attempt.arq_job_id:
        attempt.arq_job_id = arq_job_id[:255]
    if job_try is not None:
        attempt.job_try = job_try
    return attempt


async def active_job_attempt(db: AsyncSession, *, job_id: uuid.UUID) -> JobAttempt | None:
    return (
        await db.execute(
            select(JobAttempt)
            .where(JobAttempt.job_id == job_id, JobAttempt.status.in_(ACTIVE_ATTEMPT_STATUSES))
            .order_by(JobAttempt.attempt_number.desc())
            .with_for_update()
        )
    ).scalars().first()


async def _locked_attempt(db: AsyncSession, attempt_id: uuid.UUID) -> JobAttempt:
    attempt = (
        await db.execute(select(JobAttempt).where(JobAttempt.id == attempt_id).with_for_update())
    ).scalar_one_or_none()
    if attempt is None:
        raise ValueError(f"job attempt {attempt_id} does not exist")
    return attempt


async def mark_job_attempt_started(
    db: AsyncSession, *, attempt_id: uuid.UUID, at: datetime | None = None
) -> JobAttempt:
    attempt = await _locked_attempt(db, attempt_id)
    if attempt.status == "queued":
        attempt.status = "processing"
        attempt.started_at = at or datetime.now(timezone.utc)
    return attempt


async def mark_job_attempt_completed(
    db: AsyncSession, *, attempt_id: uuid.UUID, at: datetime | None = None
) -> JobAttempt:
    return await _finish_attempt(db, attempt_id=attempt_id, status="completed", at=at)


async def mark_job_attempt_failed(
    db: AsyncSession,
    *,
    attempt_id: uuid.UUID,
    failure_kind: str,
    error: object | None = None,
    at: datetime | None = None,
) -> JobAttempt:
    return await _finish_attempt(
        db,
        attempt_id=attempt_id,
        status="failed",
        at=at,
        failure_kind=failure_kind,
        error=error,
    )


async def mark_job_attempt_dead_lettered(
    db: AsyncSession,
    *,
    attempt_id: uuid.UUID,
    failure_kind: str,
    error: object | None = None,
    at: datetime | None = None,
) -> JobAttempt:
    return await _finish_attempt(
        db,
        attempt_id=attempt_id,
        status="dead_lettered",
        at=at,
        failure_kind=failure_kind,
        error=error,
    )


async def _finish_attempt(
    db: AsyncSession,
    *,
    attempt_id: uuid.UUID,
    status: str,
    at: datetime | None,
    failure_kind: str | None = None,
    error: object | None = None,
) -> JobAttempt:
    attempt = await _locked_attempt(db, attempt_id)
    # Terminal attempts are immutable, making duplicate worker callbacks harmless.
    if attempt.status in TERMINAL_ATTEMPT_STATUSES:
        return attempt
    finished_at = at or datetime.now(timezone.utc)
    attempt.status = status
    if status == "completed":
        attempt.completed_at = finished_at
    elif status == "failed":
        attempt.failed_at = finished_at
    else:
        attempt.dead_lettered_at = finished_at
    if failure_kind is not None:
        attempt.failure_kind = failure_kind[:48]
        attempt.error_summary = sanitize_error_summary(error)
    return attempt
