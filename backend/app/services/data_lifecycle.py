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

from app.database import Base, system_async_session
from app.models.data_lifecycle import DataLifecycleAuditEvent, TenantErasureState
from app.models.item import Item
from app.models.job import Job
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
            if key == "args" and _positional_identifiers(value) & tenant_identifiers:
                return True
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


def _payload_references_identifiers(
    payload: object,
    identifiers: set[str],
) -> bool:
    """Match typed durable identifiers without matching every job for a tenant."""
    if not isinstance(payload, dict):
        return False
    for key, value in payload.items():
        if key == "args" and _positional_identifiers(value) & identifiers:
            return True
        if key.endswith("_id") and key != "tenant_id":
            if isinstance(value, str) and value in identifiers:
                return True
        if key.endswith("_ids") and isinstance(value, (list, tuple)):
            if any(isinstance(entry, str) and entry in identifiers for entry in value):
                return True
        if isinstance(value, dict) and _payload_references_identifiers(value, identifiers):
            return True
    return False


def _positional_identifiers(value: object) -> set[str]:
    """Extract only UUID-shaped legacy positional IDs, never free-form text."""
    identifiers: set[str] = set()
    if isinstance(value, (list, tuple)):
        for entry in value:
            identifiers.update(_positional_identifiers(entry))
    elif isinstance(value, dict):
        identifiers.update(_typed_payload_identifiers(value))
    elif isinstance(value, str):
        try:
            identifiers.add(str(uuid.UUID(value)))
        except ValueError:
            pass
    return identifiers


def _typed_payload_identifiers(payload: object) -> set[str]:
    """Collect bounded ID candidates from typed payload fields and legacy args."""
    identifiers: set[str] = set()
    if not isinstance(payload, dict):
        return identifiers
    for key, value in payload.items():
        if key == "args":
            identifiers.update(_positional_identifiers(value))
        elif key.endswith("_id") and key != "tenant_id" and isinstance(value, str):
            identifiers.add(value)
        elif key.endswith("_ids") and isinstance(value, (list, tuple)):
            identifiers.update(str(entry) for entry in value if isinstance(entry, str))
        elif isinstance(value, dict):
            identifiers.update(_typed_payload_identifiers(value))
    return identifiers


async def _queued_identifier_candidates(arq_pool: Any) -> set[str]:
    """Validate queued payloads and collect only their possible durable IDs."""
    deserializer = getattr(arq_pool, "job_deserializer", None)
    candidates: set[str] = set()
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
        candidates.update(
            _typed_payload_identifiers(
                {"args": definition.args, "kwargs": definition.kwargs}
            )
        )
    return candidates


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


async def _find_identifier_arq_jobs(
    arq_pool: Any,
    *,
    identifiers: set[str],
) -> list[tuple[str, object]]:
    """Validate queue payloads and match only the supplied durable IDs."""
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
                f"Could not inspect ARQ payload during item erasure: {key}"
            ) from exc
        if _payload_references_identifiers(
            {"args": definition.args, "kwargs": definition.kwargs}, identifiers
        ):
            matching_jobs.append((key.removeprefix(job_key_prefix), raw_key))
    return matching_jobs


async def _purge_arq_jobs(
    arq_pool: Any,
    matching_jobs: list[tuple[str, object]],
) -> int:
    """Abort and remove an already validated set of ARQ jobs."""
    queue_names = {
        DEFAULT_WORKER_QUEUE,
        MEDIA_WORKER_QUEUE,
        PALACE_WORKER_QUEUE,
        getattr(arq_pool, "default_queue_name", DEFAULT_WORKER_QUEUE),
    }
    for job_id, _raw_key in matching_jobs:
        await arq_pool.zadd(abort_jobs_ss, {job_id: timestamp_ms()})
        for queue_name in queue_names:
            await arq_pool.zrem(queue_name, job_id)

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


async def _purge_tenant_arq_jobs(
    arq_pool: Any,
    *,
    tenant_id: str,
    tenant_identifiers: set[str],
) -> int:
    """Abort queued/running tenant work and remove serialized job payloads."""
    matching_jobs = await _find_tenant_arq_jobs(
        arq_pool,
        tenant_id=tenant_id,
        tenant_identifiers=tenant_identifiers,
    )

    return await _purge_arq_jobs(arq_pool, matching_jobs)


async def _audit_event_was_committed(audit_id: uuid.UUID) -> bool:
    """Resolve an uncertain commit through a new control-plane connection."""
    async with system_async_session() as verification_db:
        committed_id = await verification_db.scalar(
            select(DataLifecycleAuditEvent.id).where(
                DataLifecycleAuditEvent.id == audit_id
            )
        )
        return committed_id is not None


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


async def _tenant_identifiers(
    db: AsyncSession,
    tenant_id: str,
    candidates: set[str],
) -> set[str]:
    """Resolve only IDs present in queued payloads, in bounded query batches."""
    identifiers: set[str] = set()
    if not candidates:
        return identifiers
    candidate_ids: list[uuid.UUID] = []
    for candidate in candidates:
        try:
            candidate_ids.append(uuid.UUID(candidate))
        except ValueError:
            continue
    if not candidate_ids:
        return identifiers
    for table in _tenant_tables():
        if "id" not in table.c:
            continue
        for offset in range(0, len(candidate_ids), 500):
            batch = candidate_ids[offset : offset + 500]
            values = (
                await db.execute(
                    select(table.c.id).where(
                        table.c.tenant_id == tenant_id,
                        table.c.id.in_(batch),
                    )
                )
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
    audit_id = uuid.uuid4()
    commit_attempted = False
    await close_tenant_queue(arq_pool, tenant_id)
    try:
        tables = tuple(_tenant_tables())
        # Validate every queue payload before the transaction becomes
        # irreversible. Do not remove jobs yet: a database rollback must leave
        # the active tenant's queued work intact.
        queued_candidates = await _queued_identifier_candidates(arq_pool)
        tenant_identifiers = await _tenant_identifiers(
            db,
            tenant_id,
            queued_candidates,
        )
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
                id=audit_id,
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
        commit_attempted = True
        await db.commit()
    except Exception:
        await db.rollback()
        if not commit_attempted:
            _restore_staged_paths(staged_paths)
            artifact_tombstone = _tenant_artifact_directory(tenant_id)
            if artifact_tombstone.is_file() or artifact_tombstone.is_symlink():
                artifact_tombstone.unlink()
            await reopen_tenant_queue(arq_pool, tenant_id)
            raise
        try:
            committed = await _audit_event_was_committed(audit_id)
        except Exception:
            logger.critical(
                "Could not resolve tenant erasure commit outcome for %s; "
                "leaving artifacts quarantined and queue closed",
                tenant_id,
                exc_info=True,
            )
            raise
        if committed:
            logger.warning(
                "Tenant erasure commit acknowledgment failed for %s, but the "
                "committed audit event was confirmed",
                tenant_id,
            )
        else:
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
    arq_pool: Any,
    tenant_id: str,
    item_id: uuid.UUID,
    actor_id: str,
) -> bool:
    artifact_lock = acquire_item_artifact_lock(tenant_id, item_id)
    database_committed = False
    queue_closed = False
    try:
        await close_tenant_queue(arq_pool, tenant_id)
        queue_closed = True
        audit_id = uuid.uuid4()
        commit_attempted = False
        tombstone = item_artifact_tombstone(tenant_id, item_id)
        tombstone.parent.mkdir(parents=True, exist_ok=True)
        tombstone_created = not tombstone.exists()
        tombstone.touch(exist_ok=True)
        staged_paths = _stage_item_artifacts(tenant_id, item_id)
        try:
            job_ids = set(
                str(value)
                for value in (
                    await db.execute(
                        select(Job.id).where(
                            Job.item_id == item_id,
                            Job.tenant_id == tenant_id,
                        )
                    )
                ).scalars()
            )
            identifiers = {str(item_id), *job_ids}
            matching_arq_jobs = await _find_identifier_arq_jobs(
                arq_pool,
                identifiers=identifiers,
            )
            await db.execute(
                delete(Job).where(
                    Job.item_id == item_id,
                    Job.tenant_id == tenant_id,
                )
            )
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
                await reopen_tenant_queue(arq_pool, tenant_id)
                queue_closed = False
                return False
            await db.execute(
                insert(DataLifecycleAuditEvent).values(
                    id=audit_id,
                    subject_tenant_id=tenant_id,
                    subject_item_id=item_id,
                    action="item_hard_delete",
                    actor_id=actor_id,
                    details={"artifact_paths_staged": len(staged_paths)},
                )
            )
            commit_attempted = True
            await db.commit()
            database_committed = True
        except Exception:
            await db.rollback()
            if commit_attempted:
                try:
                    committed = await _audit_event_was_committed(audit_id)
                except Exception:
                    logger.critical(
                        "Could not resolve item erasure commit outcome for %s; "
                        "leaving artifacts quarantined",
                        item_id,
                        exc_info=True,
                    )
                    raise
                if committed:
                    database_committed = True
                    logger.warning(
                        "Item erasure commit acknowledgment failed for %s, but "
                        "the committed audit event was confirmed",
                        item_id,
                    )
                else:
                    _restore_staged_paths(staged_paths)
                    if tombstone_created:
                        tombstone.unlink(missing_ok=True)
                    raise
            else:
                _restore_staged_paths(staged_paths)
                if tombstone_created:
                    tombstone.unlink(missing_ok=True)
                raise
        # The tenant enqueue barrier prevents new work from racing this scan.
        # Purge only after the database commit so a rollback cannot lose work.
        await _purge_arq_jobs(arq_pool, matching_arq_jobs)
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
        await reopen_tenant_queue(arq_pool, tenant_id)
        queue_closed = False
        return True
    except Exception:
        if queue_closed and not database_committed:
            await reopen_tenant_queue(arq_pool, tenant_id)
        raise
    finally:
        artifact_lock.close()
