import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query, HTTPException, Request
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_api_key, verify_capture_job_read_auth
from app.database import get_db
from app.models.item import Item
from app.models.job import Job, JobProgressEvent
from app.schemas.job import JobProgressEventResponse, JobResponse, JobListResponse
from app.services.job_progress import record_job_progress_event
from app.utils.job_payloads import load_retry_task_from_payload
from app.utils.webhook import maybe_dispatch_webhook
from app.workers.queues import enqueue_worker_job

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["jobs"])

_VISIBLE_JOB_TYPES = frozenset({"media", "video", "webpage", "pdf", "doc", "image", "note"})
_JOB_TYPE_TO_TASK = {
    "media": "process_media",
    "video": "process_media",
    "webpage": "process_webpage",
    "pdf": "process_pdf",
    "doc": "process_doc",
    "image": "process_image",
    "note": "process_note",
}


def _ensure_visible_job(job: Job | None) -> Job:
    if not job or job.job_type not in _VISIBLE_JOB_TYPES:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


async def _recent_progress_events(
    db: AsyncSession,
    *,
    job_ids: list[uuid.UUID],
    tenant_id: str,
    limit_per_job: int = 8,
) -> dict[uuid.UUID, list[JobProgressEventResponse]]:
    if not job_ids:
        return {}
    rows = (
        await db.execute(
            select(JobProgressEvent)
            .where(JobProgressEvent.tenant_id == tenant_id)
            .where(JobProgressEvent.job_id.in_(job_ids))
            .order_by(JobProgressEvent.job_id.asc(), JobProgressEvent.created_at.desc())
        )
    ).scalars().all()
    grouped: dict[uuid.UUID, list[JobProgressEventResponse]] = {job_id: [] for job_id in job_ids}
    for event in rows:
        bucket = grouped.setdefault(event.job_id, [])
        if len(bucket) < limit_per_job:
            bucket.append(JobProgressEventResponse.model_validate(event))
    return grouped


async def _job_response(db: AsyncSession, job: Job, tenant_id: str) -> JobResponse:
    response = JobResponse.model_validate(job)
    events = await _recent_progress_events(db, job_ids=[job.id], tenant_id=tenant_id)
    response.recent_progress_events = events.get(job.id, [])
    return response


async def _extension_visible_job_ids(db: AsyncSession, request: Request) -> list[uuid.UUID] | None:
    if getattr(request.state, "auth_mode", None) != "browser_extension":
        return None
    client_id = getattr(request.state, "mcp_client_id", None)
    if client_id is None:
        return []
    rows = (
        await db.execute(
            text(
                """
                SELECT DISTINCT (params_summary->>'job_id')::uuid AS job_id
                FROM mcp_request_audit_events
                WHERE tenant_id = :tenant_id
                  AND client_id = :client_id
                  AND operation = 'browser_extension.capture'
                  AND status = 'success'
                  AND params_summary ? 'job_id'
                """
            ),
            {"tenant_id": request.state.tenant_id, "client_id": client_id},
        )
    ).mappings().all()
    return [row["job_id"] for row in rows if row["job_id"] is not None]


def _build_retry_task(job: Job, item: Item | None) -> tuple[str, dict[str, Any]]:
    task_name = _JOB_TYPE_TO_TASK.get(job.job_type)
    if not task_name:
        raise HTTPException(status_code=422, detail=f"Unsupported job_type for retry: {job.job_type}")

    if job.job_type in ("media", "video", "webpage"):
        if not (item and item.source_url):
            raise HTTPException(status_code=409, detail="No source URL on item; cannot retry")

    restored = load_retry_task_from_payload(
        job_type=job.job_type,
        job_id=job.id,
        tenant_id=job.tenant_id,
        payload=job.payload,
        expected_task_name=task_name,
    )
    if restored is not None:
        if job.job_type in ("media", "video", "webpage"):
            restored_task_name, restored_kwargs = restored
            restored_kwargs["url"] = item.source_url
            return restored_task_name, restored_kwargs
        return restored

    task_kwargs: dict[str, Any] = {"job_id": str(job.id), "tenant_id": job.tenant_id}

    if job.job_type in ("media", "video", "webpage"):
        task_kwargs["url"] = item.source_url
    elif job.job_type == "pdf":
        if not (item and item.raw_content):
            raise HTTPException(status_code=409, detail="Source file no longer on disk; re-upload required")
        task_kwargs["extracted_text"] = item.raw_content
        task_kwargs["pdf_metadata"] = {}
    elif job.job_type == "doc":
        if not (item and item.raw_content):
            raise HTTPException(status_code=409, detail="Source file no longer on disk; re-upload required")
        task_kwargs["extracted_text"] = item.raw_content
        task_kwargs["doc_metadata"] = {}
    elif job.job_type == "image":
        if not (item and item.raw_content):
            raise HTTPException(status_code=409, detail="No image description on item; re-upload required")
        task_kwargs["description"] = item.raw_content
        task_kwargs["image_metadata"] = {}
    elif job.job_type == "note":
        if not (item and item.raw_content):
            raise HTTPException(status_code=409, detail="No raw_content on item; re-ingest required")
        task_kwargs["title"] = item.title
        task_kwargs["content"] = item.raw_content

    return task_name, task_kwargs


def _snapshot_retry_state(job: Job, item: Item | None) -> dict[str, Any]:
    return {
        "job": {
            "status": job.status,
            "progress": job.progress,
            "error_message": job.error_message,
            "completed_at": job.completed_at,
            "duplicate_of": job.duplicate_of,
            "created_at": job.created_at,
        },
        "item_status": item.status if item else None,
    }


async def _restore_retry_state(
    *,
    db: AsyncSession,
    job: Job,
    item: Item | None,
    snapshot: dict[str, Any],
) -> None:
    job_state = snapshot["job"]
    job.status = job_state["status"]
    job.progress = job_state["progress"]
    job.error_message = job_state["error_message"]
    job.completed_at = job_state["completed_at"]
    job.duplicate_of = job_state["duplicate_of"]
    job.created_at = job_state["created_at"]
    if item:
        item.status = snapshot["item_status"]
    await db.commit()


@router.get("", response_model=JobListResponse, dependencies=[Depends(verify_capture_job_read_auth)])
async def list_jobs(
    request: Request,
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    q = select(Job).where(Job.tenant_id == request.state.tenant_id).where(Job.job_type.in_(_VISIBLE_JOB_TYPES))
    visible_job_ids = await _extension_visible_job_ids(db, request)
    if visible_job_ids is not None:
        if not visible_job_ids:
            return JobListResponse(jobs=[], total=0)
        q = q.where(Job.id.in_(visible_job_ids))
    if status:
        q = q.where(Job.status == status)

    count_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(count_q)).scalar_one()

    q = q.offset((page - 1) * per_page).limit(per_page)
    rows = (await db.execute(q)).scalars().all()

    events = await _recent_progress_events(db, job_ids=[r.id for r in rows], tenant_id=request.state.tenant_id, limit_per_job=3)
    jobs = []
    for row in rows:
        response = JobResponse.model_validate(row)
        response.recent_progress_events = events.get(row.id, [])
        jobs.append(response)

    return JobListResponse(
        jobs=jobs,
        total=total,
    )


@router.get("/{job_id}", response_model=JobResponse, dependencies=[Depends(verify_capture_job_read_auth)])
async def get_job(job_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    row = _ensure_visible_job(await db.get(Job, job_id))
    if row.tenant_id != request.state.tenant_id:
        raise HTTPException(status_code=404, detail="Job not found")
    visible_job_ids = await _extension_visible_job_ids(db, request)
    if visible_job_ids is not None and job_id not in visible_job_ids:
        raise HTTPException(status_code=404, detail="Job not found")
    return await _job_response(db, row, request.state.tenant_id)


@router.delete("/{job_id}", dependencies=[Depends(verify_api_key)])
async def cancel_job(job_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    job = _ensure_visible_job(await db.get(Job, job_id))
    if job.tenant_id != request.state.tenant_id:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "queued":
        raise HTTPException(status_code=409, detail=f"Job is {job.status}; only queued jobs can be cancelled")

    job.status = "cancelled"
    job.completed_at = datetime.now(timezone.utc)

    job_id_str = str(job.id)

    # Also mark associated item as failed so it doesn't sit as processing
    if job.item_id:
        item = await db.get(Item, job.item_id)
        if item and item.status == "processing":
            item.status = "failed"

    await db.commit()

    if job.webhook_url:
        try:
            await maybe_dispatch_webhook(request.app.state.arq_pool, job_id_str)
        except Exception as exc:
            logger.error("webhook dispatch failed for cancelled job %s: %s", job_id_str, exc)

    return {"cancelled": True, "job_id": job_id_str}


@router.post("/{job_id}/retry", response_model=JobResponse, dependencies=[Depends(verify_api_key)])
async def retry_job(job_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    job = _ensure_visible_job(await db.get(Job, job_id))
    if job.tenant_id != request.state.tenant_id:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in ("failed", "cancelled"):
        raise HTTPException(status_code=409, detail=f"Job is {job.status}; only failed or cancelled jobs can be retried")

    item = await db.get(Item, job.item_id) if job.item_id else None
    # Build the worker payload first so validation failures do not leave the job queued.
    task_name, task_kwargs = _build_retry_task(job, item)
    previous_state = _snapshot_retry_state(job, item)

    job.status = "queued"
    job.progress = 0
    job.error_message = None
    job.completed_at = None
    job.duplicate_of = None
    job.created_at = datetime.now(timezone.utc)
    if item:
        item.status = "processing"
    await record_job_progress_event(
        db,
        job=job,
        phase="retry",
        status="queued",
        progress=0,
        message="Job retry requested",
    )
    await db.commit()
    try:
        await enqueue_worker_job(request.app.state.arq_pool, task_name, **task_kwargs)
    except Exception as exc:
        logger.exception("retry enqueue failed for job %s", job_id)
        try:
            await _restore_retry_state(
                db=db,
                job=job,
                item=item,
                snapshot=previous_state,
            )
        except Exception:
            logger.exception("failed to restore retry state for job %s", job_id)
            raise HTTPException(
                status_code=503,
                detail="Retry enqueue failed and job state restoration also failed",
            ) from exc
        raise HTTPException(
            status_code=503,
            detail="Retry enqueue failed; job state restored",
        ) from exc
    return await _job_response(db, job, request.state.tenant_id)
