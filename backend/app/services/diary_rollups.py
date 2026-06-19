from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.item import Item
from app.services.item_processing import process_prebuilt_item

DIARY_ROLLUP_SOURCE = "palace-diary-rollup"
DIARY_ROLLUP_SCOPE_TYPES = {"session", "agent", "workspace"}


@dataclass(frozen=True)
class DiaryRollupKey:
    day: date
    scope_type: str
    scope_key: str | None


@dataclass(frozen=True)
class DiaryRollupBatchResult:
    created: int
    updated: int
    unchanged: int
    deactivated: int


async def mark_item_dirty(*args, **kwargs):
    from app.services.palace import mark_item_dirty as palace_mark_item_dirty

    return await palace_mark_item_dirty(*args, **kwargs)


def build_diary_rollup_idempotency_key(*, tenant_id: str, key: DiaryRollupKey) -> str:
    identity = {
        "day": key.day.isoformat(),
        "scope_key": key.scope_key,
        "scope_type": key.scope_type,
        "source": DIARY_ROLLUP_SOURCE,
        "tenant_id": tenant_id,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _normalize_created_at(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _memory_scope(item: Item) -> tuple[str, str | None] | None:
    memory_entry = (item.metadata_ or {}).get("memory_entry")
    if not isinstance(memory_entry, dict):
        return None
    scope = memory_entry.get("scope")
    if not isinstance(scope, dict):
        return None
    scope_type = scope.get("type")
    scope_key = scope.get("key")
    if scope_type not in DIARY_ROLLUP_SCOPE_TYPES:
        return None
    return scope_type, scope_key if isinstance(scope_key, str) else None


def _is_diary_rollup(item: Item) -> bool:
    diary_rollup = (item.metadata_ or {}).get("diary_rollup")
    return isinstance(diary_rollup, dict)


def _diary_rollup_key_from_item(item: Item) -> DiaryRollupKey | None:
    diary_rollup = (item.metadata_ or {}).get("diary_rollup")
    if not isinstance(diary_rollup, dict):
        return None
    day = diary_rollup.get("day")
    scope_type = diary_rollup.get("scope_type")
    scope_key = diary_rollup.get("scope_key")
    if not isinstance(day, str) or not isinstance(scope_type, str):
        return None
    try:
        parsed_day = date.fromisoformat(day)
    except ValueError:
        return None
    return DiaryRollupKey(
        day=parsed_day,
        scope_type=scope_type,
        scope_key=scope_key if isinstance(scope_key, str) else None,
    )


def _day_for_item(item: Item) -> date:
    return _normalize_created_at(item.created_at).date()


def _scope_label(key: DiaryRollupKey) -> str:
    if key.scope_key:
        return f"{key.scope_type}:{key.scope_key}"
    return key.scope_type


def _rollup_title(key: DiaryRollupKey) -> str:
    return f"Diary Rollup {key.day.isoformat()} [{_scope_label(key)}]"


def _rollup_summary(key: DiaryRollupKey, *, source_count: int) -> str:
    note_label = "note" if source_count == 1 else "notes"
    return f"Daily scoped diary for {_scope_label(key)} from {source_count} source {note_label}."


def _rollup_source_url(key: DiaryRollupKey) -> str:
    scope_part = key.scope_key or "shared"
    return f"memory://diary-rollup/{key.scope_type}/{scope_part}/{key.day.isoformat()}"


def _rollup_path(key: DiaryRollupKey) -> str:
    scope_suffix = key.scope_key or "shared"
    return f"diaries/{key.day.isoformat()}/{key.scope_type}-{scope_suffix}.md"


def _rollup_tags(key: DiaryRollupKey) -> list[str]:
    tags = [
        "diary-rollup",
        f"diary-day-{key.day.isoformat()}",
        f"scope-{key.scope_type}",
    ]
    if key.scope_key:
        tags.append(f"{key.scope_type}-{key.scope_key}")
    return tags


def _render_rollup_body(key: DiaryRollupKey, source_items: list[Item]) -> str:
    lines = [
        f"# Diary Rollup: {key.day.isoformat()}",
        "",
        f"Scope: {_scope_label(key)}",
        f"Source notes: {len(source_items)}",
        "",
    ]
    for item in source_items:
        created_at = _normalize_created_at(item.created_at).strftime("%H:%M UTC")
        lines.append(f"## {created_at} - {item.title}")
        if item.summary:
            lines.append(item.summary.strip())
        body = (item.raw_content or "").strip()
        if body:
            lines.append(body)
        lines.append("")
    return "\n".join(lines).strip()


def _rollup_metadata(
    *,
    key: DiaryRollupKey,
    idempotency_key: str,
    source_items: list[Item],
) -> dict:
    return {
        "memory_entry": {
            "schema_version": 1,
            "source": DIARY_ROLLUP_SOURCE,
            "source_url": _rollup_source_url(key),
            "created_at": datetime.combine(key.day, time(hour=23, minute=59, tzinfo=timezone.utc)).isoformat(),
            "created_by_role": "system",
            "scope": {"type": key.scope_type, "key": key.scope_key},
            "metadata": {
                "diary_rollup": {
                    "source_count": len(source_items),
                }
            },
            "idempotency_key": idempotency_key,
            "source_type": "note",
        },
        "diary_rollup": {
            "schema_version": 1,
            "day": key.day.isoformat(),
            "scope_type": key.scope_type,
            "scope_key": key.scope_key,
            "source_item_ids": [str(item.id) for item in source_items],
            "source_titles": [item.title for item in source_items],
        },
        # Reuse the Palace sync path heuristic so rollups land in day rooms.
        "sync_relative_path": _rollup_path(key),
    }


def _rollup_matches(item: Item, *, source_ids: list[str], body: str, summary: str, tags: list[str], metadata: dict) -> bool:
    return (
        item.raw_content == body
        and item.summary == summary
        and item.tags == tags
        and item.metadata_ == metadata
        and ((item.metadata_ or {}).get("diary_rollup", {}).get("source_item_ids") == source_ids)
    )


async def generate_memory_diary_rollups(
    db: AsyncSession,
    *,
    tenant_id: str,
    embedder,
    llm,
    target_day: date | None = None,
) -> DiaryRollupBatchResult:
    rows = (
        await db.execute(
            select(Item)
            .where(Item.tenant_id == tenant_id)
            .where(Item.source_type == "note")
            .order_by(Item.created_at.asc())
        )
    ).scalars().all()

    source_groups: dict[DiaryRollupKey, list[Item]] = {}
    existing_rollups: dict[DiaryRollupKey, Item] = {}

    for item in rows:
        if _is_diary_rollup(item):
            key = _diary_rollup_key_from_item(item)
            if key is not None and (target_day is None or key.day == target_day):
                existing_rollups[key] = item
            continue

        if item.status != "ready":
            continue
        scope = _memory_scope(item)
        if scope is None:
            continue
        key = DiaryRollupKey(day=_day_for_item(item), scope_type=scope[0], scope_key=scope[1])
        if target_day is not None and key.day != target_day:
            continue
        source_groups.setdefault(key, []).append(item)

    created = 0
    updated = 0
    unchanged = 0
    deactivated = 0

    for key, source_items in source_groups.items():
        idempotency_key = build_diary_rollup_idempotency_key(tenant_id=tenant_id, key=key)
        title = _rollup_title(key)
        summary = _rollup_summary(key, source_count=len(source_items))
        body = _render_rollup_body(key, source_items)
        metadata = _rollup_metadata(key=key, idempotency_key=idempotency_key, source_items=source_items)
        tags = _rollup_tags(key)
        source_ids = [str(item.id) for item in source_items]

        rollup = existing_rollups.get(key)
        if rollup is not None and _rollup_matches(
            rollup,
            source_ids=source_ids,
            body=body,
            summary=summary,
            tags=tags,
            metadata=metadata,
        ):
            unchanged += 1
            continue

        is_new = rollup is None
        if rollup is None:
            rollup = Item(
                source_type="note",
                source_url=_rollup_source_url(key),
                title=title,
                summary=summary,
                raw_content=body,
                metadata_=metadata,
                tags=tags,
                categories=["diary"],
                tenant_id=tenant_id,
                status="processing",
                created_at=datetime.combine(key.day, time(hour=23, minute=59, tzinfo=timezone.utc)),
                updated_at=datetime.now(timezone.utc),
                idempotency_key=idempotency_key,
            )
            db.add(rollup)
            await db.flush()
            existing_rollups[key] = rollup
        else:
            rollup.source_url = _rollup_source_url(key)
            rollup.title = title
            rollup.summary = summary
            rollup.raw_content = body
            rollup.metadata_ = metadata
            rollup.tags = tags
            rollup.categories = ["diary"]
            rollup.status = "processing"
            rollup.idempotency_key = idempotency_key
            rollup.updated_at = datetime.now(timezone.utc)

        await process_prebuilt_item(
            db,
            item=rollup,
            embedder=embedder,
            llm=llm,
            tenant_id=tenant_id,
            enable_ai_enrichment=False,
        )
        await mark_item_dirty(
            db,
            tenant_id=tenant_id,
            item_id=rollup.id,
            reason="diary-rollup",
        )
        await db.commit()
        if is_new:
            created += 1
        else:
            updated += 1

    for key, rollup in existing_rollups.items():
        if key in source_groups:
            continue
        if target_day is not None and key.day != target_day:
            continue
        if rollup.status == "failed":
            continue
        rollup.status = "failed"
        rollup.updated_at = datetime.now(timezone.utc)
        await mark_item_dirty(
            db,
            tenant_id=tenant_id,
            item_id=rollup.id,
            reason="diary-rollup",
        )
        await db.commit()
        deactivated += 1

    return DiaryRollupBatchResult(
        created=created,
        updated=updated,
        unchanged=unchanged,
        deactivated=deactivated,
    )


async def build_diary_rollup_summary(
    db: AsyncSession,
    *,
    tenant_id: str,
    today: date | None = None,
) -> dict[str, object]:
    expected_through_day = (today or datetime.now(timezone.utc).date()) - timedelta(days=1)
    rows = (
        await db.execute(
            select(Item)
            .where(Item.tenant_id == tenant_id)
            .where(Item.status == "ready")
            .where(Item.deleted_at.is_(None))
            .order_by(Item.updated_at.desc(), Item.id.desc())
        )
    ).scalars().all()

    latest_by_scope: dict[tuple[str, str | None], dict[str, object]] = {}
    last_refreshed_at: datetime | None = None
    for item in rows:
        diary_rollup = (item.metadata_ or {}).get("diary_rollup")
        if not isinstance(diary_rollup, dict):
            continue
        scope_type = diary_rollup.get("scope_type")
        if scope_type not in DIARY_ROLLUP_SCOPE_TYPES:
            continue
        raw_day = diary_rollup.get("day")
        if not isinstance(raw_day, str):
            continue
        try:
            rollup_day = date.fromisoformat(raw_day)
        except ValueError:
            continue
        scope_key = diary_rollup.get("scope_key") if isinstance(diary_rollup.get("scope_key"), str) else None
        source_ids = diary_rollup.get("source_item_ids")
        source_count = len(source_ids) if isinstance(source_ids, list) else 0
        scope = (scope_type, scope_key)
        current = latest_by_scope.get(scope)
        # Keep the newest rollup per scope so operators see coverage, not duplicate history rows.
        if current is not None and isinstance(current.get("day"), date) and current["day"] >= rollup_day:
            continue

        updated_at = item.updated_at
        if last_refreshed_at is None or updated_at > last_refreshed_at:
            last_refreshed_at = updated_at
        latest_by_scope[scope] = {
            "title": item.title,
            "scope_type": scope_type,
            "scope_key": scope_key,
            "day": rollup_day,
            "updated_at": updated_at,
            "source_count": source_count,
            "stale": rollup_day < expected_through_day,
        }

    all_rollups = sorted(
        latest_by_scope.values(),
        key=lambda rollup: (rollup["day"], rollup["updated_at"]),
        reverse=True,
    )
    recent_rollups = all_rollups[:8]
    stale = sum(1 for rollup in all_rollups if rollup["stale"])
    return {
        "fresh": len(all_rollups) - stale,
        "stale": stale,
        "expected_through_day": expected_through_day,
        "last_refreshed_at": last_refreshed_at,
        "recent_rollups": recent_rollups,
    }
