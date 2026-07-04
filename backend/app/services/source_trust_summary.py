from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import Select, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.item import Item
from app.models.palace import SourceChunk, SourceRecord
from app.schemas.retrieval_provenance import RetrievalTrustClass

SourceTrustState = Literal[
    "source_backed",
    "curated_memory",
    "generated_unpromoted",
    "stale_source",
    "source_missing",
    "policy_limited",
    "unknown",
]

_STALE_SOURCE_STATUSES = {"stale", "failed", "deleted", "superseded"}
_GENERATED_ARTIFACT_KEYS = {
    "candidate_curation_artifact",
    "conversation_fact",
    "diary_rollup",
    "memory_dream",
    "routing_manifest",
    "wakeup_brief",
}
DEFAULT_SOURCE_TRUST_HEALTH_ITEM_LIMIT = 500


@dataclass(frozen=True)
class SourceTrustSummary:
    item_id: uuid.UUID
    state: SourceTrustState
    source_record_id: uuid.UUID | None = None
    source_status: str | None = None
    chunk_count: int = 0
    stale_reason: str | None = None
    warning: str | None = None
    source_title: str | None = None
    source_url: str | None = None


@dataclass(frozen=True)
class SourceTrustHealthWarning:
    state: SourceTrustState
    warning: str
    count: int


@dataclass(frozen=True)
class SourceTrustHealthSummary:
    status: Literal["ready", "empty"] = "empty"
    total_contexts: int = 0
    source_backed: int = 0
    generated_unpromoted: int = 0
    stale_missing: int = 0
    policy_limited: int = 0
    unknown: int = 0
    recent_warnings: list[SourceTrustHealthWarning] | None = None


@dataclass(frozen=True)
class _SourceRecordRow:
    record: SourceRecord
    chunk_count: int


@dataclass(frozen=True)
class _ItemTrustProjection:
    id: uuid.UUID
    metadata_: dict[str, Any]
    title: str | None = None
    source_url: str | None = None


def map_retrieval_trust_class(trust_class: RetrievalTrustClass | str | None) -> SourceTrustState:
    if trust_class == "raw_source":
        return "source_backed"
    if trust_class == "curated_memory":
        return "curated_memory"
    if trust_class in {"generated_synthesis", "low_support_generated"}:
        return "generated_unpromoted"
    if trust_class == "stale_context":
        return "stale_source"
    if trust_class == "broad_fallback":
        return "unknown"
    return "unknown"


async def get_source_trust_summaries(
    db: AsyncSession,
    *,
    tenant_id: str,
    item_ids: list[uuid.UUID] | tuple[uuid.UUID, ...] | set[uuid.UUID],
) -> dict[uuid.UUID, SourceTrustSummary]:
    requested_item_ids = tuple(dict.fromkeys(item_ids))
    if not requested_item_ids:
        return {}

    items = (
        await db.scalars(
            select(Item)
            .where(Item.tenant_id == tenant_id)
            .where(Item.id.in_(requested_item_ids))
        )
    ).all()
    items_by_id = {item.id: item for item in items}
    source_records = await _latest_source_records_by_item_id(db, tenant_id=tenant_id, item_ids=requested_item_ids)

    return {
        item_id: _trust_summary_for_item(
            item_id=item_id,
            item=items_by_id.get(item_id),
            source_row=source_records.get(item_id),
        )
        for item_id in requested_item_ids
    }


async def build_source_trust_health_summary(
    db: AsyncSession,
    *,
    tenant_id: str,
    item_limit: int = DEFAULT_SOURCE_TRUST_HEALTH_ITEM_LIMIT,
) -> SourceTrustHealthSummary:
    rows = (await db.execute(source_trust_health_item_statement(tenant_id=tenant_id, limit=item_limit))).all()
    items = [
        _ItemTrustProjection(
            id=_row_value(row, "id", 0),
            metadata_=_row_value(row, "metadata_", 1) or {},
        )
        for row in rows
    ]
    item_ids = [item.id for item in items]
    if not item_ids:
        return SourceTrustHealthSummary(status="empty", recent_warnings=[])

    source_records = await _latest_source_records_by_item_id(db, tenant_id=tenant_id, item_ids=tuple(item_ids))
    summaries = {
        item.id: _trust_summary_for_item(
            item_id=item.id,
            item=item,
            source_row=source_records.get(item.id),
        )
        for item in items
    }
    state_counts: Counter[str] = Counter()
    warning_counts: Counter[tuple[str, str]] = Counter()
    warning_first_seen: dict[tuple[str, str], int] = {}
    for index, summary in enumerate(summaries.values()):
        state_counts[summary.state] += 1
        if summary.warning:
            warning_key = (summary.state, summary.warning)
            warning_counts[warning_key] += 1
            warning_first_seen.setdefault(warning_key, index)

    recent_warnings = [
        SourceTrustHealthWarning(state=state, warning=warning, count=count)
        for (state, warning), count in sorted(
            warning_counts.items(),
            key=lambda item: (warning_first_seen[item[0]], item[0][0], item[0][1]),
        )[:5]
    ]
    return SourceTrustHealthSummary(
        status="ready",
        total_contexts=len(item_ids),
        source_backed=state_counts["source_backed"],
        generated_unpromoted=state_counts["generated_unpromoted"],
        stale_missing=state_counts["stale_source"] + state_counts["source_missing"],
        policy_limited=state_counts["policy_limited"],
        unknown=state_counts["unknown"] + state_counts["curated_memory"],
        recent_warnings=recent_warnings,
    )


def source_trust_health_item_statement(
    *,
    tenant_id: str,
    limit: int = DEFAULT_SOURCE_TRUST_HEALTH_ITEM_LIMIT,
) -> Select:
    bounded_limit = max(int(limit), 1)
    return (
        select(Item.id, Item.metadata_)
        .where(Item.tenant_id == tenant_id)
        .where(Item.status == "ready")
        .where(Item.deleted_at.is_(None))
        .order_by(Item.updated_at.desc())
        .limit(bounded_limit)
    )


def _row_value(row: Any, key: str, index: int = 0) -> Any:
    mapping = getattr(row, "_mapping", None)
    if mapping is not None and key in mapping:
        return mapping[key]
    if hasattr(row, key):
        return getattr(row, key)
    if isinstance(row, tuple):
        return row[index]
    return row


async def _latest_source_records_by_item_id(
    db: AsyncSession,
    *,
    tenant_id: str,
    item_ids: tuple[uuid.UUID, ...],
) -> dict[uuid.UUID, _SourceRecordRow]:
    rows = (
        await db.execute(
            source_record_batch_statement(tenant_id=tenant_id, item_ids=item_ids)
        )
    ).all()
    latest_by_item_id: dict[uuid.UUID, _SourceRecordRow] = {}
    for record, chunk_count in rows:
        if record.item_id in latest_by_item_id:
            continue
        latest_by_item_id[record.item_id] = _SourceRecordRow(record=record, chunk_count=int(chunk_count or 0))
    return latest_by_item_id


def source_record_batch_statement(*, tenant_id: str, item_ids: tuple[uuid.UUID, ...]) -> Select:
    return (
        select(SourceRecord, func.count(SourceChunk.id).label("chunk_count"))
        .outerjoin(
            SourceChunk,
            and_(
                SourceChunk.tenant_id == tenant_id,
                SourceChunk.source_record_id == SourceRecord.id,
            ),
        )
        .where(SourceRecord.tenant_id == tenant_id)
        .where(SourceRecord.item_id.in_(item_ids))
        .group_by(SourceRecord.id)
        .order_by(SourceRecord.item_id.asc(), SourceRecord.updated_at.desc(), SourceRecord.created_at.desc())
    )


def _trust_summary_for_item(
    *,
    item_id: uuid.UUID,
    item: Item | None,
    source_row: _SourceRecordRow | None,
) -> SourceTrustSummary:
    if item is None:
        return SourceTrustSummary(
            item_id=item_id,
            state="unknown",
            warning="item_not_found_or_not_visible",
        )

    metadata = item.metadata_ if isinstance(item.metadata_, dict) else {}
    if _is_policy_limited(metadata):
        return SourceTrustSummary(
            item_id=item_id,
            state="policy_limited",
            warning="source_summary_policy_limited",
        )

    if source_row is not None:
        return _trust_summary_for_source_record(item=item, source_row=source_row)

    if _is_generated_unpromoted(metadata):
        return SourceTrustSummary(
            item_id=item_id,
            state="generated_unpromoted",
            warning="generated_artifact_without_promoted_source_support",
            source_title=_safe_title(item),
            source_url=_safe_source_url(item.source_url),
        )

    if _is_curated_memory(metadata):
        return SourceTrustSummary(
            item_id=item_id,
            state="curated_memory",
            warning="curated_memory_without_source_record",
            source_title=_safe_title(item),
            source_url=_safe_source_url(item.source_url),
        )

    return SourceTrustSummary(
        item_id=item_id,
        state="source_missing",
        warning="source_record_missing",
        source_title=_safe_title(item),
        source_url=_safe_source_url(item.source_url),
    )


def _trust_summary_for_source_record(*, item: Item, source_row: _SourceRecordRow) -> SourceTrustSummary:
    record = source_row.record
    if record.status in _STALE_SOURCE_STATUSES:
        return SourceTrustSummary(
            item_id=item.id,
            state="stale_source",
            source_record_id=record.id,
            source_status=record.status,
            chunk_count=source_row.chunk_count,
            stale_reason=record.failure_reason or _metadata_stale_reason(record.metadata_),
            warning=f"source_record_{record.status}",
            source_title=_safe_title(item),
            source_url=_safe_source_url(record.source_uri or item.source_url),
        )
    if record.status == "active" and source_row.chunk_count > 0:
        return SourceTrustSummary(
            item_id=item.id,
            state="source_backed",
            source_record_id=record.id,
            source_status=record.status,
            chunk_count=source_row.chunk_count,
            source_title=_safe_title(item),
            source_url=_safe_source_url(record.source_uri or item.source_url),
        )
    if record.status == "active":
        return SourceTrustSummary(
            item_id=item.id,
            state="source_missing",
            source_record_id=record.id,
            source_status=record.status,
            chunk_count=0,
            warning="source_record_has_no_chunks",
            source_title=_safe_title(item),
            source_url=_safe_source_url(record.source_uri or item.source_url),
        )
    return SourceTrustSummary(
        item_id=item.id,
        state="unknown",
        source_record_id=record.id,
        source_status=record.status,
        chunk_count=source_row.chunk_count,
        warning="source_record_status_unknown",
        source_title=_safe_title(item),
        source_url=_safe_source_url(record.source_uri or item.source_url),
    )


def _is_policy_limited(metadata: dict[str, Any]) -> bool:
    memory_entry = metadata.get("memory_entry") if isinstance(metadata.get("memory_entry"), dict) else {}
    entry_metadata = memory_entry.get("metadata") if isinstance(memory_entry.get("metadata"), dict) else {}
    return any(
        value is True
        for value in (
            metadata.get("policy_limited"),
            metadata.get("source_policy_limited"),
            memory_entry.get("policy_limited"),
            entry_metadata.get("policy_limited"),
            entry_metadata.get("source_policy_limited"),
        )
    )


def _is_generated_unpromoted(metadata: dict[str, Any]) -> bool:
    if metadata.get("advisory_generated_context") is True:
        return metadata.get("promotion_state") != "promoted"
    if metadata.get("promoted_source_backed") is False and metadata.get("source_support_level") in {"no_source", "partial_source"}:
        return True
    if any(key in metadata for key in _GENERATED_ARTIFACT_KEYS):
        return True
    memory_entry = metadata.get("memory_entry") if isinstance(metadata.get("memory_entry"), dict) else {}
    entry_metadata = memory_entry.get("metadata") if isinstance(memory_entry.get("metadata"), dict) else {}
    return any(key in entry_metadata for key in _GENERATED_ARTIFACT_KEYS)


def _is_curated_memory(metadata: dict[str, Any]) -> bool:
    memory_entry = metadata.get("memory_entry")
    return isinstance(memory_entry, dict)


def _metadata_stale_reason(metadata: Any) -> str | None:
    if not isinstance(metadata, dict):
        return None
    reason = metadata.get("stale_reason") or metadata.get("failure_reason") or metadata.get("last_error")
    return str(reason) if reason else None


def _safe_title(item: Item) -> str | None:
    title = (item.title or "").strip()
    return title or None


def _safe_source_url(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
