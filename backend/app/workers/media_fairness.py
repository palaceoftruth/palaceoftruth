"""Tenant-fair scheduling for long-running media ingestion jobs."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from arq.jobs import Job as ArqJob, JobStatus
from sqlalchemy import text

from app.config import settings
from app.database import async_session
from app.utils.job_payloads import load_retry_task_from_payload
from app.workers.queues import MEDIA_TASK_NAMES, MEDIA_WORKER_QUEUE

logger = logging.getLogger(__name__)

MEDIA_FAIR_DISPATCH_TASK_NAME = "dispatch_tenant_fair_media_jobs"
MEDIA_JOB_TYPES = frozenset({"media", "video", "youtube"})
_ACTIVE_ARQ_STATUSES = {JobStatus.queued, JobStatus.deferred, JobStatus.in_progress}


@dataclass(frozen=True)
class PendingMediaJob:
    id: Any
    tenant_id: str
    job_type: str
    source_url: str | None
    payload: dict | None
    created_at: datetime


def _positive_setting(value: int, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, parsed)


async def _is_active_in_media_queue(redis, job_id: str) -> bool:
    try:
        status = await ArqJob(job_id, redis=redis, _queue_name=MEDIA_WORKER_QUEUE).status()
    except Exception as exc:
        logger.warning(
            "media_fairness: could not inspect ARQ media status for job %s; treating as active: %s",
            job_id,
            exc,
        )
        return True
    return status in _ACTIVE_ARQ_STATUSES


def _build_media_task(row: PendingMediaJob) -> tuple[str, dict[str, Any]] | None:
    task_name = "process_media"
    task_kwargs: dict[str, Any] = {
        "job_id": str(row.id),
        "tenant_id": row.tenant_id,
    }
    restored = load_retry_task_from_payload(
        job_type=row.job_type,
        job_id=row.id,
        tenant_id=row.tenant_id,
        payload=row.payload,
        expected_task_name=task_name,
    )
    if restored is not None:
        task_name, task_kwargs = restored

    if task_name not in MEDIA_TASK_NAMES:
        logger.warning(
            "media_fairness: job %s restored unexpected media task %s; skipping",
            row.id,
            task_name,
        )
        return None
    if row.source_url:
        task_kwargs["url"] = row.source_url
    if not task_kwargs.get("url"):
        logger.warning("media_fairness: job %s has no source_url; skipping dispatch", row.id)
        return None
    task_kwargs["tenant_id"] = row.tenant_id
    task_kwargs["job_id"] = str(row.id)
    return task_name, task_kwargs


def _choose_fair_jobs(
    pending: list[PendingMediaJob],
    *,
    active_by_tenant: dict[str, int],
    active_job_ids: set[str],
    limit: int,
    per_tenant_limit: int,
) -> list[PendingMediaJob]:
    grouped: dict[str, list[PendingMediaJob]] = defaultdict(list)
    for row in pending:
        if str(row.id) not in active_job_ids:
            grouped[row.tenant_id].append(row)

    tenant_order = sorted(grouped, key=lambda tenant_id: (grouped[tenant_id][0].created_at, tenant_id))
    selected: list[PendingMediaJob] = []
    while len(selected) < limit:
        made_progress = False
        for tenant_id in tenant_order:
            if len(selected) >= limit:
                break
            if active_by_tenant.get(tenant_id, 0) >= per_tenant_limit:
                continue
            rows = grouped.get(tenant_id) or []
            if not rows:
                continue
            row = rows.pop(0)
            selected.append(row)
            active_by_tenant[tenant_id] = active_by_tenant.get(tenant_id, 0) + 1
            made_progress = True
        if not made_progress:
            break
    return selected


async def dispatch_tenant_fair_media_jobs(
    ctx: dict,
    *,
    limit: int | None = None,
    per_tenant_limit: int | None = None,
    candidate_limit: int | None = None,
) -> int:
    """Move queued media DB jobs into the ARQ media queue without tenant starvation."""
    dispatch_limit = _positive_setting(
        limit if limit is not None else settings.media_tenant_fair_dispatch_batch_size,
        default=2,
    )
    tenant_limit = _positive_setting(
        per_tenant_limit if per_tenant_limit is not None else settings.media_tenant_fair_per_tenant_inflight_limit,
        default=1,
    )
    candidates = _positive_setting(
        candidate_limit if candidate_limit is not None else settings.media_tenant_fair_candidate_limit,
        default=100,
    )

    async with async_session() as db:
        pending_rows = (
            await db.execute(
                text(
                    """
                    WITH ranked AS (
                        SELECT
                            j.id,
                            j.tenant_id,
                            j.job_type,
                            j.payload,
                            j.created_at,
                            i.source_url,
                            ROW_NUMBER() OVER (
                                PARTITION BY j.tenant_id
                                ORDER BY j.created_at ASC, j.id ASC
                            ) AS tenant_rank
                        FROM jobs j
                        LEFT JOIN items i ON i.id = j.item_id
                        WHERE (
                            j.status = 'queued'
                            OR (
                                j.status = 'pending_availability'
                                AND (j.payload->'pending_availability'->>'retry_after_at')::timestamptz <= NOW()
                            )
                        )
                          AND j.job_type IN ('media', 'video', 'youtube')
                    )
                    SELECT id, tenant_id, job_type, payload, created_at, source_url
                    FROM ranked
                    WHERE tenant_rank <= :candidate_limit
                    ORDER BY created_at ASC, id ASC
                    """
                ),
                {"candidate_limit": candidates},
            )
        ).fetchall()
        active_rows = (
            await db.execute(
                text(
                    """
                    SELECT tenant_id, COUNT(*) AS active_count
                    FROM jobs
                    WHERE status = 'processing'
                      AND job_type IN ('media', 'video', 'youtube')
                    GROUP BY tenant_id
                    """
                )
            )
        ).fetchall()

    pending = [
        PendingMediaJob(
            id=row.id,
            tenant_id=row.tenant_id,
            job_type=row.job_type,
            source_url=row.source_url,
            payload=row.payload,
            created_at=row.created_at,
        )
        for row in pending_rows
    ]
    if not pending:
        return 0

    active_by_tenant = {row.tenant_id: int(row.active_count) for row in active_rows}
    active_job_ids: set[str] = set()
    for row in pending:
        if await _is_active_in_media_queue(ctx["redis"], str(row.id)):
            active_job_ids.add(str(row.id))
            active_by_tenant[row.tenant_id] = active_by_tenant.get(row.tenant_id, 0) + 1

    selected = _choose_fair_jobs(
        pending,
        active_by_tenant=active_by_tenant,
        active_job_ids=active_job_ids,
        limit=dispatch_limit,
        per_tenant_limit=tenant_limit,
    )
    dispatched = 0
    for row in selected:
        task = _build_media_task(row)
        if task is None:
            continue
        task_name, task_kwargs = task
        await ctx["redis"].enqueue_job(
            task_name,
            _job_id=str(row.id),
            _queue_name=MEDIA_WORKER_QUEUE,
            **task_kwargs,
        )
        dispatched += 1
    if dispatched:
        logger.info("media_fairness: dispatched %d tenant-fair media job(s)", dispatched)
    return dispatched
