"""Explicit, audited hard-deletion paths for tenant-owned database data."""

from __future__ import annotations

import asyncio
import uuid
import hashlib
import logging
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from arq.constants import abort_jobs_ss, in_progress_key_prefix, job_key_prefix, result_key_prefix, retry_key_prefix
from arq.jobs import DeserializationError, deserialize_job
from arq.utils import timestamp_ms
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import Base
from app.models.data_lifecycle import DataLifecycleAuditEvent, TenantErasureState
from app.models.item import Item
from app.services.bundle import acquire_item_artifact_lock, item_artifact_tombstone
from app.config import settings
from app.workers.queues import (
    DEFAULT_WORKER_QUEUE,
    MEDIA_WORKER_QUEUE,
    PALACE_WORKER_QUEUE,
    close_tenant_queue,
    reopen_tenant_queue,
)

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
    payload: object,
    *,
    tenant_id: str,
    tenant_identifiers: set[str],
) -> bool:
    if isinstance(payload, dict):
        if payload.get("tenant_id") == tenant_id:
            return True
        for key, value in payload.items():
            if key == "tenant_id":
                continue
            if key.endswith("_id") and isinstance(value, str) and value in tenant_identifiers:
                return True
            if key.endswith("_ids") and isinstance(value, (list, tuple)):
                if any(isinstance(entry, str) and entry in tenant_identifiers for entry in value):
                    return True
            if isinstance(value, dict) and _payload_references_tenant(
                value,
                tenant_id=tenant_id,
                tenant_identifiers=tenant_identifiers,
            ):
                return True
    return False


async def _find_tenant_arq_jobs(
    arq_pool: Any,
    *,
    tenant_id: str,
    tenant_identifiers: set[str],
) -> list[tuple[str, object]]:
    """Validate queue payloads and return only typed tenant references."""
    deserializer = getattr(arq_pool, "job_deserializer", None)
    matching_jobs: list[tuple[str, object]] = []
    async for raw_key in arq_pool.scan_iter(match=f"{job_key_prefix}*"):
        key = raw_key.decode() if isinstance(raw_key, bytes) else str(raw_key)
        payload = await arq_pool.get(raw_key)
        if not payload:
            continue
        try:
            definition = deserialize_job(payload, deserializer=deserializer)
        except (DeserializationError, ValueError, TypeError) as exc:
            raise RuntimeError(
                f"Could not inspect ARQ payload during tenant erasure: {key}"
            ) from exc
        if _payload_references_tenant(
            {"args": definition.args, "kwargs": definition.kwargs},
            tenant_id=tenant_id,
            tenant_identifiers=tenant_identifiers,
        ):
            matching_jobs.append((key.removeprefix(job_key_prefix), raw_key))
    return matching_jobs


async def _purge_tenant_arq_jobs(
    arq_pool: Any,
    *,
    tenant_id: str,
    tenant_identifiers: set[str],
) -> int:
    """Abort queued/running tenant work and remove serialized job payloads."""
    queue_names = {
        DEFAULT_WORKER_QUEUE,
        MEDIA_WORKER_QUEUE,
        PALACE_WORKER_QUEUE,
        getattr(arq_pool, "default_queue_name", DEFAULT_WORKER_QUEUE),
    }
    matching_jobs = await _find_tenant_arq_jobs(
        arq_pool,
        tenant_id=tenant_id,
        tenant_identifiers=tenant_identifiers,
    )

    for job_id, _raw_key in matching_jobs:
        await arq_pool.zadd(abort_jobs_ss, {job_id: timestamp_ms()})
        for queue_name in queue_names:
            await arq_pool.zrem(queue_name, job_id)

    # ARQ workers poll the abort set and remove their in-progress key after the
    # task accepts cancellation. Do not claim erasure while tenant code is still
    # running. A timeout fails closed and leaves the permanent marker uncommitted.
    if matching_jobs:
        async with asyncio.timeout(30):
            while any([
                await arq_pool.exists(f"{in_progress_key_prefix}{job_id}")
                for job_id, _raw_key in matching_jobs
            ]):
                await asyncio.sleep(0.1)

    for job_id, raw_key in matching_jobs:
        await arq_pool.delete(
            raw_key,
            f"{result_key_prefix}{job_id}",
            f"{retry_key_prefix}{job_id}",
        )
    return len(matching_jobs)


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
    await close_tenant_queue(arq_pool, tenant_id)
    try:
        tables = tuple(_tenant_tables())
        # Validate every queue payload before the transaction becomes
        # irreversible. Do not remove jobs yet: a database rollback must leave
        # the active tenant's queued work intact.
        tenant_identifiers = await _tenant_identifiers(db, tenant_id)
        tenant_arq_jobs = await _find_tenant_arq_jobs(
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
                    "arq_jobs_identified": len(tenant_arq_jobs),
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
        await reopen_tenant_queue(arq_pool, tenant_id)
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
    artifact_lock = acquire_item_artifact_lock(tenant_id, item_id)
    try:
        tombstone = item_artifact_tombstone(tenant_id, item_id)
        tombstone.parent.mkdir(parents=True, exist_ok=True)
        tombstone_created = not tombstone.exists()
        tombstone.touch(exist_ok=True)
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
                if tombstone_created:
                    tombstone.unlink(missing_ok=True)
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
            if tombstone_created:
                tombstone.unlink(missing_ok=True)
            raise
        _purge_staged_paths(staged_paths)
        quarantine_directories = {staged.parent for _original, staged in staged_paths}
        for quarantine in quarantine_directories:
            try:
                quarantine.rmdir()
            except OSError:
                logger.critical(
                    "Could not remove item deletion quarantine %s",
                    quarantine,
                    exc_info=True,
                )
        return True
    finally:
        artifact_lock.close()
