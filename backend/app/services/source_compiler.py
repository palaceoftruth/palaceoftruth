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
from app.models.palace import Claim, ClaimSource, SourceChunk, SourceRecord, TemporalFact


ACTIVE_SOURCE_STATUSES = {"active", "stale", "superseded"}
CLAIM_SUPPORT_SOURCE_STATUSES = {"active"}
SAFE_CLAIM_METADATA_KEYS = {
    "compiler",
    "reviewed_at",
    "reviewed_by",
    "review_role",
    "source_item_id",
    "task_id",
    "pr_url",
    "run_id",
    "source_url",
}
SAFE_SOURCE_SPAN_KEYS = {
    "chunk_index",
    "source_chunk_index",
    "source_chunk_id",
    "source_chunk_digest",
    "chunk_digest",
    "start",
    "end",
    "page",
    "section",
    "temporal_fact_id",
    "valid_from",
    "valid_to",
}


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
    source_records_marked_stale: int = 0
    claim_sources_marked_stale: int = 0
    claims_marked_stale: int = 0


@dataclass(frozen=True)
class ClaimProjection:
    tenant_id: str
    temporal_fact_id: uuid.UUID
    source_item_id: uuid.UUID
    claim_key: str
    claim_text: str
    claim_type: str
    confidence: float
    status: str
    support_role: str
    source_digest: str
    source_span: dict[str, Any]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ClaimBackfillReport:
    tenant_id: str
    dry_run: bool
    facts_seen: int
    claims_planned: int
    claim_sources_planned: int
    claims_upserted: int
    claim_sources_upserted: int
    unsupported_facts: int


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


@dataclass(frozen=True)
class ClaimSourceSupportSummary:
    id: uuid.UUID
    source_record_id: uuid.UUID
    source_chunk_id: uuid.UUID | None
    source_item_id: uuid.UUID
    source_record_status: str
    support_role: str
    status: str
    source_digest: str
    source_span: dict[str, Any]


@dataclass(frozen=True)
class ClaimSupportSummary:
    id: uuid.UUID
    claim_key: str
    claim_text: str
    claim_type: str
    confidence: float
    status: str
    support_state: str
    warning: str | None
    metadata: dict[str, Any]
    sources: tuple[ClaimSourceSupportSummary, ...]


@dataclass(frozen=True)
class ClaimSupportReport:
    tenant_id: str
    claims: tuple[ClaimSupportSummary, ...]


@dataclass(frozen=True)
class SourceInvalidationReport:
    tenant_id: str
    source_records_seen: int
    claim_sources_marked_stale: int
    claims_marked_stale: int


def _stable_digest(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_metadata(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_claim_metadata(value: Any) -> dict[str, Any]:
    metadata = _normalize_metadata(value)
    return {key: metadata[key] for key in SAFE_CLAIM_METADATA_KEYS if key in metadata and metadata[key] not in (None, [], {})}


def _safe_source_span(value: Any) -> dict[str, Any]:
    span = value if isinstance(value, dict) else {}
    return {key: span[key] for key in SAFE_SOURCE_SPAN_KEYS if key in span and span[key] not in (None, [], {})}


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


def _claim_text_for_fact(fact: TemporalFact) -> str:
    return " ".join((fact.subject.strip(), fact.predicate.strip(), fact.object_text.strip()))


def _claim_status_for_fact(fact: TemporalFact) -> str:
    metadata = _normalize_metadata(getattr(fact, "metadata_json", None))
    contradiction_sweep = metadata.get("contradiction_sweep")
    if fact.status == "active" and isinstance(contradiction_sweep, dict):
        conflict_count = contradiction_sweep.get("conflict_count")
        if isinstance(conflict_count, int) and conflict_count > 0:
            return "conflicted"
        conflicting_fact_ids = contradiction_sweep.get("conflicting_fact_ids")
        if isinstance(conflicting_fact_ids, list) and conflicting_fact_ids:
            return "conflicted"
    if fact.status == "active":
        return "active"
    if fact.status == "superseded":
        return "stale"
    return "draft"


def _support_role_for_fact(fact: TemporalFact) -> str:
    if fact.status == "superseded":
        return "derived_from"
    return "supports"


def project_claim_from_temporal_fact(fact: TemporalFact) -> ClaimProjection:
    metadata = _normalize_metadata(getattr(fact, "metadata_json", None))
    return ClaimProjection(
        tenant_id=fact.tenant_id,
        temporal_fact_id=fact.id,
        source_item_id=fact.source_item_id,
        claim_key=f"temporal_fact:{fact.fact_key}",
        claim_text=_claim_text_for_fact(fact),
        claim_type="fact",
        confidence=float(fact.confidence or 1.0),
        status=_claim_status_for_fact(fact),
        support_role=_support_role_for_fact(fact),
        source_digest=fact.source_fingerprint,
        source_span={
            "temporal_fact_id": str(fact.id),
            "valid_from": fact.valid_from.isoformat() if fact.valid_from else None,
            "valid_to": fact.valid_to.isoformat() if fact.valid_to else None,
        },
        metadata={
            "compiler": "temporal_fact_claim_backfill",
            "temporal_fact_id": str(fact.id),
            "temporal_fact_key": fact.fact_key,
            "temporal_fact_status": fact.status,
            "source_item_id": str(fact.source_item_id),
            "fact_metadata": metadata,
        },
    )


def plan_claim_backfill(
    facts: Iterable[TemporalFact],
    *,
    tenant_id: str,
    dry_run: bool,
    supported_source_item_ids: set[uuid.UUID],
) -> tuple[ClaimBackfillReport, tuple[ClaimProjection, ...]]:
    fact_rows = tuple(facts)
    tenant_facts = tuple(fact for fact in fact_rows if fact.tenant_id == tenant_id)
    projections = tuple(
        project_claim_from_temporal_fact(fact)
        for fact in tenant_facts
        if fact.source_item_id in supported_source_item_ids
    )
    unsupported = len(tenant_facts) - len(projections)
    return (
        ClaimBackfillReport(
            tenant_id=tenant_id,
            dry_run=dry_run,
            facts_seen=len(tenant_facts),
            claims_planned=len(projections),
            claim_sources_planned=len(projections),
            claims_upserted=0 if dry_run else len(projections),
            claim_sources_upserted=0 if dry_run else len(projections),
            unsupported_facts=unsupported,
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

    source_records_marked_stale = 0
    claim_sources_marked_stale = 0
    claims_marked_stale = 0
    for projection in projections:
        record_id = await _upsert_source_record(db, projection)
        stale_record_ids = await _mark_prior_source_records_stale(db, projection=projection, active_record_id=record_id)
        source_records_marked_stale += len(stale_record_ids)
        invalidation = await _invalidate_decision_claims_for_source_records(
            db,
            tenant_id=projection.tenant_id,
            source_record_ids=stale_record_ids,
        )
        claim_sources_marked_stale += invalidation.claim_sources_marked_stale
        claims_marked_stale += invalidation.claims_marked_stale
        for chunk in projection.chunks:
            await _upsert_source_chunk(db, projection=projection, source_record_id=record_id, chunk=chunk)
    await db.commit()
    return SourceBackfillReport(
        tenant_id=report.tenant_id,
        dry_run=report.dry_run,
        items_seen=report.items_seen,
        records_planned=report.records_planned,
        chunks_planned=report.chunks_planned,
        records_upserted=report.records_upserted,
        chunks_upserted=report.chunks_upserted,
        skipped_items=report.skipped_items,
        source_records_marked_stale=source_records_marked_stale,
        claim_sources_marked_stale=claim_sources_marked_stale,
        claims_marked_stale=claims_marked_stale,
    )


def _backfill_fact_query(tenant_id: str, *, fact_ids: Iterable[uuid.UUID] | None, limit: int) -> Select[tuple[TemporalFact]]:
    statement = select(TemporalFact).where(TemporalFact.tenant_id == tenant_id).order_by(TemporalFact.extracted_at.asc()).limit(limit)
    if fact_ids is not None:
        statement = statement.where(TemporalFact.id.in_(tuple(fact_ids)))
    return statement


async def backfill_claims_from_temporal_facts(
    db: AsyncSession,
    *,
    tenant_id: str,
    fact_ids: Iterable[uuid.UUID] | None = None,
    limit: int = 500,
    dry_run: bool = True,
) -> ClaimBackfillReport:
    facts = (await db.scalars(_backfill_fact_query(tenant_id, fact_ids=fact_ids, limit=limit))).all()
    source_records_by_item_id = await _latest_source_records_by_item_id(
        db,
        tenant_id=tenant_id,
        item_ids={fact.source_item_id for fact in facts},
    )
    report, projections = plan_claim_backfill(
        facts,
        tenant_id=tenant_id,
        dry_run=dry_run,
        supported_source_item_ids=set(source_records_by_item_id),
    )
    if dry_run:
        return report

    supported_fact_ids = {projection.temporal_fact_id for projection in projections}
    for projection in projections:
        claim_id = await _upsert_claim(db, projection)
        source_record = source_records_by_item_id[projection.source_item_id]
        claim_source_id = await _upsert_claim_source(
            db,
            projection=projection,
            claim_id=claim_id,
            source_record_id=source_record.id,
        )
        await _mark_prior_claim_sources_stale(
            db,
            tenant_id=projection.tenant_id,
            claim_id=claim_id,
            active_claim_source_id=claim_source_id,
        )
    for fact in facts:
        if fact.id in supported_fact_ids:
            continue
        await _mark_unsupported_claim_stale(db, projection=project_claim_from_temporal_fact(fact))
    await db.commit()
    return report


async def _latest_source_records_by_item_id(
    db: AsyncSession,
    *,
    tenant_id: str,
    item_ids: set[uuid.UUID],
) -> dict[uuid.UUID, SourceRecord]:
    if not item_ids:
        return {}
    records = (
        await db.scalars(
            select(SourceRecord)
            .where(SourceRecord.tenant_id == tenant_id)
            .where(SourceRecord.item_id.in_(item_ids))
            .where(SourceRecord.status.in_(CLAIM_SUPPORT_SOURCE_STATUSES))
            .order_by(SourceRecord.item_id.asc(), SourceRecord.updated_at.desc(), SourceRecord.created_at.desc())
        )
    ).all()
    latest_by_item_id: dict[uuid.UUID, SourceRecord] = {}
    for record in records:
        if record.item_id not in latest_by_item_id:
            latest_by_item_id[record.item_id] = record
    return latest_by_item_id


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
) -> tuple[uuid.UUID, ...]:
    result = await db.execute(
        update(SourceRecord)
        .where(SourceRecord.tenant_id == projection.tenant_id)
        .where(SourceRecord.item_id == projection.item_id)
        .where(SourceRecord.id != active_record_id)
        .where(SourceRecord.status == "active")
        .values(status="stale", updated_at=func.now())
        .returning(SourceRecord.id)
    )
    return tuple(result.scalars().all())


async def _invalidate_decision_claims_for_source_records(
    db: AsyncSession,
    *,
    tenant_id: str,
    source_record_ids: Iterable[uuid.UUID],
) -> SourceInvalidationReport:
    record_ids = tuple(dict.fromkeys(source_record_ids))
    if not record_ids:
        return SourceInvalidationReport(
            tenant_id=tenant_id,
            source_records_seen=0,
            claim_sources_marked_stale=0,
            claims_marked_stale=0,
        )

    dependency_rows = (
        await db.execute(
            select(ClaimSource.id, ClaimSource.claim_id)
            .join(Claim, Claim.id == ClaimSource.claim_id)
            .where(ClaimSource.tenant_id == tenant_id)
            .where(ClaimSource.source_record_id.in_(record_ids))
            .where(ClaimSource.status == "current")
            .where(Claim.tenant_id == tenant_id)
            .where(Claim.claim_type == "decision")
            .where(Claim.status == "active")
        )
    ).all()
    claim_source_ids = tuple(row[0] for row in dependency_rows)
    claim_ids = tuple(dict.fromkeys(row[1] for row in dependency_rows))
    if not claim_source_ids:
        return SourceInvalidationReport(
            tenant_id=tenant_id,
            source_records_seen=len(record_ids),
            claim_sources_marked_stale=0,
            claims_marked_stale=0,
        )

    claim_source_result = await db.execute(
        update(ClaimSource)
        .where(ClaimSource.tenant_id == tenant_id)
        .where(ClaimSource.id.in_(claim_source_ids))
        .where(ClaimSource.status == "current")
        .values(status="stale")
        .returning(ClaimSource.id)
    )
    claim_result = await db.execute(
        update(Claim)
        .where(Claim.tenant_id == tenant_id)
        .where(Claim.id.in_(claim_ids))
        .where(Claim.claim_type == "decision")
        .where(Claim.status == "active")
        .values(status="stale", updated_at=func.now())
        .returning(Claim.id)
    )
    return SourceInvalidationReport(
        tenant_id=tenant_id,
        source_records_seen=len(record_ids),
        claim_sources_marked_stale=len(claim_source_result.scalars().all()),
        claims_marked_stale=len(claim_result.scalars().all()),
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


async def _upsert_claim(db: AsyncSession, projection: ClaimProjection) -> uuid.UUID:
    stmt = (
        insert(Claim.__table__)
        .values(
            tenant_id=projection.tenant_id,
            claim_key=projection.claim_key,
            claim_text=projection.claim_text,
            claim_type=projection.claim_type,
            confidence=projection.confidence,
            status=projection.status,
            metadata=projection.metadata,
        )
        .on_conflict_do_update(
            constraint="uq_claims_tenant_claim_key",
            set_={
                "claim_text": projection.claim_text,
                "claim_type": projection.claim_type,
                "confidence": projection.confidence,
                "status": projection.status,
                "metadata": projection.metadata,
                "updated_at": func.now(),
            },
        )
        .returning(Claim.__table__.c.id)
    )
    return await db.scalar(stmt)


async def _resolve_source_chunk_id(
    db: AsyncSession,
    *,
    tenant_id: str,
    source_record_id: uuid.UUID,
    projection: ClaimProjection,
) -> uuid.UUID | None:
    span = projection.source_span if isinstance(projection.source_span, dict) else {}

    raw_chunk_id = span.get("source_chunk_id")
    if raw_chunk_id:
        try:
            chunk_id = uuid.UUID(str(raw_chunk_id))
        except ValueError:
            chunk_id = None
        if chunk_id is not None:
            return await db.scalar(
                select(SourceChunk.id)
                .where(SourceChunk.tenant_id == tenant_id)
                .where(SourceChunk.source_record_id == source_record_id)
                .where(SourceChunk.id == chunk_id)
            )

    raw_chunk_digest = span.get("source_chunk_digest") or span.get("chunk_digest")
    if isinstance(raw_chunk_digest, str) and raw_chunk_digest.strip():
        return await db.scalar(
            select(SourceChunk.id)
            .where(SourceChunk.tenant_id == tenant_id)
            .where(SourceChunk.source_record_id == source_record_id)
            .where(SourceChunk.chunk_digest == raw_chunk_digest.strip())
        )

    raw_chunk_index = span.get("source_chunk_index") or span.get("chunk_index")
    if raw_chunk_index is not None:
        try:
            chunk_index = int(raw_chunk_index)
        except (TypeError, ValueError):
            chunk_index = None
        if chunk_index is not None:
            return await db.scalar(
                select(SourceChunk.id)
                .where(SourceChunk.tenant_id == tenant_id)
                .where(SourceChunk.source_record_id == source_record_id)
                .where(SourceChunk.chunk_index == chunk_index)
            )

    return None


async def _upsert_claim_source(
    db: AsyncSession,
    *,
    projection: ClaimProjection,
    claim_id: uuid.UUID,
    source_record_id: uuid.UUID,
) -> uuid.UUID:
    source_span = _safe_source_span(projection.source_span)
    source_chunk_id = await _resolve_source_chunk_id(
        db,
        tenant_id=projection.tenant_id,
        source_record_id=source_record_id,
        projection=projection,
    )
    stmt = (
        insert(ClaimSource.__table__)
        .values(
            tenant_id=projection.tenant_id,
            claim_id=claim_id,
            source_record_id=source_record_id,
            source_chunk_id=source_chunk_id,
            support_role=projection.support_role,
            status="current",
            source_digest=projection.source_digest,
            source_span=source_span,
        )
        .on_conflict_do_update(
            constraint="uq_claim_sources_support",
            set_={
                "source_chunk_id": source_chunk_id,
                "status": "current",
                "source_span": source_span,
            },
        )
        .returning(ClaimSource.__table__.c.id)
    )
    return await db.scalar(stmt)


async def _mark_prior_claim_sources_stale(
    db: AsyncSession,
    *,
    tenant_id: str,
    claim_id: uuid.UUID,
    active_claim_source_id: uuid.UUID,
) -> None:
    await db.execute(
        update(ClaimSource)
        .where(ClaimSource.tenant_id == tenant_id)
        .where(ClaimSource.claim_id == claim_id)
        .where(ClaimSource.id != active_claim_source_id)
        .where(ClaimSource.status == "current")
        .values(status="stale")
    )


async def _mark_unsupported_claim_stale(db: AsyncSession, *, projection: ClaimProjection) -> None:
    claim_id = await db.scalar(
        select(Claim.id).where(Claim.tenant_id == projection.tenant_id, Claim.claim_key == projection.claim_key)
    )
    if claim_id is None:
        return
    await db.execute(
        update(Claim)
        .where(Claim.tenant_id == projection.tenant_id)
        .where(Claim.id == claim_id)
        .where(Claim.status == "active")
        .values(status="stale", updated_at=func.now())
    )
    await db.execute(
        update(ClaimSource)
        .where(ClaimSource.tenant_id == projection.tenant_id)
        .where(ClaimSource.claim_id == claim_id)
        .where(ClaimSource.status == "current")
        .values(status="stale")
    )


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


def _claim_support_state(claim: Claim, sources: tuple[ClaimSourceSupportSummary, ...]) -> tuple[str, str | None]:
    if claim.status == "conflicted":
        return "conflicted", "claim_status_conflicted"
    if claim.status in {"rejected", "superseded"}:
        return "not_authoritative", f"claim_status_{claim.status}"
    if claim.status == "draft":
        return "generated_unpromoted", "claim_not_promoted"
    if claim.status == "stale":
        return "stale_source", "claim_status_stale"
    if not sources:
        return "source_missing", "claim_has_no_source_support"
    if any(source.status == "stale" or source.source_record_status != "active" for source in sources):
        return "stale_source", "claim_source_not_current"
    if any(source.support_role == "contradicts" for source in sources):
        return "conflicted", "claim_source_contradicts"
    if any(source.source_chunk_id is not None for source in sources):
        return "source_backed", None
    return "weak_source_support", "claim_source_lacks_exact_chunk"


async def get_claim_support_report(
    db: AsyncSession,
    *,
    tenant_id: str,
    claim_type: str | None = "decision",
    status: str | None = None,
    limit: int = 50,
) -> ClaimSupportReport:
    statement = select(Claim).where(Claim.tenant_id == tenant_id).order_by(Claim.updated_at.desc(), Claim.id.desc()).limit(limit)
    if claim_type is not None:
        statement = statement.where(Claim.claim_type == claim_type)
    if status is not None:
        statement = statement.where(Claim.status == status)
    claims = (await db.scalars(statement)).all()
    summaries: list[ClaimSupportSummary] = []
    for claim in claims:
        rows = (
            await db.execute(
                select(ClaimSource, SourceRecord.item_id, SourceRecord.status)
                .join(SourceRecord, SourceRecord.id == ClaimSource.source_record_id)
                .where(ClaimSource.tenant_id == tenant_id, ClaimSource.claim_id == claim.id)
                .where(SourceRecord.tenant_id == tenant_id)
                .order_by(ClaimSource.created_at.asc(), ClaimSource.id.asc())
            )
        ).all()
        source_summaries = tuple(
            ClaimSourceSupportSummary(
                id=claim_source.id,
                source_record_id=claim_source.source_record_id,
                source_chunk_id=claim_source.source_chunk_id,
                source_item_id=source_item_id,
                source_record_status=source_record_status,
                support_role=claim_source.support_role,
                status=claim_source.status,
                source_digest=claim_source.source_digest,
                source_span=_safe_source_span(claim_source.source_span),
            )
            for claim_source, source_item_id, source_record_status in rows
        )
        support_state, warning = _claim_support_state(claim, source_summaries)
        summaries.append(
            ClaimSupportSummary(
                id=claim.id,
                claim_key=claim.claim_key,
                claim_text=claim.claim_text,
                claim_type=claim.claim_type,
                confidence=claim.confidence,
                status=claim.status,
                support_state=support_state,
                warning=warning,
                metadata=_safe_claim_metadata(claim.metadata_),
                sources=source_summaries,
            )
        )
    return ClaimSupportReport(tenant_id=tenant_id, claims=tuple(summaries))
