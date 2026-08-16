"""Bounded, tenant-safe summary recovery for ready media and web items."""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import func, or_, select

from app.database import tenant_async_session
from app.models.item import Item
from app.services.llm import LLMCompletionDiagnostics, LLMService

logger = logging.getLogger(__name__)

ALLOWED_SOURCE_TYPES = frozenset({"media", "webpage"})
DEFAULT_SOURCE_TYPES = ("media", "webpage")
DEFAULT_MODEL = "minimax/minimax-m2.7"
MAX_BATCH_SIZE = 25
MAX_LIMIT = 10_000

PersistOutcome = Literal["completed", "skipped"]


@dataclass(frozen=True)
class SummaryCandidate:
    item_id: uuid.UUID
    tenant_id: str
    source_type: str
    raw_content: str
    source_content_hash: str


@dataclass(frozen=True)
class SummaryBackfillReport:
    selected: int = 0
    completed: int = 0
    skipped: int = 0
    failed: int = 0
    remaining: int = 0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _content_hash(raw_content: str) -> str:
    return hashlib.sha256(raw_content.encode()).hexdigest()


def _eligible(item: Item, *, source_types: Sequence[str] = DEFAULT_SOURCE_TYPES) -> bool:
    """Apply all write gates again after the row is locked."""
    return (
        item.tenant_id is not None
        and item.source_type in source_types
        and item.status == "ready"
        and item.deleted_at is None
        and not str(item.summary or "").strip()
        and bool(str(item.raw_content or "").strip())
    )


def _candidate_filters(
    *,
    tenant_id: str,
    source_types: Sequence[str],
    item_ids: Sequence[uuid.UUID],
) -> tuple[Any, ...]:
    filters: list[Any] = [
        Item.tenant_id == tenant_id,
        Item.source_type.in_(tuple(source_types)),
        Item.status == "ready",
        Item.deleted_at.is_(None),
        or_(Item.summary.is_(None), func.btrim(Item.summary) == ""),
        Item.raw_content.is_not(None),
        func.btrim(Item.raw_content) != "",
    ]
    if item_ids:
        filters.append(Item.id.in_(tuple(item_ids)))
    return tuple(filters)


def _candidate_query(
    *,
    tenant_id: str,
    source_types: Sequence[str],
    limit: int,
    item_ids: Sequence[uuid.UUID],
):
    return (
        select(Item)
        .where(
            *_candidate_filters(
                tenant_id=tenant_id,
                source_types=source_types,
                item_ids=item_ids,
            )
        )
        .order_by(Item.created_at.asc(), Item.id.asc())
        .limit(limit)
    )


async def _load_candidates(
    *,
    tenant_id: str,
    source_types: Sequence[str],
    limit: int,
    item_ids: Sequence[uuid.UUID],
    session_factory: Callable[[str], Any] = tenant_async_session,
) -> list[SummaryCandidate]:
    # Close this read transaction before any provider request begins.
    async with session_factory(tenant_id) as db:
        result = await db.execute(
            _candidate_query(
                tenant_id=tenant_id,
                source_types=source_types,
                limit=limit,
                item_ids=item_ids,
            )
        )
        items = result.scalars().all()
    return [
        SummaryCandidate(
            item_id=item.id,
            tenant_id=item.tenant_id,
            source_type=item.source_type,
            raw_content=str(item.raw_content),
            source_content_hash=_content_hash(str(item.raw_content)),
        )
        for item in items
    ]


async def _count_candidates(
    *,
    tenant_id: str,
    source_types: Sequence[str],
    item_ids: Sequence[uuid.UUID],
    session_factory: Callable[[str], Any] = tenant_async_session,
) -> int:
    statement = select(func.count(Item.id)).where(
        *_candidate_filters(
            tenant_id=tenant_id,
            source_types=source_types,
            item_ids=item_ids,
        )
    )
    async with session_factory(tenant_id) as db:
        result = await db.execute(statement)
        return int(result.scalar_one())


async def _persist_summary(
    candidate: SummaryCandidate,
    summary: str,
    *,
    diagnostics: LLMCompletionDiagnostics,
    requested_model: str,
    source_types: Sequence[str] = DEFAULT_SOURCE_TYPES,
    session_factory: Callable[[str], Any] = tenant_async_session,
    now: Callable[[], datetime] = _utc_now,
) -> PersistOutcome:
    """Recheck a locked row and update only its summary and compact receipt."""
    del diagnostics  # Provider details are intentionally excluded from stored metadata.
    clean_summary = summary.strip()
    if not clean_summary:
        return "skipped"

    async with session_factory(candidate.tenant_id) as db:
        result = await db.execute(
            select(Item)
            .where(Item.tenant_id == candidate.tenant_id)
            .where(Item.id == candidate.item_id)
            .with_for_update(skip_locked=True)
        )
        item = result.scalar_one_or_none()
        if item is None or not _eligible(item, source_types=source_types):
            return "skipped"
        if _content_hash(str(item.raw_content)) != candidate.source_content_hash:
            return "skipped"

        metadata = dict(item.metadata_ or {})
        metadata["summary_backfill"] = {
            "schema_version": 1,
            "source": "backfill_missing_summaries",
            "completed_at": now().isoformat(),
            "requested_model": requested_model,
            "source_content_hash": candidate.source_content_hash,
            "source_type": candidate.source_type,
        }
        item.summary = clean_summary
        item.metadata_ = metadata
        await db.commit()
        return "completed"


async def run_summary_backfill(
    *,
    tenant_id: str,
    limit: int,
    batch_size: int = MAX_BATCH_SIZE,
    source_types: Sequence[str] = DEFAULT_SOURCE_TYPES,
    item_ids: Sequence[uuid.UUID] = (),
    write: bool = False,
    model: str = DEFAULT_MODEL,
    llm: LLMService | Any | None = None,
    session_factory: Callable[[str], Any] = tenant_async_session,
    candidate_loader: Callable[..., Awaitable[list[SummaryCandidate]]] = _load_candidates,
    candidate_counter: Callable[..., Awaitable[int]] = _count_candidates,
    summary_writer: Callable[..., Awaitable[PersistOutcome]] = _persist_summary,
) -> SummaryBackfillReport:
    """Backfill summaries sequentially; dry-run is the default behavior."""
    clean_tenant_id = tenant_id.strip()
    selected_source_types = tuple(dict.fromkeys(source_types))
    selected_item_ids = tuple(dict.fromkeys(item_ids))
    if not clean_tenant_id:
        raise ValueError("tenant_id must not be blank")
    if limit < 1 or limit > MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    if batch_size < 1 or batch_size > MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
    if not selected_source_types or not set(selected_source_types) <= ALLOWED_SOURCE_TYPES:
        raise ValueError("source_types must contain only media or webpage")
    if len(selected_item_ids) > limit:
        raise ValueError("item_ids must not exceed limit")
    if not model.strip():
        raise ValueError("model must not be blank")

    common = {
        "tenant_id": clean_tenant_id,
        "source_types": selected_source_types,
        "item_ids": selected_item_ids,
        "session_factory": session_factory,
    }
    candidates = await candidate_loader(limit=limit, **common)
    if not write:
        remaining = await candidate_counter(**common)
        return SummaryBackfillReport(selected=len(candidates), remaining=remaining)

    service = llm or LLMService()
    completed = skipped = failed = 0
    for batch_start in range(0, len(candidates), batch_size):
        for candidate in candidates[batch_start : batch_start + batch_size]:
            diagnostics = LLMCompletionDiagnostics()
            try:
                summary = await service.summarize(
                    candidate.raw_content[:4000],
                    model=model,
                    diagnostics=diagnostics,
                )
                outcome = await summary_writer(
                    candidate,
                    summary,
                    diagnostics=diagnostics,
                    requested_model=model,
                    source_types=selected_source_types,
                    session_factory=session_factory,
                )
                if outcome == "completed":
                    completed += 1
                else:
                    skipped += 1
            except Exception as exc:
                failed += 1
                logger.error(
                    "Summary backfill failed item_id=%s model=%s error_type=%s",
                    candidate.item_id,
                    model,
                    type(exc).__name__,
                )

    remaining = await candidate_counter(**common)
    return SummaryBackfillReport(
        selected=len(candidates),
        completed=completed,
        skipped=skipped,
        failed=failed,
        remaining=remaining,
    )
