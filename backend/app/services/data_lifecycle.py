"""Explicit, audited hard-deletion paths for tenant-owned database data."""

from __future__ import annotations

import uuid
import hashlib
import logging
import shutil
from collections.abc import Iterable
from pathlib import Path

from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import Base
from app.models.data_lifecycle import DataLifecycleAuditEvent
from app.models.item import Item
from app.config import settings

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
    if not artifact_directory.exists() and not artifact_directory.is_symlink():
        return []
    quarantine = artifact_directory.with_name(
        f".{artifact_directory.name}.erasing-{uuid.uuid4()}"
    )
    artifact_directory.replace(quarantine)
    return [(artifact_directory, quarantine)]


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
        quarantine.rmdir()
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
    tenant_id: str,
    actor_id: str,
    dry_run: bool,
) -> dict[str, int]:
    """Count or delete every ORM-declared tenant row in one transaction."""
    counts = await tenant_row_counts(db, tenant_id)
    staged_paths = [] if dry_run else _stage_tenant_artifacts(tenant_id)
    try:
        if not dry_run:
            for table in _tenant_tables():
                await db.execute(delete(table).where(table.c.tenant_id == tenant_id))
        await db.execute(
            insert(DataLifecycleAuditEvent).values(
                subject_tenant_id=tenant_id,
                subject_item_id=None,
                action="tenant_erasure_dry_run" if dry_run else "tenant_erasure",
                actor_id=actor_id,
                details={"row_counts": counts, "artifact_paths_staged": len(staged_paths)},
            )
        )
        await db.commit()
    except Exception:
        await db.rollback()
        _restore_staged_paths(staged_paths)
        raise
    _purge_staged_paths(staged_paths)
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
