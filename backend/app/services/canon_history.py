"""Read-only history for canon sync runs.

A run records its commit and its page counters; each per-path row records what
the run did to that path (``last_action``). This module turns both into lists a
route can render: the tenant's runs, newest first, and the files one run touched.

Every function is a read: no writes, no flush, no commit. Every query is scoped
by ``tenant_id``, which is the only thing that makes a run id or a source id
safe to accept from a caller.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.palace import SyncRun, SyncSourceFile
from app.services.canon import (
    CANON_RUN_FILE_DEFAULT,
    CANON_RUN_LIST_DEFAULT,
    clamp_run_file_limit,
    clamp_run_list_limit,
)


async def list_canon_runs(
    db: AsyncSession,
    *,
    tenant_id: str,
    source_id: uuid.UUID | None = None,
    limit: int = CANON_RUN_LIST_DEFAULT,
) -> list[SyncRun]:
    """The tenant's canon runs, newest first.

    ``started_at`` orders the list and ``id`` breaks ties, so two runs that
    started in the same instant still have one stable order across calls.
    """
    query = select(SyncRun).where(SyncRun.tenant_id == tenant_id)
    if source_id is not None:
        query = query.where(SyncRun.sync_source_id == source_id)
    query = (
        query.order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
        .limit(clamp_run_list_limit(limit))
    )
    rows = await db.scalars(query)
    return list(rows)


async def latest_canon_run(
    db: AsyncSession,
    *,
    tenant_id: str,
    source_id: uuid.UUID,
) -> SyncRun | None:
    """The most recent run for one binding, or ``None`` if it never ran."""
    return await db.scalar(
        select(SyncRun)
        .where(SyncRun.tenant_id == tenant_id)
        .where(SyncRun.sync_source_id == source_id)
        .order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
        .limit(1)
    )


async def get_canon_run(
    db: AsyncSession,
    *,
    tenant_id: str,
    run_id: uuid.UUID,
) -> SyncRun | None:
    """One run by id, or ``None`` when this tenant does not own that id."""
    return await db.scalar(
        select(SyncRun)
        .where(SyncRun.tenant_id == tenant_id)
        .where(SyncRun.id == run_id)
        .limit(1)
    )


async def list_canon_run_files(
    db: AsyncSession,
    *,
    tenant_id: str,
    run_id: uuid.UUID,
    limit: int = CANON_RUN_FILE_DEFAULT,
) -> list[SyncSourceFile]:
    """The per-path rows one run last saw, in path order.

    A row belongs to a run through ``last_seen_run_id``: a path the run created,
    updated, conflicted on, skipped, archived or failed to parse.
    """
    rows = await db.scalars(
        select(SyncSourceFile)
        .where(SyncSourceFile.tenant_id == tenant_id)
        .where(SyncSourceFile.last_seen_run_id == run_id)
        .order_by(SyncSourceFile.relative_path.asc())
        .limit(clamp_run_file_limit(limit))
    )
    return list(rows)
