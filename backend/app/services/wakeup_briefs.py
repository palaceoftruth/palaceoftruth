from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.item import Item
from app.models.palace import PalaceTenantState, Room, RoomMembership, RoomSnapshot, TemporalFact, Wing
from app.services.item_processing import process_prebuilt_item

WAKEUP_BRIEF_SOURCE = "palace-wakeup-brief"
WAKEUP_BRIEF_MAX_TENANT_ROOMS = 4
WAKEUP_BRIEF_MAX_WING_ROOMS = 3
WAKEUP_BRIEF_MAX_DIARIES = 3
WAKEUP_BRIEF_MAX_FACTS = 5
WAKEUP_BRIEF_SCOPE_TYPES = {"tenant", "wing"}


@dataclass(frozen=True)
class WakeupBriefKey:
    day: date
    scope_type: str
    scope_key: str | None


@dataclass(frozen=True)
class WakeupBriefBatchResult:
    created: int
    updated: int
    unchanged: int
    deactivated: int


@dataclass(frozen=True)
class WakeupRoomContext:
    room_id: uuid.UUID
    wing_slug: str
    wing_name: str
    room_name: str
    item_count: int
    summary: str
    updated_at: datetime


@dataclass(frozen=True)
class WakeupDiaryContext:
    item_id: uuid.UUID
    title: str
    summary: str | None
    raw_content: str
    updated_at: datetime


@dataclass(frozen=True)
class WakeupFactContext:
    fact_id: uuid.UUID
    source_item_id: uuid.UUID
    source_item_title: str
    subject: str
    predicate: str
    object_text: str
    extracted_at: datetime
    valid_from: datetime | None
    valid_to: datetime | None


@dataclass(frozen=True)
class _WakeupBriefSummaryRow:
    title: str | None
    metadata_: dict[str, Any]
    updated_at: datetime


def _normalize_created_at(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _tokenize(*values: str | None) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        if not value:
            continue
        normalized = "".join(ch.lower() if ch.isalnum() else " " for ch in value)
        tokens.update(part for part in normalized.split() if len(part) > 2)
    return tokens


def build_wakeup_brief_idempotency_key(*, tenant_id: str, key: WakeupBriefKey) -> str:
    identity = {
        "day": key.day.isoformat(),
        "scope_key": key.scope_key,
        "scope_type": key.scope_type,
        "source": WAKEUP_BRIEF_SOURCE,
        "tenant_id": tenant_id,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _is_wakeup_brief(item: Item) -> bool:
    brief = (item.metadata_ or {}).get("wakeup_brief")
    return isinstance(brief, dict)


def _wakeup_brief_key_from_item(item: Item) -> WakeupBriefKey | None:
    brief = (item.metadata_ or {}).get("wakeup_brief")
    if not isinstance(brief, dict):
        return None
    day = brief.get("day")
    scope_type = brief.get("scope_type")
    scope_key = brief.get("scope_key")
    if not isinstance(day, str) or scope_type not in WAKEUP_BRIEF_SCOPE_TYPES:
        return None
    try:
        parsed_day = date.fromisoformat(day)
    except ValueError:
        return None
    return WakeupBriefKey(
        day=parsed_day,
        scope_type=scope_type,
        scope_key=scope_key if isinstance(scope_key, str) else None,
    )


def _brief_scope_label(key: WakeupBriefKey) -> str:
    if key.scope_type == "tenant":
        return "Tenant"
    return f"Wing: {key.scope_key or 'unknown'}"


def _brief_title(key: WakeupBriefKey) -> str:
    if key.scope_type == "tenant":
        return f"Wake-up Brief {key.day.isoformat()} [tenant]"
    return f"Wake-up Brief {key.day.isoformat()} [wing:{key.scope_key}]"


def _brief_summary(
    key: WakeupBriefKey,
    *,
    room_count: int,
    diary_count: int,
    fact_count: int,
) -> str:
    return (
        f"Startup context for {_brief_scope_label(key).lower()} from {room_count} rooms, "
        f"{diary_count} diary rollups, and {fact_count} active facts."
    )


def _brief_source_url(key: WakeupBriefKey) -> str:
    scope_part = key.scope_key or "tenant"
    return f"memory://wakeup-brief/{key.scope_type}/{scope_part}/{key.day.isoformat()}"


def _brief_path(key: WakeupBriefKey) -> str:
    scope_part = key.scope_key or "tenant"
    return f"wake-up/{key.day.isoformat()}/{key.scope_type}-{scope_part}.md"


def _brief_tags(key: WakeupBriefKey) -> list[str]:
    tags = [
        "wake-up-brief",
        f"wake-up-day-{key.day.isoformat()}",
        f"brief-scope-{key.scope_type}",
    ]
    if key.scope_key:
        tags.append(f"wing-{key.scope_key}")
    return tags


def _format_fact_window(fact: WakeupFactContext) -> str:
    if fact.valid_from and fact.valid_to:
        return f"{fact.valid_from.date().isoformat()} to {fact.valid_to.date().isoformat()}"
    if fact.valid_from:
        return f"from {fact.valid_from.date().isoformat()}"
    if fact.valid_to:
        return f"until {fact.valid_to.date().isoformat()}"
    return "current"


def _render_wakeup_body(
    key: WakeupBriefKey,
    *,
    generation: int,
    rooms: list[WakeupRoomContext],
    diaries: list[WakeupDiaryContext],
    facts: list[WakeupFactContext],
) -> str:
    lines = [
        f"# Wake-up Brief: {key.day.isoformat()}",
        "",
        f"Scope: {_brief_scope_label(key)}",
        f"Palace generation: {generation}",
        "",
        "This bounded startup brief highlights the rooms, diary rollups, and facts most worth loading first.",
        "",
        "## High-signal rooms",
    ]
    if rooms:
        for room in rooms:
            lines.append(
                f"- {room.wing_name} / {room.room_name} ({room.item_count} items): {room.summary}"
            )
    else:
        lines.append("- No current room snapshots were available.")
    lines.extend(["", "## Recent diary rollups"])
    if diaries:
        for diary in diaries:
            detail = diary.summary or (diary.raw_content.strip().splitlines()[0] if diary.raw_content.strip() else "No summary.")
            lines.append(f"- {diary.title}: {detail}")
    else:
        lines.append("- No recent diary rollups were available.")
    lines.extend(["", "## Current facts"])
    if facts:
        for fact in facts:
            lines.append(
                f"- {fact.subject} {fact.predicate} {fact.object_text} ({_format_fact_window(fact)}; source: {fact.source_item_title})"
            )
    else:
        lines.append("- No active temporal facts were available.")
    return "\n".join(lines).strip()


def _brief_metadata(
    *,
    key: WakeupBriefKey,
    idempotency_key: str,
    generation: int,
    rooms: list[WakeupRoomContext],
    diaries: list[WakeupDiaryContext],
    facts: list[WakeupFactContext],
) -> dict:
    return {
        "memory_entry": {
            "schema_version": 1,
            "source": WAKEUP_BRIEF_SOURCE,
            "source_url": _brief_source_url(key),
            "created_at": datetime.combine(key.day, time(hour=6, minute=0, tzinfo=timezone.utc)).isoformat(),
            "created_by_role": "system",
            "scope": {"type": "tenant_shared", "key": None},
            "metadata": {
                "wakeup_brief": {
                    "room_count": len(rooms),
                    "diary_count": len(diaries),
                    "fact_count": len(facts),
                }
            },
            "idempotency_key": idempotency_key,
            "source_type": "note",
        },
        "wakeup_brief": {
            "schema_version": 1,
            "day": key.day.isoformat(),
            "scope_type": key.scope_type,
            "scope_key": key.scope_key,
            "generation": generation,
            "room_ids": [str(room.room_id) for room in rooms],
            "diary_item_ids": [str(diary.item_id) for diary in diaries],
            "fact_ids": [str(fact.fact_id) for fact in facts],
            "room_count": len(rooms),
            "diary_count": len(diaries),
            "fact_count": len(facts),
        },
        "sync_relative_path": _brief_path(key),
    }


def _brief_matches(item: Item, *, body: str, summary: str, tags: list[str], metadata: dict) -> bool:
    return (
        item.raw_content == body
        and item.summary == summary
        and item.tags == tags
        and item.metadata_ == metadata
    )


def _score_context(tokens: set[str], *values: str | None) -> int:
    if not tokens:
        return 0
    return len(tokens & _tokenize(*values))


def _select_diaries(
    diaries: list[WakeupDiaryContext],
    *,
    scope_tokens: set[str],
) -> list[WakeupDiaryContext]:
    ranked = sorted(
        diaries,
        key=lambda diary: (
            _score_context(scope_tokens, diary.title, diary.summary, diary.raw_content),
            _normalize_created_at(diary.updated_at),
        ),
        reverse=True,
    )
    return ranked[:WAKEUP_BRIEF_MAX_DIARIES]


def _select_facts(
    facts: list[WakeupFactContext],
    *,
    scope_tokens: set[str],
) -> list[WakeupFactContext]:
    ranked = sorted(
        facts,
        key=lambda fact: (
            _score_context(scope_tokens, fact.source_item_title, fact.subject, fact.object_text),
            _normalize_created_at(fact.extracted_at),
        ),
        reverse=True,
    )
    return ranked[:WAKEUP_BRIEF_MAX_FACTS]


async def _list_wakeup_rooms(
    db: AsyncSession,
    *,
    tenant_id: str,
    generation: int,
) -> list[WakeupRoomContext]:
    rows = (
        await db.execute(
            select(
                Room.id,
                Wing.slug,
                Wing.name,
                Room.name,
                func.count(RoomMembership.id),
                RoomSnapshot.summary,
                RoomSnapshot.updated_at,
            )
            .join(Wing, Wing.id == Room.wing_id)
            .join(
                RoomSnapshot,
                (RoomSnapshot.room_id == Room.id) & (RoomSnapshot.generation == generation),
            )
            .outerjoin(
                RoomMembership,
                (RoomMembership.room_id == Room.id) & (RoomMembership.tenant_id == tenant_id),
            )
            .where(Room.tenant_id == tenant_id)
            .where(Room.state == "active")
            .group_by(
                Room.id,
                Wing.slug,
                Wing.name,
                Room.name,
                RoomSnapshot.summary,
                RoomSnapshot.updated_at,
            )
            .order_by(func.count(RoomMembership.id).desc(), RoomSnapshot.updated_at.desc())
        )
    ).all()
    return [
        WakeupRoomContext(
            room_id=room_id,
            wing_slug=wing_slug,
            wing_name=wing_name,
            room_name=room_name,
            item_count=item_count,
            summary=summary,
            updated_at=updated_at,
        )
        for room_id, wing_slug, wing_name, room_name, item_count, summary, updated_at in rows
        if isinstance(summary, str) and summary.strip()
    ]


async def _list_recent_diary_rollups(
    db: AsyncSession,
    *,
    tenant_id: str,
) -> list[WakeupDiaryContext]:
    rows = (
        await db.execute(
            select(Item)
            .where(Item.tenant_id == tenant_id)
            .where(Item.status == "ready")
            .where(Item.deleted_at.is_(None))
            .order_by(Item.updated_at.desc(), Item.id.desc())
        )
    ).scalars().all()
    return [
        WakeupDiaryContext(
            item_id=item.id,
            title=item.title,
            summary=item.summary,
            raw_content=item.raw_content or "",
            updated_at=item.updated_at,
        )
        for item in rows
        if isinstance((item.metadata_ or {}).get("diary_rollup"), dict)
    ][:12]


async def _list_active_facts(
    db: AsyncSession,
    *,
    tenant_id: str,
) -> list[WakeupFactContext]:
    rows = (
        await db.execute(
            select(
                TemporalFact.id,
                TemporalFact.source_item_id,
                Item.title,
                TemporalFact.subject,
                TemporalFact.predicate,
                TemporalFact.object_text,
                TemporalFact.extracted_at,
                TemporalFact.valid_from,
                TemporalFact.valid_to,
            )
            .join(Item, Item.id == TemporalFact.source_item_id)
            .where(TemporalFact.tenant_id == tenant_id)
            .where(TemporalFact.status == "active")
            .order_by(TemporalFact.extracted_at.desc(), TemporalFact.id.desc())
            .limit(24)
        )
    ).all()
    return [
        WakeupFactContext(
            fact_id=fact_id,
            source_item_id=source_item_id,
            source_item_title=source_item_title,
            subject=subject,
            predicate=predicate,
            object_text=object_text,
            extracted_at=extracted_at,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        for fact_id, source_item_id, source_item_title, subject, predicate, object_text, extracted_at, valid_from, valid_to in rows
    ]


def _build_scope_tokens(*, key: WakeupBriefKey, rooms: list[WakeupRoomContext]) -> set[str]:
    if key.scope_type == "tenant":
        return _tokenize(*(f"{room.wing_name} {room.room_name}" for room in rooms))
    return _tokenize(key.scope_key, *(room.room_name for room in rooms), *(room.wing_name for room in rooms))


async def generate_wakeup_briefs(
    db: AsyncSession,
    *,
    tenant_id: str,
    embedder,
    llm,
    target_day: date | None = None,
) -> WakeupBriefBatchResult:
    state = await db.get(PalaceTenantState, tenant_id)
    if state is None or state.indexed_generation <= 0:
        return WakeupBriefBatchResult(created=0, updated=0, unchanged=0, deactivated=0)

    brief_day = target_day or datetime.now(timezone.utc).date()
    generation = state.indexed_generation

    rooms = await _list_wakeup_rooms(db, tenant_id=tenant_id, generation=generation)
    diaries = await _list_recent_diary_rollups(db, tenant_id=tenant_id)
    facts = await _list_active_facts(db, tenant_id=tenant_id)

    existing_rows = (
        await db.execute(
            select(Item)
            .where(Item.tenant_id == tenant_id)
            .order_by(Item.updated_at.desc(), Item.id.desc())
        )
    ).scalars().all()
    existing_briefs: dict[WakeupBriefKey, Item] = {}
    for item in existing_rows:
        key = _wakeup_brief_key_from_item(item)
        if key is not None and key.day == brief_day:
            existing_briefs[key] = item

    desired_scopes: list[tuple[WakeupBriefKey, list[WakeupRoomContext]]] = []
    desired_scopes.append(
        (
            WakeupBriefKey(day=brief_day, scope_type="tenant", scope_key=None),
            rooms[:WAKEUP_BRIEF_MAX_TENANT_ROOMS],
        )
    )
    by_wing: dict[str, list[WakeupRoomContext]] = {}
    wing_names: dict[str, str] = {}
    for room in rooms:
        by_wing.setdefault(room.wing_slug, []).append(room)
        wing_names[room.wing_slug] = room.wing_name
    for wing_slug, scoped_rooms in by_wing.items():
        desired_scopes.append(
            (
                WakeupBriefKey(day=brief_day, scope_type="wing", scope_key=wing_slug),
                scoped_rooms[:WAKEUP_BRIEF_MAX_WING_ROOMS],
            )
        )

    created = 0
    updated = 0
    unchanged = 0
    deactivated = 0
    desired_keys = {key for key, _rooms in desired_scopes}

    for key, scoped_rooms in desired_scopes:
        scope_tokens = _build_scope_tokens(key=key, rooms=scoped_rooms)
        scoped_diaries = _select_diaries(diaries, scope_tokens=scope_tokens)
        scoped_facts = _select_facts(facts, scope_tokens=scope_tokens)
        idempotency_key = build_wakeup_brief_idempotency_key(tenant_id=tenant_id, key=key)
        title = _brief_title(key)
        summary = _brief_summary(
            key,
            room_count=len(scoped_rooms),
            diary_count=len(scoped_diaries),
            fact_count=len(scoped_facts),
        )
        body = _render_wakeup_body(
            key,
            generation=generation,
            rooms=scoped_rooms,
            diaries=scoped_diaries,
            facts=scoped_facts,
        )
        metadata = _brief_metadata(
            key=key,
            idempotency_key=idempotency_key,
            generation=generation,
            rooms=scoped_rooms,
            diaries=scoped_diaries,
            facts=scoped_facts,
        )
        tags = _brief_tags(key)

        brief = existing_briefs.get(key)
        if brief is not None and _brief_matches(brief, body=body, summary=summary, tags=tags, metadata=metadata):
            unchanged += 1
            continue

        is_new = brief is None
        if brief is None:
            brief = Item(
                source_type="note",
                source_url=_brief_source_url(key),
                title=title,
                summary=summary,
                raw_content=body,
                metadata_=metadata,
                tags=tags,
                categories=["brief"],
                tenant_id=tenant_id,
                status="processing",
                created_at=datetime.combine(brief_day, time(hour=6, minute=0, tzinfo=timezone.utc)),
                updated_at=datetime.now(timezone.utc),
                idempotency_key=idempotency_key,
            )
            db.add(brief)
            await db.flush()
            existing_briefs[key] = brief
        else:
            brief.source_url = _brief_source_url(key)
            brief.title = title
            brief.summary = summary
            brief.raw_content = body
            brief.metadata_ = metadata
            brief.tags = tags
            brief.categories = ["brief"]
            brief.status = "processing"
            brief.idempotency_key = idempotency_key
            brief.updated_at = datetime.now(timezone.utc)

        await process_prebuilt_item(
            db,
            item=brief,
            embedder=embedder,
            llm=llm,
            tenant_id=tenant_id,
            enable_ai_enrichment=False,
        )
        # Wake-up briefs are derived from the current Palace generation. Marking
        # them dirty would trigger a self-invalidating follow-up Palace rebuild.
        await db.commit()
        if is_new:
            created += 1
        else:
            updated += 1

    for key, brief in existing_briefs.items():
        if key in desired_keys or brief.status == "failed":
            continue
        brief.status = "failed"
        brief.updated_at = datetime.now(timezone.utc)
        await db.commit()
        deactivated += 1

    return WakeupBriefBatchResult(
        created=created,
        updated=updated,
        unchanged=unchanged,
        deactivated=deactivated,
    )


async def build_wakeup_brief_summary(
    db: AsyncSession,
    *,
    tenant_id: str,
    indexed_generation: int,
    today: date | None = None,
) -> dict[str, object]:
    brief_day = today or datetime.now(timezone.utc).date()
    rows = [
        _WakeupBriefSummaryRow(
            title=_row_value(row, "title", 0),
            metadata_=_row_value(row, "metadata_", 1) or {},
            updated_at=_row_value(row, "updated_at", 2),
        )
        for row in (await db.execute(wakeup_brief_summary_statement(tenant_id=tenant_id))).all()
    ]

    relevant: list[dict[str, object]] = []
    last_refreshed_at = None
    fresh = 0
    stale = 0
    for row in rows:
        brief = (row.metadata_ or {}).get("wakeup_brief")
        if not isinstance(brief, dict):
            continue
        if brief.get("day") != brief_day.isoformat():
            continue
        scope_type = brief.get("scope_type")
        scope_key = brief.get("scope_key")
        generation = int(brief.get("generation", 0))
        updated_at = row.updated_at
        if last_refreshed_at is None or updated_at > last_refreshed_at:
            last_refreshed_at = updated_at
        is_stale = generation < indexed_generation
        if is_stale:
            stale += 1
        else:
            fresh += 1
        relevant.append(
            {
                "title": row.title,
                "scope_type": scope_type,
                "scope_key": scope_key,
                "generation": generation,
                "updated_at": updated_at,
                "room_count": int(brief.get("room_count", 0)),
                "diary_count": int(brief.get("diary_count", 0)),
                "fact_count": int(brief.get("fact_count", 0)),
                "stale": is_stale,
            }
        )

    return {
        "fresh": fresh,
        "stale": stale,
        "generated_for_day": brief_day,
        "last_refreshed_at": last_refreshed_at,
        "recent_briefs": relevant[:8],
    }


def wakeup_brief_summary_statement(*, tenant_id: str) -> Select:
    return (
        select(Item.title, Item.metadata_, Item.updated_at)
        .where(Item.tenant_id == tenant_id)
        .where(Item.status == "ready")
        .where(Item.deleted_at.is_(None))
        .where(Item.metadata_.has_key("wakeup_brief"))
        .order_by(Item.updated_at.desc(), Item.id.desc())
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
