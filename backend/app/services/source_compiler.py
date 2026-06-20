from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy import Select, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.item import Item
from app.models.palace import SourceChunk, SourceRecord


ACTIVE_SOURCE_STATUSES = {"active", "stale", "superseded"}


@dataclass(frozen=True)
class SourceChunkProjection:
    chunk_index: int
    chunk_text: str
    chunk_digest: str
    span: dict[str, Any] = field(default_factory=dict)
    token_count: int | None = None


@dataclass(frozen=True)
class SourceRecordProjection:
    tenant_id: str
    item_id: uuid.UUID
    source_kind: str
    source_uri: str | None
    source_version: str
    content_hash: str
    status: str
    failure_reason: str | None
    metadata: dict[str, Any]
    chunks: tuple[SourceChunkProjection, ...]


@dataclass(frozen=True)
class SourceBackfillReport:
    tenant_id: str
    dry_run: bool
    items_seen: int
    records_planned: int
    chunks_planned: int
    records_upserted: int
    chunks_upserted: int
    skipped_items: int


@dataclass(frozen=True)
class SourceChunkSummary:
    id: uuid.UUID
    chunk_index: int
    chunk_digest: str
    token_count: int | None
    preview: str


@dataclass(frozen=True)
class SourceRecordSummary:
    id: uuid.UUID
    item_id: uuid.UUID
    source_kind: str
    source_uri: str | None
    source_version: str
    content_hash: str
    status: str
    failure_reason: str | None
    metadata: dict[str, Any]
    chunk_count: int
    chunks: tuple[SourceChunkSummary, ...]


@dataclass(frozen=True)
class ItemSourceSummary:
    tenant_id: str
    item_id: uuid.UUID
    source_records: tuple[SourceRecordSummary, ...]


def _stable_digest(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_metadata(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _source_kind_for_item(item: Item) -> str:
    metadata = _normalize_metadata(getattr(item, "metadata_", None))
    if isinstance(metadata.get("memory_entry"), dict):
        return "memory_entry"
    source_type = (getattr(item, "source_type", "") or "").strip().lower()
    return source_type or "capture"


def _source_uri_for_item(item: Item) -> str | None:
    source_url = getattr(item, "source_url", None)
    if isinstance(source_url, str) and source_url.strip():
        return source_url.strip()
    metadata = _normalize_metadata(getattr(item, "metadata_", None))
    memory_entry = metadata.get("memory_entry")
    if isinstance(memory_entry, dict):
        entry_id = memory_entry.get("id") or item.id
        return f"memory://{item.tenant_id}/{entry_id}"
    return None


def _status_for_item(item: Item) -> str:
    if getattr(item, "deleted_at", None) is not None or getattr(item, "status", None) == "deleted":
        return "deleted"
    if getattr(item, "status", None) == "failed":
        return "failed"
    return "active"


def _failure_reason_for_item(item: Item) -> str | None:
    if _status_for_item(item) != "failed":
        return None
    metadata = _normalize_metadata(getattr(item, "metadata_", None))
    reason = metadata.get("failure_reason") or metadata.get("error") or metadata.get("last_error")
    return str(reason) if reason else "item processing failed"


def _chunk_text_and_span(raw_chunk: Any, fallback_index: int) -> tuple[int, str, dict[str, Any], int | None] | None:
    if isinstance(raw_chunk, str):
        text_value = raw_chunk.strip()
        return (fallback_index, text_value, {}, None) if text_value else None
    if not isinstance(raw_chunk, dict):
        return None
    text_value = raw_chunk.get("text") or raw_chunk.get("chunk_text") or raw_chunk.get("content")
    if not isinstance(text_value, str) or not text_value.strip():
        return None
    raw_index = raw_chunk.get("index", raw_chunk.get("chunk_index", fallback_index))
    try:
        chunk_index = int(raw_index)
    except (TypeError, ValueError):
        chunk_index = fallback_index
    span = raw_chunk.get("span")
    token_count = raw_chunk.get("token_count")
    try:
        parsed_token_count = int(token_count) if token_count is not None else None
    except (TypeError, ValueError):
        parsed_token_count = None
    return chunk_index, text_value.strip(), span if isinstance(span, dict) else {}, parsed_token_count


def project_item_source(item: Item) -> SourceRecordProjection:
    metadata = _normalize_metadata(getattr(item, "metadata_", None))
    content_hash = getattr(item, "content_hash", None) or _stable_digest(
        {
            "raw_content": getattr(item, "raw_content", None),
            "source_type": getattr(item, "source_type", None),
            "source_url": getattr(item, "source_url", None),
            "metadata": metadata,
        }
    )
    chunks: list[SourceChunkProjection] = []
    if _status_for_item(item) in ACTIVE_SOURCE_STATUSES:
        raw_chunks = getattr(item, "content_chunks", None)
        if isinstance(raw_chunks, list):
            for fallback_index, raw_chunk in enumerate(raw_chunks):
                parsed = _chunk_text_and_span(raw_chunk, fallback_index)
                if parsed is None:
                    continue
                chunk_index, chunk_text, span, token_count = parsed
                chunks.append(
                    SourceChunkProjection(
                        chunk_index=chunk_index,
                        chunk_text=chunk_text,
                        chunk_digest=_stable_digest({"index": chunk_index, "text": chunk_text, "span": span}),
                        span=span,
                        token_count=token_count,
                    )
                )
    source_version = _stable_digest(
        {
            "content_hash": content_hash,
            "source_kind": _source_kind_for_item(item),
            "source_uri": _source_uri_for_item(item),
            "status": _status_for_item(item),
            "chunks": [(chunk.chunk_index, chunk.chunk_digest) for chunk in chunks],
        }
    )
    return SourceRecordProjection(
        tenant_id=item.tenant_id,
        item_id=item.id,
        source_kind=_source_kind_for_item(item),
        source_uri=_source_uri_for_item(item),
        source_version=source_version,
        content_hash=content_hash,
        status=_status_for_item(item),
        failure_reason=_failure_reason_for_item(item),
        metadata={
            "item_status": getattr(item, "status", None),
            "item_source_type": getattr(item, "source_type", None),
            "item_deleted": _status_for_item(item) == "deleted",
        },
        chunks=tuple(chunks),
    )


def plan_source_backfill(items: Iterable[Item], *, tenant_id: str, dry_run: bool) -> tuple[SourceBackfillReport, tuple[SourceRecordProjection, ...]]:
    item_rows = tuple(items)
    projections = tuple(project_item_source(item) for item in item_rows if item.tenant_id == tenant_id)
    skipped = sum(1 for item in item_rows if item.tenant_id != tenant_id)
    chunk_count = sum(len(projection.chunks) for projection in projections)
    return (
        SourceBackfillReport(
            tenant_id=tenant_id,
            dry_run=dry_run,
            items_seen=len(projections),
            records_planned=len(projections),
            chunks_planned=chunk_count,
            records_upserted=0 if dry_run else len(projections),
            chunks_upserted=0 if dry_run else chunk_count,
            skipped_items=skipped,
        ),
        projections,
    )


def _backfill_item_query(tenant_id: str, *, item_ids: Iterable[uuid.UUID] | None, limit: int) -> Select[tuple[Item]]:
    statement = select(Item).where(Item.tenant_id == tenant_id).order_by(Item.created_at.asc()).limit(limit)
    if item_ids is not None:
        statement = statement.where(Item.id.in_(tuple(item_ids)))
    return statement


async def backfill_source_records_and_chunks(
    db: AsyncSession,
    *,
    tenant_id: str,
    item_ids: Iterable[uuid.UUID] | None = None,
    limit: int = 500,
    dry_run: bool = True,
) -> SourceBackfillReport:
    rows = (await db.scalars(_backfill_item_query(tenant_id, item_ids=item_ids, limit=limit))).all()
    report, projections = plan_source_backfill(rows, tenant_id=tenant_id, dry_run=dry_run)
    if dry_run:
        return report

    for projection in projections:
        record_id = await _upsert_source_record(db, projection)
        await _mark_prior_source_records_stale(db, projection=projection, active_record_id=record_id)
        for chunk in projection.chunks:
            await _upsert_source_chunk(db, projection=projection, source_record_id=record_id, chunk=chunk)
    await db.commit()
    return report


async def _upsert_source_record(db: AsyncSession, projection: SourceRecordProjection) -> uuid.UUID:
    stmt = (
        insert(SourceRecord.__table__)
        .values(
            tenant_id=projection.tenant_id,
            item_id=projection.item_id,
            source_kind=projection.source_kind,
            source_uri=projection.source_uri,
            source_version=projection.source_version,
            content_hash=projection.content_hash,
            status=projection.status,
            failure_reason=projection.failure_reason,
            metadata=projection.metadata,
        )
        .on_conflict_do_update(
            constraint="uq_source_records_tenant_item_version",
            set_={
                "source_kind": projection.source_kind,
                "source_uri": projection.source_uri,
                "content_hash": projection.content_hash,
                "status": projection.status,
                "failure_reason": projection.failure_reason,
                "metadata": projection.metadata,
                "updated_at": func.now(),
            },
        )
        .returning(SourceRecord.__table__.c.id)
    )
    return await db.scalar(stmt)


async def _mark_prior_source_records_stale(
    db: AsyncSession,
    *,
    projection: SourceRecordProjection,
    active_record_id: uuid.UUID,
) -> None:
    await db.execute(
        update(SourceRecord)
        .where(SourceRecord.tenant_id == projection.tenant_id)
        .where(SourceRecord.item_id == projection.item_id)
        .where(SourceRecord.id != active_record_id)
        .where(SourceRecord.status == "active")
        .values(status="stale", updated_at=func.now())
    )


async def _upsert_source_chunk(
    db: AsyncSession,
    *,
    projection: SourceRecordProjection,
    source_record_id: uuid.UUID,
    chunk: SourceChunkProjection,
) -> uuid.UUID:
    stmt = (
        insert(SourceChunk.__table__)
        .values(
            tenant_id=projection.tenant_id,
            source_record_id=source_record_id,
            item_id=projection.item_id,
            chunk_index=chunk.chunk_index,
            chunk_text=chunk.chunk_text,
            chunk_digest=chunk.chunk_digest,
            span=chunk.span,
            token_count=chunk.token_count,
        )
        .on_conflict_do_update(
            constraint="uq_source_chunks_tenant_record_index",
            set_={
                "chunk_text": chunk.chunk_text,
                "chunk_digest": chunk.chunk_digest,
                "span": chunk.span,
                "token_count": chunk.token_count,
            },
        )
        .returning(SourceChunk.__table__.c.id)
    )
    return await db.scalar(stmt)


async def get_item_source_summary(db: AsyncSession, *, tenant_id: str, item_id: uuid.UUID) -> ItemSourceSummary | None:
    item_exists = await db.scalar(select(Item.id).where(Item.tenant_id == tenant_id, Item.id == item_id))
    if item_exists is None:
        return None

    records = (
        await db.scalars(
            select(SourceRecord)
            .where(SourceRecord.tenant_id == tenant_id, SourceRecord.item_id == item_id)
            .order_by(SourceRecord.created_at.desc())
        )
    ).all()
    summaries: list[SourceRecordSummary] = []
    for record in records:
        chunks = (
            await db.scalars(
                select(SourceChunk)
                .where(SourceChunk.tenant_id == tenant_id, SourceChunk.source_record_id == record.id)
                .order_by(SourceChunk.chunk_index.asc())
            )
        ).all()
        summaries.append(
            SourceRecordSummary(
                id=record.id,
                item_id=record.item_id,
                source_kind=record.source_kind,
                source_uri=record.source_uri,
                source_version=record.source_version,
                content_hash=record.content_hash,
                status=record.status,
                failure_reason=record.failure_reason,
                metadata=record.metadata_ or {},
                chunk_count=len(chunks),
                chunks=tuple(
                    SourceChunkSummary(
                        id=chunk.id,
                        chunk_index=chunk.chunk_index,
                        chunk_digest=chunk.chunk_digest,
                        token_count=chunk.token_count,
                        preview=chunk.chunk_text[:240],
                    )
                    for chunk in chunks
                ),
            )
        )
    return ItemSourceSummary(tenant_id=tenant_id, item_id=item_id, source_records=tuple(summaries))
