from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import ARRAY, Text, and_, bindparam, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.embedding import Embedding
from app.models.item import Item
from app.models.palace import RetrievalHintArtifact, Room, RoomMembership
from app.schemas.search import SearchResult
from app.services.memory_entries import source_project_from_memory_metadata

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9.-]*")
_MAX_HINT_TEXT_CHARS = 900


@dataclass(frozen=True)
class RetrievalHintCandidate:
    item_id: uuid.UUID
    room_id: uuid.UUID
    source_chunk_index: int
    generation: int
    score: float
    already_returned: bool

    def as_trace_row(self) -> dict[str, Any]:
        return {
            "item_id": str(self.item_id),
            "room_id": str(self.room_id),
            "source_chunk_index": self.source_chunk_index,
            "generation": self.generation,
            "score": round(self.score, 4),
            "already_returned": self.already_returned,
        }


def tokenize_hint_text(value: str) -> set[str]:
    return {token for token in _TOKEN_RE.findall(value.lower()) if len(token) > 1}


def retrieval_hint_fingerprint(*parts: str | None) -> str:
    joined = "\n".join(part or "" for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def build_retrieval_hint_text(item: Item, chunk_text: str | None) -> str:
    metadata = item.metadata_ or {}
    memory_entry = metadata.get("memory_entry") if isinstance(metadata.get("memory_entry"), dict) else {}
    source = memory_entry.get("source") if isinstance(memory_entry, dict) else None
    scope = memory_entry.get("scope") if isinstance(memory_entry, dict) else None
    parts = [
        item.title,
        item.summary,
        item.source_type,
        item.source_url,
        " ".join(item.tags or []),
        f"source:{source}" if source else None,
        f"scope:{scope.get('type')}:{scope.get('key')}" if isinstance(scope, dict) else None,
        chunk_text,
    ]
    compact = " ".join(part.strip() for part in parts if isinstance(part, str) and part.strip())
    return compact[:_MAX_HINT_TEXT_CHARS]


def build_retrieval_hint_artifact(
    *,
    tenant_id: str,
    room_id: uuid.UUID,
    generation: int,
    item: Item,
    source_chunk_index: int,
    chunk_text: str | None,
) -> RetrievalHintArtifact:
    hint_text = build_retrieval_hint_text(item, chunk_text)
    return RetrievalHintArtifact(
        tenant_id=tenant_id,
        room_id=room_id,
        source_item_id=item.id,
        source_chunk_index=source_chunk_index,
        generation=generation,
        hint_text=hint_text,
        source_fingerprint=retrieval_hint_fingerprint(str(item.id), str(source_chunk_index), hint_text),
        metadata_json={
            "title": item.title,
            "source_type": item.source_type,
            "tags": item.tags or [],
            "source_url": item.source_url,
        },
    )


async def rebuild_room_retrieval_hints(
    db: AsyncSession,
    *,
    tenant_id: str,
    room_id: uuid.UUID,
    generation: int,
    room: Room | None = None,
    limit: int = 50,
) -> list[RetrievalHintArtifact]:
    room = room or await db.get(Room, room_id)
    if room is None:
        raise ValueError(f"Room {room_id} not found")

    chunk_rank = (
        func.row_number()
        .over(partition_by=Embedding.item_id, order_by=Embedding.chunk_index)
        .label("chunk_rank")
    )
    chunk_subquery = (
        select(
            Embedding.item_id.label("item_id"),
            Embedding.chunk_index.label("chunk_index"),
            Embedding.chunk_text.label("chunk_text"),
            chunk_rank,
        )
        .subquery()
    )
    rows = (
        await db.execute(
            select(Item, chunk_subquery.c.chunk_index, chunk_subquery.c.chunk_text)
            .join(RoomMembership, RoomMembership.item_id == Item.id)
            .outerjoin(
                chunk_subquery,
                and_(chunk_subquery.c.item_id == Item.id, chunk_subquery.c.chunk_rank == 1),
            )
            .where(RoomMembership.tenant_id == tenant_id)
            .where(RoomMembership.room_id == room_id)
            .where(Item.tenant_id == tenant_id)
            .where(Item.status == "ready")
            .where(Item.deleted_at.is_(None))
            .order_by(RoomMembership.source.desc(), Item.created_at.desc())
            .limit(limit)
        )
    ).all()

    await db.execute(
        delete(RetrievalHintArtifact)
        .where(RetrievalHintArtifact.tenant_id == tenant_id)
        .where(RetrievalHintArtifact.room_id == room_id)
        .where(RetrievalHintArtifact.generation == generation)
    )
    hints = [
        build_retrieval_hint_artifact(
            tenant_id=tenant_id,
            room_id=room_id,
            generation=generation,
            item=item,
            source_chunk_index=int(chunk_index or 0),
            chunk_text=chunk_text,
        )
        for item, chunk_index, chunk_text in rows
    ]
    for hint in hints:
        db.add(hint)
    room.retrieval_hint_generation = generation
    return hints


def score_retrieval_hint(query: str, hint_text: str) -> float:
    query_tokens = tokenize_hint_text(query)
    if not query_tokens:
        return 0.0
    hint_tokens = tokenize_hint_text(hint_text)
    if not hint_tokens:
        return 0.0
    matched = query_tokens & hint_tokens
    return len(matched) / len(query_tokens)


async def report_retrieval_hint_candidates(
    db: AsyncSession,
    *,
    tenant_id: str,
    query: str,
    current_results: list[SearchResult],
    room_ids: list[uuid.UUID] | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    returned_item_ids = {result.item_id for result in current_results}
    statement = (
        select(RetrievalHintArtifact)
        .join(Room, Room.id == RetrievalHintArtifact.room_id)
        .where(RetrievalHintArtifact.tenant_id == tenant_id)
        .where(Room.tenant_id == tenant_id)
        .where(Room.state == "active")
        .where(Room.retrieval_hint_generation == RetrievalHintArtifact.generation)
        .order_by(RetrievalHintArtifact.updated_at.desc())
        .limit(max(limit * 20, 20))
    )
    if room_ids:
        statement = statement.where(RetrievalHintArtifact.room_id.in_(room_ids))

    hints = (await db.execute(statement)).scalars().all()
    candidates = [
        RetrievalHintCandidate(
            item_id=hint.source_item_id,
            room_id=hint.room_id,
            source_chunk_index=hint.source_chunk_index,
            generation=hint.generation,
            score=score,
            already_returned=hint.source_item_id in returned_item_ids,
        )
        for hint in hints
        if (score := score_retrieval_hint(query, hint.hint_text)) > 0
    ]
    candidates.sort(key=lambda candidate: (candidate.score, not candidate.already_returned), reverse=True)
    would_add = [candidate for candidate in candidates if not candidate.already_returned]
    return {
        "report_enabled": True,
        "applied": False,
        "applied_count": 0,
        "candidate_count": len(candidates),
        "would_add_count": len(would_add),
        "candidates": [candidate.as_trace_row() for candidate in candidates[:limit]],
    }


async def retrieve_retrieval_hint_rescue_results(
    db: AsyncSession,
    *,
    tenant_id: str,
    query: str,
    current_results: list[SearchResult],
    room_ids: list[uuid.UUID] | None = None,
    scope_type: str | None = None,
    scope_key: str | None = None,
    tags: list[str] | None = None,
    tags_mode: str = "any",
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    min_score: float = 0.8,
    limit: int = 3,
) -> list[SearchResult]:
    returned_item_ids = {result.item_id for result in current_results}
    statement = (
        select(
            RetrievalHintArtifact,
            Item,
            Embedding.chunk_index,
            Embedding.chunk_text,
        )
        .join(Room, Room.id == RetrievalHintArtifact.room_id)
        .join(Item, Item.id == RetrievalHintArtifact.source_item_id)
        .outerjoin(
            Embedding,
            and_(
                Embedding.item_id == RetrievalHintArtifact.source_item_id,
                Embedding.chunk_index == RetrievalHintArtifact.source_chunk_index,
            ),
        )
        .where(RetrievalHintArtifact.tenant_id == tenant_id)
        .where(Room.tenant_id == tenant_id)
        .where(Room.state == "active")
        .where(Room.retrieval_hint_generation == RetrievalHintArtifact.generation)
        .where(Item.tenant_id == tenant_id)
        .where(Item.status == "ready")
        .where(Item.deleted_at.is_(None))
        .order_by(RetrievalHintArtifact.updated_at.desc())
        .limit(max(limit * 20, 20))
    )
    if returned_item_ids:
        statement = statement.where(RetrievalHintArtifact.source_item_id.not_in(returned_item_ids))
    if room_ids:
        statement = statement.where(RetrievalHintArtifact.room_id.in_(room_ids))
    if scope_type is not None:
        memory_scope_type = Item.metadata_["memory_entry"]["scope"]["type"].as_string()
        if scope_type == "tenant_shared":
            statement = statement.where(or_(memory_scope_type.is_(None), memory_scope_type == "tenant_shared"))
        else:
            statement = statement.where(memory_scope_type == scope_type)
    if scope_key is not None:
        statement = statement.where(Item.metadata_["memory_entry"]["scope"]["key"].as_string() == scope_key)
    if tags:
        tag_param = bindparam("retrieval_hint_rescue_tags", tags, type_=ARRAY(Text))
        if tags_mode == "all":
            statement = statement.where(Item.tags.op("@>")(tag_param))
        else:
            statement = statement.where(Item.tags.op("&&")(tag_param))
    if date_from is not None:
        statement = statement.where(Item.created_at >= date_from)
    if date_to is not None:
        statement = statement.where(Item.created_at <= date_to)

    rows = (await db.execute(statement)).all()
    best_by_item_id: dict[uuid.UUID, tuple[float, Item, int, str]] = {}
    for hint, item, chunk_index, chunk_text in rows:
        if item.id in returned_item_ids:
            continue
        score = score_retrieval_hint(query, hint.hint_text)
        if score < min_score:
            continue
        existing = best_by_item_id.get(item.id)
        if existing is not None and existing[0] >= score:
            continue
        best_by_item_id[item.id] = (
            score,
            item,
            int(chunk_index if chunk_index is not None else hint.source_chunk_index),
            chunk_text or hint.hint_text,
        )

    rescue_rows = sorted(best_by_item_id.values(), key=lambda row: row[0], reverse=True)[:limit]
    return [
        SearchResult(
            item_id=item.id,
            title=item.title,
            summary=item.summary,
            source_type=item.source_type,
            source_url=item.source_url,
            tags=list(item.tags or []),
            source_project=source_project_from_memory_metadata(item.metadata_),
            created_at=item.created_at,
            chunk_text=chunk_text,
            chunk_index=chunk_index,
            score=round(score, 6),
        )
        for score, item, chunk_index, chunk_text in rescue_rows
    ]


async def score_retrieval_hints_for_items(
    db: AsyncSession,
    *,
    tenant_id: str,
    query: str,
    candidate_item_ids: list[uuid.UUID],
    room_ids: list[uuid.UUID] | None = None,
    limit: int = 100,
) -> dict[uuid.UUID, float]:
    if not candidate_item_ids:
        return {}
    statement = (
        select(RetrievalHintArtifact)
        .join(Room, Room.id == RetrievalHintArtifact.room_id)
        .where(RetrievalHintArtifact.tenant_id == tenant_id)
        .where(Room.tenant_id == tenant_id)
        .where(Room.state == "active")
        .where(Room.retrieval_hint_generation == RetrievalHintArtifact.generation)
        .where(RetrievalHintArtifact.source_item_id.in_(candidate_item_ids))
        .order_by(RetrievalHintArtifact.updated_at.desc())
        .limit(max(limit, len(candidate_item_ids)))
    )
    if room_ids:
        statement = statement.where(RetrievalHintArtifact.room_id.in_(room_ids))

    scores: dict[uuid.UUID, float] = {}
    hints = (await db.execute(statement)).scalars().all()
    for hint in hints:
        score = score_retrieval_hint(query, hint.hint_text)
        if score <= 0:
            continue
        scores[hint.source_item_id] = max(scores.get(hint.source_item_id, 0.0), score)
    return scores
