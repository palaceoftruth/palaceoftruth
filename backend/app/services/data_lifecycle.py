"""Explicit, audited hard-deletion paths for tenant-owned database data."""

from __future__ import annotations

import uuid
import hashlib
import logging
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from arq.constants import abort_jobs_ss, job_key_prefix, result_key_prefix, retry_key_prefix
from arq.jobs import DeserializationError, deserialize_job
from arq.utils import timestamp_ms
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import Base
from app.models.data_lifecycle import DataLifecycleAuditEvent, TenantErasureState
from app.models.item import Item
from app.config import settings
from app.workers.queues import DEFAULT_WORKER_QUEUE, MEDIA_WORKER_QUEUE, PALACE_WORKER_QUEUE

logger = logging.getLogger(__name__)


def _tenant_artifact_directory(tenant_id: str) -> Path:
    root = Path(settings.upload_artifact_dir).expanduser().resolve()
    tenant_hash = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()
    return root / tenant_hash


def _purge_staged_path(path: Path) -> None:
    """Remove a path that is already outside the active artifact namespace."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _restore_staged_paths(staged_paths: list[tuple[Path, Path]]) -> None:
    """Restore filesystem artifacts after a database transaction failure."""
    quarantine_directories = {staged.parent for _original, staged in staged_paths}
    for original, staged in reversed(staged_paths):
        if staged.exists() or staged.is_symlink():
            if original.exists() or original.is_symlink():
                _purge_staged_path(original)
            staged.replace(original)
    for quarantine in quarantine_directories:
        if quarantine.name.startswith(".erasing-"):
            quarantine.rmdir()


def _purge_staged_paths(staged_paths: list[tuple[Path, Path]]) -> None:
    for _original, staged in staged_paths:
        try:
            _purge_staged_path(staged)
        except OSError:
            # The artifact is no longer addressable through the active tenant path.
            # Keep the quarantine path for an operator to remove safely.
            logger.critical("Could not purge quarantined deletion artifact %s", staged, exc_info=True)


def _stage_tenant_artifacts(tenant_id: str) -> list[tuple[Path, Path]]:
    artifact_directory = _tenant_artifact_directory(tenant_id)
    artifact_directory.parent.mkdir(parents=True, exist_ok=True)
    staged_paths: list[tuple[Path, Path]] = []
    if artifact_directory.exists() or artifact_directory.is_symlink():
        quarantine = artifact_directory.with_name(
            f".{artifact_directory.name}.erasing-{uuid.uuid4()}"
        )
        artifact_directory.replace(quarantine)
        staged_paths.append((artifact_directory, quarantine))
    # Keep a file at the former directory path. Late upload work can no longer
    # recreate the tenant directory after the database erasure marker commits.
    try:
        artifact_directory.touch(exist_ok=False)
    except OSError:
        _restore_staged_paths(staged_paths)
        raise
    return staged_paths


def _stage_item_artifacts(tenant_id: str, item_id: uuid.UUID) -> list[tuple[Path, Path]]:
    artifact_directory = _tenant_artifact_directory(tenant_id)
    if not artifact_directory.is_dir():
        return []
    item_prefix = str(item_id)
    candidates = [
        path
        for path in artifact_directory.iterdir()
        if path.name == item_prefix or path.name.startswith(f"{item_prefix}.")
    ]
    if not candidates:
        return []
    quarantine = artifact_directory / f".erasing-{item_prefix}-{uuid.uuid4()}"
    quarantine.mkdir()
    staged_paths: list[tuple[Path, Path]] = []
    try:
        for original in candidates:
            staged = quarantine / original.name
            original.replace(staged)
            staged_paths.append((original, staged))
    except OSError:
        _restore_staged_paths(staged_paths)
        raise
    # Purging the final staged child also removes the quarantine directory below.
    return staged_paths


def _tenant_tables() -> Iterable:
    """Return child tables before parents so restrictive foreign keys stay safe."""
    return (
        table
        for table in reversed(Base.metadata.sorted_tables)
        if "tenant_id" in table.c and table.name != DataLifecycleAuditEvent.__tablename__
    )


def _payload_references_tenant(
    value: object,
    *,
    tenant_id: str,
    tenant_identifiers: set[str],
) -> bool:
    if isinstance(value, dict):
        if value.get("tenant_id") == tenant_id:
            return True
        return any(
            _payload_references_tenant(
                nested,
                tenant_id=tenant_id,
                tenant_identifiers=tenant_identifiers,
            )
            for nested in value.values()
        )
    if isinstance(value, (list, tuple)):
        return any(
            _payload_references_tenant(
                nested,
                tenant_id=tenant_id,
                tenant_identifiers=tenant_identifiers,
            )
            for nested in value
        )
    return isinstance(value, str) and value in tenant_identifiers


async def _purge_tenant_arq_jobs(
    arq_pool: Any,
    *,
    tenant_id: str,
    tenant_identifiers: set[str],
) -> int:
    """Abort queued/running tenant work and remove serialized job payloads."""
    deserializer = getattr(arq_pool, "job_deserializer", None)
    queue_names = {
        DEFAULT_WORKER_QUEUE,
        MEDIA_WORKER_QUEUE,
        PALACE_WORKER_QUEUE,
        getattr(arq_pool, "default_queue_name", DEFAULT_WORKER_QUEUE),
    }
    purged = 0
    async for raw_key in arq_pool.scan_iter(match=f"{job_key_prefix}*"):
        key = raw_key.decode() if isinstance(raw_key, bytes) else str(raw_key)
        payload = await arq_pool.get(raw_key)
        if not payload:
            continue
        try:
            definition = deserialize_job(payload, deserializer=deserializer)
        except DeserializationError:
            logger.warning("Could not inspect ARQ payload during tenant erasure: %s", key)
            continue
        if not _payload_references_tenant(
            {"args": definition.args, "kwargs": definition.kwargs},
            tenant_id=tenant_id,
            tenant_identifiers=tenant_identifiers,
        ):
            continue
        job_id = key.removeprefix(job_key_prefix)
        await arq_pool.zadd(abort_jobs_ss, {job_id: timestamp_ms()})
        for queue_name in queue_names:
            await arq_pool.zrem(queue_name, job_id)
        await arq_pool.delete(
            raw_key,
            f"{result_key_prefix}{job_id}",
            f"{retry_key_prefix}{job_id}",
        )
        purged += 1
    return purged


async def _finalize_committed_tenant_erasure(
    arq_pool: Any,
    *,
    tenant_id: str,
    tenant_identifiers: set[str],
    staged_paths: list[tuple[Path, Path]],
) -> None:
    """Finish irreversible external cleanup after the database commit."""
    try:
        await _purge_tenant_arq_jobs(
            arq_pool,
            tenant_id=tenant_id,
            tenant_identifiers=tenant_identifiers,
        )
    finally:
        # The database erasure is already committed. Quarantined tenant files
        # must not survive because the final best-effort Redis scan failed.
        _purge_staged_paths(staged_paths)


async def _tenant_identifiers(db: AsyncSession, tenant_id: str) -> set[str]:
    identifiers: set[str] = set()
    for table in _tenant_tables():
        if "id" not in table.c:
            continue
        values = (
            await db.execute(select(table.c.id).where(table.c.tenant_id == tenant_id))
        ).scalars()
        identifiers.update(str(value) for value in values)
    return identifiers


async def tenant_row_counts(db: AsyncSession, tenant_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in _tenant_tables():
        count = await db.scalar(
            select(func.count()).select_from(table).where(table.c.tenant_id == tenant_id)
        )
        if count:
            counts[table.name] = int(count)
    return counts


async def erase_tenant_data(
    db: AsyncSession,
    *,
    arq_pool: Any,
    tenant_id: str,
    actor_id: str,
    dry_run: bool,
) -> dict[str, int]:
    """Count or delete every ORM-declared tenant row in one transaction."""
    if dry_run:
        counts = await tenant_row_counts(db, tenant_id)
        await db.execute(
            insert(DataLifecycleAuditEvent).values(
                subject_tenant_id=tenant_id,
                subject_item_id=None,
                action="tenant_erasure_dry_run",
                actor_id=actor_id,
                details={"row_counts": counts, "artifact_paths_staged": 0},
            )
        )
        await db.commit()
        return counts

    staged_paths: list[tuple[Path, Path]] = []
    try:
        tables = tuple(_tenant_tables())
        # Inspect and purge durable queue references before taking locks that
        # block writes for every tenant. A slow or unavailable Redis service
        # must not extend the database maintenance window.
        tenant_identifiers = await _tenant_identifiers(db, tenant_id)
        purged_arq_jobs = await _purge_tenant_arq_jobs(
            arq_pool,
            tenant_id=tenant_id,
            tenant_identifiers=tenant_identifiers,
        )

        # Erasure is deliberate maintenance and can exceed normal request query
        # limits. Keep the marker, row deletion, and audit record atomic: if a
        # lock or delete fails, the marker rolls back and the tenant is not left
        # frozen around still-readable data.
        await db.execute(text("SET LOCAL statement_timeout = 0"))
        await db.execute(text("SET LOCAL idle_in_transaction_session_timeout = 0"))
        quoted_tables = ", ".join(f'"{table.name}"' for table in tables)
        await db.execute(text(f"LOCK TABLE {quoted_tables} IN SHARE ROW EXCLUSIVE MODE"))
        await db.execute(
            text(
                """
                INSERT INTO tenant_erasure_states (subject_tenant_id)
                VALUES (:tenant_id)
                ON CONFLICT (subject_tenant_id) DO NOTHING
                """
            ),
            {"tenant_id": tenant_id},
        )
        counts = await tenant_row_counts(db, tenant_id)
        staged_paths = _stage_tenant_artifacts(tenant_id)
        for table in tables:
            await db.execute(delete(table).where(table.c.tenant_id == tenant_id))
        await db.execute(
            insert(DataLifecycleAuditEvent).values(
                subject_tenant_id=tenant_id,
                subject_item_id=None,
                action="tenant_erasure",
                actor_id=actor_id,
                details={
                    "row_counts": counts,
                    "artifact_paths_staged": len(staged_paths),
                    "arq_jobs_purged": purged_arq_jobs,
                },
            )
        )
        await db.execute(
            update(TenantErasureState)
            .where(TenantErasureState.subject_tenant_id == tenant_id)
            .values(completed_at=func.now())
        )
        await db.commit()
    except Exception:
        await db.rollback()
        _restore_staged_paths(staged_paths)
        artifact_tombstone = _tenant_artifact_directory(tenant_id)
        if artifact_tombstone.is_file() or artifact_tombstone.is_symlink():
            artifact_tombstone.unlink()
        raise
    # Catch an enqueue that was already between its database commit and Redis
    # write when the table locks were acquired.
    await _finalize_committed_tenant_erasure(
        arq_pool,
        tenant_id=tenant_id,
        tenant_identifiers=tenant_identifiers,
        staged_paths=staged_paths,
    )
    return counts


async def hard_delete_item(
    db: AsyncSession,
    *,
    tenant_id: str,
    item_id: uuid.UUID,
    actor_id: str,
) -> bool:
    staged_paths = _stage_item_artifacts(tenant_id, item_id)
    try:
        result = await db.execute(
            delete(Item)
            .where(Item.id == item_id, Item.tenant_id == tenant_id)
            .returning(Item.id)
        )
        deleted = result.scalar_one_or_none()
        if deleted is None:
            await db.rollback()
            _restore_staged_paths(staged_paths)
            return False
        await db.execute(
            insert(DataLifecycleAuditEvent).values(
                subject_tenant_id=tenant_id,
                subject_item_id=item_id,
                action="item_hard_delete",
                actor_id=actor_id,
                details={"artifact_paths_staged": len(staged_paths)},
            )
        )
        await db.commit()
    except Exception:
        await db.rollback()
        _restore_staged_paths(staged_paths)
        raise
    _purge_staged_paths(staged_paths)
    quarantine_directories = {staged.parent for _original, staged in staged_paths}
    for quarantine in quarantine_directories:
        try:
            quarantine.rmdir()
        except OSError:
            logger.critical("Could not remove item deletion quarantine %s", quarantine, exc_info=True)
    return True
