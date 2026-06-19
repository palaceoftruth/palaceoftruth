from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.item import Item
from app.schemas.memory import MemoryEntryRequest, MemoryScope
from app.services.memory import MemoryArtifactAcceptanceResult, accept_canonical_memory_entry


CONVERSATION_FACT_SOURCE = "conversation-fact-backfill"
CONVERSATION_FACT_TAG = "conversation-fact"
DERIVED_MEMORY_TAG = "derived-memory"
CONVERSATION_FACT_SCHEMA_VERSION = 1
DEFAULT_FACT_LIMIT = 100
DEFAULT_MAX_FACTS_PER_ITEM = 20
DEFAULT_BACKFILL_WORKERS = 1
MAX_BACKFILL_WORKERS = 16

_MARKDOWN_TURN_RE = re.compile(
    r"^\*\*(?P<speaker>.+?)\*\*\s*"
    r"\((?P<date>\d{4}-\d{2}-\d{2})\s+"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>AM|PM|am|pm)?\)\s*:\s*"
    r"(?P<text>.*)$"
)
_SIMPLE_TURN_RE = re.compile(r"^(?P<speaker>User|Assistant|Human|Andrew|Codex|Agent)\s*:\s*(?P<text>.+)$", re.I)


@dataclass(frozen=True)
class ConversationTurn:
    speaker: str
    text: str
    turn_index: int
    line_start: int
    line_end: int
    timestamp: str | None = None


@dataclass(frozen=True)
class ConversationFactBackfillResult:
    items_scanned: int
    items_completed: int
    items_skipped: int
    items_failed: int
    facts_discovered: int
    facts_submitted: int
    facts_queued: int
    facts_existing: int
    dry_run: bool
    worker_count: int = DEFAULT_BACKFILL_WORKERS
    failures: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class _ItemBackfillPlan:
    item: Item
    entries: tuple[MemoryEntryRequest, ...]


@dataclass
class _WorkerState:
    next_index: int = 0


def _normalize_speaker(value: str) -> str:
    return " ".join(value.strip().split())


def _normalize_fact_text(value: str) -> str:
    return " ".join(value.strip().split())


def _parse_markdown_timestamp(match: re.Match[str]) -> str:
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    ampm = (match.group("ampm") or "").upper()
    if ampm == "PM" and hour < 12:
        hour += 12
    if ampm == "AM" and hour == 12:
        hour = 0
    return f"{match.group('date')}T{hour:02d}:{minute:02d}:00Z"


def parse_conversation_turns(body: str) -> list[ConversationTurn]:
    turns: list[ConversationTurn] = []
    current: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        text = _normalize_fact_text("\n".join(current["text_lines"]))
        speaker = _normalize_speaker(current["speaker"])
        if speaker and text:
            turns.append(
                ConversationTurn(
                    speaker=speaker,
                    text=text,
                    timestamp=current.get("timestamp"),
                    turn_index=len(turns),
                    line_start=int(current["line_start"]),
                    line_end=int(current["line_end"]),
                )
            )
        current = None

    for line_number, raw_line in enumerate(body.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        markdown_match = _MARKDOWN_TURN_RE.match(line)
        simple_match = _SIMPLE_TURN_RE.match(line)
        if markdown_match or simple_match:
            flush()
            current = {
                "speaker": (markdown_match or simple_match).group("speaker"),
                "timestamp": _parse_markdown_timestamp(markdown_match) if markdown_match else None,
                "text_lines": [(markdown_match or simple_match).group("text")],
                "line_start": line_number,
                "line_end": line_number,
            }
            continue
        if current is not None:
            current["text_lines"].append(line)
            current["line_end"] = line_number
    flush()
    return turns


def _memory_scope_from_item(item: Item) -> MemoryScope | None:
    memory_entry = (item.metadata_ or {}).get("memory_entry")
    if not isinstance(memory_entry, dict):
        return None
    scope = memory_entry.get("scope")
    if not isinstance(scope, dict):
        return None
    try:
        return MemoryScope.model_validate(scope)
    except ValueError:
        return None


def _is_source_memory_entry(item: Item) -> bool:
    metadata = item.metadata_ or {}
    if metadata.get("conversation_fact"):
        return False
    memory_entry = metadata.get("memory_entry")
    if isinstance(memory_entry, dict):
        if memory_entry.get("source") == CONVERSATION_FACT_SOURCE:
            return False
        client_metadata = memory_entry.get("metadata")
        if isinstance(client_metadata, dict) and client_metadata.get("conversation_fact"):
            return False
    return _memory_scope_from_item(item) is not None and bool(item.raw_content)


def _chunk_index_for_line(item: Item, line_start: int) -> int:
    chunks = item.content_chunks
    if not isinstance(chunks, list) or not chunks:
        return 0
    next_line = 1
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        text = str(chunk.get("text") or "")
        line_count = max(1, text.count("\n") + 1)
        if next_line <= line_start < next_line + line_count:
            raw_index = chunk.get("index")
            return int(raw_index) if isinstance(raw_index, int) else 0
        next_line += line_count
    return 0


def _source_fingerprint(item: Item, turn: ConversationTurn) -> str:
    payload = {
        "source_item_id": str(item.id),
        "turn_index": turn.turn_index,
        "line_start": turn.line_start,
        "line_end": turn.line_end,
        "speaker": turn.speaker,
        "timestamp": turn.timestamp,
        "text": turn.text,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _idempotency_key(item: Item, turn: ConversationTurn) -> str:
    return _source_fingerprint(item, turn)


def _source_span(item: Item, turn: ConversationTurn) -> dict[str, Any]:
    return {
        "source_item_id": str(item.id),
        "source_item_title": item.title,
        "chunk_index": _chunk_index_for_line(item, turn.line_start),
        "line_start": turn.line_start,
        "line_end": turn.line_end,
        "turn_index": turn.turn_index,
        "timestamp": turn.timestamp,
    }


def build_conversation_fact_entries(
    item: Item,
    *,
    max_facts_per_item: int = DEFAULT_MAX_FACTS_PER_ITEM,
) -> list[MemoryEntryRequest]:
    scope = _memory_scope_from_item(item)
    if scope is None or not item.raw_content:
        return []

    entries: list[MemoryEntryRequest] = []
    for turn in parse_conversation_turns(item.raw_content)[:max_facts_per_item]:
        source_span = _source_span(item, turn)
        subject = turn.speaker
        predicate = "said"
        object_text = turn.text
        title = f"Conversation fact: {subject} said"
        body = (
            f"Conversation fact\n\n"
            f"Subject: {subject}\n"
            f"Predicate: {predicate}\n"
            f"Object: {object_text}\n\n"
            f"Source span: item_id={source_span['source_item_id']} "
            f"chunk_index={source_span['chunk_index']} "
            f"lines={source_span['line_start']}-{source_span['line_end']} "
            f"turn_index={source_span['turn_index']}"
        )
        if turn.timestamp:
            body += f" timestamp={turn.timestamp}"
        metadata = {
            "conversation_fact": {
                "schema_version": CONVERSATION_FACT_SCHEMA_VERSION,
                "advisory": True,
                "source_item_id": str(item.id),
                "source_item_title": item.title,
                "source_span": source_span,
                "subject": subject,
                "predicate": predicate,
                "object_text": object_text,
                "source_fingerprint": _source_fingerprint(item, turn),
            }
        }
        entries.append(
            MemoryEntryRequest(
                tenant_id=item.tenant_id,
                title=title,
                body=body,
                summary=f"{subject} said: {object_text[:220]}",
                source=CONVERSATION_FACT_SOURCE,
                created_at=item.created_at or datetime.now(timezone.utc),
                tags=[CONVERSATION_FACT_TAG, DERIVED_MEMORY_TAG],
                scope=scope,
                source_url=item.source_url,
                metadata=metadata,
                idempotency_key=_idempotency_key(item, turn),
                enable_ai_enrichment=False,
                relationship_policy="skip",
            )
        )
    return entries


def _bounded_worker_count(workers: int, total_items: int) -> int:
    if total_items <= 0:
        return 0
    if workers < 1:
        return 1
    return min(workers, total_items, MAX_BACKFILL_WORKERS)


def _failure_for_item(item: Item, *, reason: str, error: BaseException | str) -> dict[str, Any]:
    return {
        "source_item_id": str(item.id),
        "source_item_title": item.title,
        "reason": reason,
        "error": str(error),
    }


async def _build_entries_for_item(item: Item, *, max_facts_per_item: int) -> _ItemBackfillPlan | None:
    if not _is_source_memory_entry(item):
        return None
    entries = build_conversation_fact_entries(item, max_facts_per_item=max_facts_per_item)
    return _ItemBackfillPlan(item=item, entries=tuple(entries))


async def _run_with_optional_timeout(coro, *, timeout_seconds: float | None):
    if timeout_seconds is None:
        return await coro
    return await asyncio.wait_for(coro, timeout=timeout_seconds)


async def _build_item_plans(
    items: list[Item],
    *,
    max_facts_per_item: int,
    workers: int,
    item_timeout_seconds: float | None,
) -> tuple[list[_ItemBackfillPlan], list[dict[str, Any]], int]:
    worker_count = _bounded_worker_count(workers, len(items))
    if worker_count == 0:
        return [], [], 0

    state = _WorkerState()
    plans: list[_ItemBackfillPlan] = []
    failures: list[dict[str, Any]] = []
    lock = asyncio.Lock()

    async def worker() -> None:
        while True:
            async with lock:
                if state.next_index >= len(items):
                    return
                item = items[state.next_index]
                state.next_index += 1
            try:
                plan = await _run_with_optional_timeout(
                    _build_entries_for_item(item, max_facts_per_item=max_facts_per_item),
                    timeout_seconds=item_timeout_seconds,
                )
            except TimeoutError as exc:
                failures.append(_failure_for_item(item, reason="timeout", error=exc))
                continue
            except Exception as exc:
                failures.append(_failure_for_item(item, reason="build_failed", error=exc))
                continue
            if plan is not None:
                plans.append(plan)

    await asyncio.gather(*(worker() for _ in range(worker_count)))
    plans.sort(key=lambda plan: (plan.item.updated_at or datetime.min.replace(tzinfo=timezone.utc), plan.item.id))
    failures.sort(key=lambda failure: failure["source_item_id"])
    return plans, failures, worker_count


async def _submit_item_plan(
    db: AsyncSession,
    *,
    plan: _ItemBackfillPlan,
    signing_key: str | None,
    item_timeout_seconds: float | None,
) -> tuple[int, int, int, dict[str, Any] | None]:
    facts_submitted = 0
    facts_queued = 0
    facts_existing = 0
    for entry in plan.entries:
        try:
            result: MemoryArtifactAcceptanceResult = await _run_with_optional_timeout(
                accept_canonical_memory_entry(
                    db,
                    body=entry,
                    signing_key=signing_key,
                ),
                timeout_seconds=item_timeout_seconds,
            )
        except TimeoutError as exc:
            if hasattr(db, "rollback"):
                await db.rollback()
            return (
                facts_submitted,
                facts_queued,
                facts_existing,
                _failure_for_item(plan.item, reason="timeout", error=exc),
            )
        except Exception as exc:
            if hasattr(db, "rollback"):
                await db.rollback()
            return (
                facts_submitted,
                facts_queued,
                facts_existing,
                _failure_for_item(plan.item, reason="submit_failed", error=exc),
            )
        facts_submitted += 1
        if result.enqueue_requested:
            facts_queued += 1
        else:
            facts_existing += 1
    return facts_submitted, facts_queued, facts_existing, None


async def backfill_conversation_facts(
    db: AsyncSession,
    *,
    tenant_id: str,
    limit: int = DEFAULT_FACT_LIMIT,
    max_facts_per_item: int = DEFAULT_MAX_FACTS_PER_ITEM,
    dry_run: bool = True,
    signing_key: str | None = None,
    workers: int = DEFAULT_BACKFILL_WORKERS,
    item_timeout_seconds: float | None = None,
) -> ConversationFactBackfillResult:
    items = (
        await db.execute(
            select(Item)
            .where(Item.tenant_id == tenant_id)
            .where(Item.status == "ready")
            .where(Item.deleted_at.is_(None))
            .where(Item.raw_content.is_not(None))
            .where(Item.metadata_.has_key("memory_entry"))  # noqa: W601 - SQLAlchemy JSONB operator
            .where(~Item.metadata_.has_key("conversation_fact"))  # noqa: W601 - SQLAlchemy JSONB operator
            .where(Item.metadata_["memory_entry"]["source"].astext != CONVERSATION_FACT_SOURCE)
            .order_by(Item.updated_at.asc(), Item.id.asc())
            .limit(limit)
        )
    ).scalars().all()

    plans, failures, worker_count = await _build_item_plans(
        list(items),
        max_facts_per_item=max_facts_per_item,
        workers=workers,
        item_timeout_seconds=item_timeout_seconds,
    )
    facts_discovered = sum(len(plan.entries) for plan in plans)
    facts_submitted = 0
    facts_queued = 0
    facts_existing = 0
    completed_item_ids: set[uuid.UUID] = set()
    if not dry_run:
        # SQLAlchemy AsyncSession is not concurrency-safe, so DB writes stay
        # sequential while discovery/parsing above is worker-bounded.
        for plan in plans:
            submitted, queued, existing, failure = await _submit_item_plan(
                db,
                plan=plan,
                signing_key=signing_key,
                item_timeout_seconds=item_timeout_seconds,
            )
            facts_submitted += submitted
            facts_queued += queued
            facts_existing += existing
            if failure is not None:
                failures.append(failure)
                continue
            completed_item_ids.add(plan.item.id)
    else:
        completed_item_ids = {plan.item.id for plan in plans}

    failure_item_ids = {
        uuid.UUID(str(failure["source_item_id"]))
        for failure in failures
        if isinstance(failure.get("source_item_id"), str)
    }
    skipped_count = sum(
        1
        for item in items
        if item.id not in completed_item_ids and item.id not in failure_item_ids and not _is_source_memory_entry(item)
    )

    return ConversationFactBackfillResult(
        items_scanned=len(items),
        items_completed=len(completed_item_ids),
        items_skipped=skipped_count,
        items_failed=len(failure_item_ids),
        facts_discovered=facts_discovered,
        facts_submitted=facts_submitted,
        facts_queued=facts_queued,
        facts_existing=facts_existing,
        dry_run=dry_run,
        worker_count=worker_count,
        failures=tuple(failures),
    )
