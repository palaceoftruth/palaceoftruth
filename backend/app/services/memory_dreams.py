from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.item import Item
from app.models.job import Job
from app.models.palace import PalaceTenantState, TemporalFact
from app.services.item_processing import process_prebuilt_item

MEMORY_DREAM_SOURCE_PREFIX = "palace-dream"
MEMORY_DREAM_SCHEMA_VERSION = 1
MEMORY_DREAM_PROMPT_VERSION = "deterministic-v1"
MEMORY_DREAM_REPLAY_DAYS = 2
MEMORY_DREAM_SCOPE_TYPES = {"agent", "workspace", "session", "tenant_shared"}
MEMORY_DREAM_ARTIFACT_TYPES = (
    "palace-dream-summary",
    "palace-routing-manifest",
    "palace-hygiene-report",
)
_MAX_SOURCE_ITEMS_PER_ARTIFACT = 40


@dataclass(frozen=True)
class MemoryDreamKey:
    day: date
    scope_type: str
    scope_key: str | None
    artifact_type: str


@dataclass(frozen=True)
class MemoryDreamBatchResult:
    created: int
    updated: int
    unchanged: int
    deactivated: int


@dataclass(frozen=True)
class MemoryDreamSourceContext:
    item: Item
    digest: str
    job_ids: tuple[str, ...]
    contradiction_fact_ids: tuple[str, ...]


def build_memory_dream_idempotency_key(*, tenant_id: str, key: MemoryDreamKey) -> str:
    identity = {
        "artifact_type": key.artifact_type,
        "day": key.day.isoformat(),
        "scope_key": key.scope_key,
        "scope_type": key.scope_type,
        "source": MEMORY_DREAM_SOURCE_PREFIX,
        "tenant_id": tenant_id,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def memory_dream_target_days(*, today: date | None = None, replay_days: int = MEMORY_DREAM_REPLAY_DAYS) -> tuple[date, ...]:
    reference_day = today or datetime.now(timezone.utc).date()
    if replay_days < 1:
        return ()
    return tuple(reference_day - timedelta(days=offset) for offset in range(replay_days, 0, -1))


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
    if scope_type not in MEMORY_DREAM_SCOPE_TYPES:
        return None
    if scope_type == "tenant_shared":
        return scope_type, None
    return scope_type, scope_key if isinstance(scope_key, str) else None


def _is_other_derived_memory(item: Item) -> bool:
    metadata = item.metadata_ or {}
    return any(isinstance(metadata.get(key), dict) for key in ("diary_rollup", "wakeup_brief", "memory_dream"))


def _memory_dream_key_from_item(item: Item) -> MemoryDreamKey | None:
    dream = (item.metadata_ or {}).get("memory_dream")
    if not isinstance(dream, dict):
        return None
    day = dream.get("day")
    scope_type = dream.get("scope_type")
    artifact_type = dream.get("artifact_type")
    if not isinstance(day, str) or scope_type not in MEMORY_DREAM_SCOPE_TYPES or artifact_type not in MEMORY_DREAM_ARTIFACT_TYPES:
        return None
    try:
        parsed_day = date.fromisoformat(day)
    except ValueError:
        return None
    scope_key = dream.get("scope_key")
    return MemoryDreamKey(
        day=parsed_day,
        scope_type=scope_type,
        scope_key=scope_key if isinstance(scope_key, str) else None,
        artifact_type=artifact_type,
    )


def _scope_label(key: MemoryDreamKey) -> str:
    if key.scope_type == "tenant_shared":
        return "tenant_shared"
    return f"{key.scope_type}:{key.scope_key or 'unknown'}"


def _dream_source_url(key: MemoryDreamKey) -> str:
    scope_part = key.scope_key or "shared"
    return f"memory://dream/{key.artifact_type}/{key.scope_type}/{scope_part}/{key.day.isoformat()}"


def _dream_path(key: MemoryDreamKey) -> str:
    scope_part = key.scope_key or "shared"
    return f"dreams/{key.day.isoformat()}/{key.scope_type}-{scope_part}-{key.artifact_type}.md"


def _dream_title(key: MemoryDreamKey) -> str:
    return f"Memory Dream {key.day.isoformat()} [{_scope_label(key)}] {key.artifact_type}"


def _dream_tags(key: MemoryDreamKey) -> list[str]:
    tags = [
        "memory-dream",
        key.artifact_type,
        f"dream-day-{key.day.isoformat()}",
        f"dream-scope-{key.scope_type}",
    ]
    if key.scope_key:
        tags.append(f"{key.scope_type}-{key.scope_key}")
    return tags


def _source_digest(item: Item) -> str:
    if item.content_hash:
        return item.content_hash
    content = {
        "body": item.raw_content or "",
        "summary": item.summary or "",
        "title": item.title,
    }
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _source_fingerprint(item: Item) -> str:
    body = (item.raw_content or "").strip().lower()
    title = item.title.strip().lower()
    return hashlib.sha256(f"{title}\n{body}".encode()).hexdigest()


def _artifact_source(source_contexts: list[MemoryDreamSourceContext]) -> tuple[list[Item], list[str], list[str], list[str]]:
    source_items = [context.item for context in source_contexts]
    source_item_ids = [str(context.item.id) for context in source_contexts]
    source_digests = [context.digest for context in source_contexts]
    source_job_ids = sorted({job_id for context in source_contexts for job_id in context.job_ids})
    return source_items, source_item_ids, source_digests, source_job_ids


def _render_summary_body(key: MemoryDreamKey, source_contexts: list[MemoryDreamSourceContext]) -> tuple[str, str, dict]:
    lines = [
        f"# Memory Dream Summary: {key.day.isoformat()}",
        "",
        f"Scope: {_scope_label(key)}",
        f"Source memories: {len(source_contexts)}",
        "",
        "This derived summary is a source-traceable orientation artifact. Claims need source verification before use as canonical truth.",
        "",
        "## Source highlights",
    ]
    if not source_contexts:
        lines.append("- No source memories were available for this bounded window.")
    for context in source_contexts[:12]:
        item = context.item
        body = (item.raw_content or "").strip()
        detail = item.summary or (body.splitlines()[0] if body else "No summary.")
        lines.append(f"- {item.title}: {detail}")
    summary = f"Derived daily memory summary for {_scope_label(key)} from {len(source_contexts)} source memories."
    metrics = {"source_count": len(source_contexts)}
    return "\n".join(lines).strip(), summary, metrics


def _render_routing_body(key: MemoryDreamKey, source_contexts: list[MemoryDreamSourceContext]) -> tuple[str, str, dict]:
    tag_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    for context in source_contexts:
        item = context.item
        for tag in item.tags or []:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
        memory_entry = (item.metadata_ or {}).get("memory_entry")
        if isinstance(memory_entry, dict) and isinstance(memory_entry.get("source"), str):
            source_counts[memory_entry["source"]] = source_counts.get(memory_entry["source"], 0) + 1

    top_tags = sorted(tag_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:12]
    top_sources = sorted(source_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:8]
    manifest = {
        "artifact_type": key.artifact_type,
        "day": key.day.isoformat(),
        "scope": {"type": key.scope_type, "key": key.scope_key},
        "source_count": len(source_contexts),
        "top_tags": [{"tag": tag, "count": count} for tag, count in top_tags],
        "top_sources": [{"source": source, "count": count} for source, count in top_sources],
        "claims_need_source": True,
    }
    body = "\n".join(
        [
            f"# Routing Manifest: {key.day.isoformat()}",
            "",
            "```json",
            json.dumps(manifest, indent=2, sort_keys=True),
            "```",
        ]
    )
    summary = f"Derived route manifest for {_scope_label(key)} with {len(top_tags)} tag hints and {len(top_sources)} source hints."
    return body, summary, {"top_tag_count": len(top_tags), "top_source_count": len(top_sources)}


def _detect_hygiene(source_contexts: list[MemoryDreamSourceContext]) -> dict[str, list[dict[str, object]]]:
    by_hash: dict[str, list[Item]] = {}
    by_fingerprint: dict[str, list[Item]] = {}
    missing_provenance: list[dict[str, object]] = []
    low_signal_negative_self_recall: list[dict[str, object]] = []
    contradictions: list[dict[str, object]] = []
    over_compression: list[dict[str, object]] = []

    negative_phrases = (
        "can't remember",
        "cannot remember",
        "do not remember",
        "don't remember",
        "no memory",
        "nothing relevant",
        "forgot",
    )
    for context in source_contexts:
        item = context.item
        digest = item.content_hash or context.digest
        by_hash.setdefault(digest, []).append(item)
        by_fingerprint.setdefault(_source_fingerprint(item), []).append(item)

        memory_entry = (item.metadata_ or {}).get("memory_entry")
        if not isinstance(memory_entry, dict) or not memory_entry.get("source") or not memory_entry.get("created_at"):
            missing_provenance.append({"item_id": str(item.id), "title": item.title})

        raw_content = (item.raw_content or "").strip().lower()
        if any(phrase in raw_content for phrase in negative_phrases):
            low_signal_negative_self_recall.append({"item_id": str(item.id), "title": item.title})

        if context.contradiction_fact_ids:
            contradictions.append(
                {
                    "item_id": str(item.id),
                    "title": item.title,
                    "fact_ids": list(context.contradiction_fact_ids),
                }
            )

        source_ids = ((item.metadata_ or {}).get("diary_rollup") or {}).get("source_item_ids")
        if isinstance(source_ids, list) and len(source_ids) > 25:
            over_compression.append({"item_id": str(item.id), "title": item.title, "source_count": len(source_ids)})

    duplicate_candidates: list[dict[str, object]] = []
    seen_groups: set[tuple[str, ...]] = set()
    for group in (*by_hash.values(), *by_fingerprint.values()):
        if len(group) < 2:
            continue
        ids = tuple(sorted(str(item.id) for item in group))
        if ids in seen_groups:
            continue
        seen_groups.add(ids)
        duplicate_candidates.append({"item_ids": list(ids), "titles": [item.title for item in group]})

    return {
        "duplicate_candidates": duplicate_candidates,
        "missing_provenance": missing_provenance,
        "low_signal_negative_self_recall": low_signal_negative_self_recall,
        "contradictions": contradictions,
        "over_compression": over_compression,
    }


def _render_hygiene_body(key: MemoryDreamKey, source_contexts: list[MemoryDreamSourceContext]) -> tuple[str, str, dict]:
    findings = _detect_hygiene(source_contexts)
    lines = [
        f"# Memory Hygiene Report: {key.day.isoformat()}",
        "",
        f"Scope: {_scope_label(key)}",
        "All findings are advisory. This artifact does not delete, suppress, overwrite, or mutate raw memory.",
        "",
    ]
    for name, rows in findings.items():
        lines.append(f"## {name.replace('_', ' ').title()}")
        if not rows:
            lines.append("- None detected.")
        for row in rows[:12]:
            lines.append(f"- {json.dumps(row, sort_keys=True)}")
        lines.append("")
    summary = (
        f"Advisory memory hygiene report for {_scope_label(key)}: "
        f"{sum(len(rows) for rows in findings.values())} findings across {len(source_contexts)} source memories."
    )
    metrics = {name: len(rows) for name, rows in findings.items()}
    return "\n".join(lines).strip(), summary, metrics


def _render_artifact(key: MemoryDreamKey, source_contexts: list[MemoryDreamSourceContext]) -> tuple[str, str, dict]:
    if key.artifact_type == "palace-dream-summary":
        return _render_summary_body(key, source_contexts)
    if key.artifact_type == "palace-routing-manifest":
        return _render_routing_body(key, source_contexts)
    if key.artifact_type == "palace-hygiene-report":
        return _render_hygiene_body(key, source_contexts)
    raise ValueError(f"Unsupported memory dream artifact type: {key.artifact_type}")


def _dream_metadata(
    *,
    key: MemoryDreamKey,
    idempotency_key: str,
    source_contexts: list[MemoryDreamSourceContext],
    palace_generation: int,
    generated_at: datetime,
    artifact_metrics: dict[str, object],
) -> dict:
    source_items, source_item_ids, source_digests, source_job_ids = _artifact_source(source_contexts)
    return {
        "memory_entry": {
            "schema_version": 1,
            "source": key.artifact_type,
            "source_url": _dream_source_url(key),
            "created_at": datetime.combine(key.day, time(hour=23, minute=50, tzinfo=timezone.utc)).isoformat(),
            "created_by_role": "system",
            "scope": {"type": key.scope_type, "key": key.scope_key},
            "metadata": {
                "memory_dream": {
                    "artifact_type": key.artifact_type,
                    "source_count": len(source_contexts),
                    "claims_need_source": True,
                }
            },
            "idempotency_key": idempotency_key,
            "source_type": "note",
        },
        "memory_dream": {
            "schema_version": MEMORY_DREAM_SCHEMA_VERSION,
            "artifact_type": key.artifact_type,
            "day": key.day.isoformat(),
            "scope_type": key.scope_type,
            "scope_key": key.scope_key,
            "source_item_ids": source_item_ids,
            "source_titles": [item.title for item in source_items],
            "source_digests": source_digests,
            "source_job_ids": source_job_ids,
            "palace_generation": palace_generation,
            "generated_at": generated_at.isoformat(),
            "prompt_version": MEMORY_DREAM_PROMPT_VERSION,
            "model_version": "deterministic",
            "confidence": 0.6 if source_contexts else 0.0,
            "claims_need_source": True,
            "artifact_metrics": artifact_metrics,
        },
        "sync_relative_path": _dream_path(key),
    }


def _dream_matches(item: Item, *, body: str, summary: str, tags: list[str], metadata: dict) -> bool:
    return item.raw_content == body and item.summary == summary and item.tags == tags and item.metadata_ == metadata


async def _list_source_jobs(db: AsyncSession, *, item_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, tuple[str, ...]]:
    ids = tuple(item_ids)
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(Job.item_id, Job.id)
            .where(Job.item_id.in_(ids))
            .order_by(Job.created_at.desc(), Job.id.desc())
        )
    ).all()
    by_item: dict[uuid.UUID, list[str]] = {}
    for item_id, job_id in rows:
        if item_id is None:
            continue
        by_item.setdefault(item_id, []).append(str(job_id))
    return {item_id: tuple(job_ids[:5]) for item_id, job_ids in by_item.items()}


async def _list_contradiction_fact_ids(db: AsyncSession, *, tenant_id: str, item_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, tuple[str, ...]]:
    ids = tuple(item_ids)
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(TemporalFact.source_item_id, TemporalFact.id, TemporalFact.metadata_json)
            .where(TemporalFact.tenant_id == tenant_id)
            .where(TemporalFact.source_item_id.in_(ids))
        )
    ).all()
    by_item: dict[uuid.UUID, list[str]] = {}
    for item_id, fact_id, metadata in rows:
        if isinstance(metadata, dict) and isinstance(metadata.get("contradiction_sweep"), dict):
            by_item.setdefault(item_id, []).append(str(fact_id))
    return {item_id: tuple(fact_ids[:10]) for item_id, fact_ids in by_item.items()}


async def generate_memory_dreams(
    db: AsyncSession,
    *,
    tenant_id: str,
    embedder,
    llm,
    target_day: date | None = None,
) -> MemoryDreamBatchResult:
    dream_day = target_day or datetime.now(timezone.utc).date()
    state = await db.get(PalaceTenantState, tenant_id)
    palace_generation = int(getattr(state, "indexed_generation", 0) or 0)
    generated_at = datetime.combine(dream_day, time(hour=23, minute=50, tzinfo=timezone.utc))

    rows = (
        await db.execute(
            select(Item)
            .where(Item.tenant_id == tenant_id)
            .where(Item.source_type == "note")
            .order_by(Item.created_at.asc(), Item.id.asc())
        )
    ).scalars().all()

    existing_dreams: dict[MemoryDreamKey, Item] = {}
    source_groups: dict[tuple[str, str | None], list[Item]] = {}
    for item in rows:
        key = _memory_dream_key_from_item(item)
        if key is not None and key.day == dream_day:
            existing_dreams[key] = item
            continue
        if item.status != "ready" or item.deleted_at is not None or _is_other_derived_memory(item):
            continue
        if _normalize_created_at(item.created_at).date() != dream_day:
            continue
        scope = _memory_scope(item)
        if scope is None:
            continue
        source_groups.setdefault(scope, []).append(item)

    source_item_ids = [item.id for items in source_groups.values() for item in items]
    source_jobs = await _list_source_jobs(db, item_ids=source_item_ids)
    contradiction_facts = await _list_contradiction_fact_ids(db, tenant_id=tenant_id, item_ids=source_item_ids)

    created = 0
    updated = 0
    unchanged = 0
    deactivated = 0
    desired_keys: set[MemoryDreamKey] = set()

    for (scope_type, scope_key), source_items in source_groups.items():
        contexts = [
            MemoryDreamSourceContext(
                item=item,
                digest=_source_digest(item),
                job_ids=source_jobs.get(item.id, ()),
                contradiction_fact_ids=contradiction_facts.get(item.id, ()),
            )
            for item in source_items[:_MAX_SOURCE_ITEMS_PER_ARTIFACT]
        ]
        for artifact_type in MEMORY_DREAM_ARTIFACT_TYPES:
            key = MemoryDreamKey(
                day=dream_day,
                scope_type=scope_type,
                scope_key=scope_key,
                artifact_type=artifact_type,
            )
            desired_keys.add(key)
            idempotency_key = build_memory_dream_idempotency_key(tenant_id=tenant_id, key=key)
            body, summary, metrics = _render_artifact(key, contexts)
            tags = _dream_tags(key)
            metadata = _dream_metadata(
                key=key,
                idempotency_key=idempotency_key,
                source_contexts=contexts,
                palace_generation=palace_generation,
                generated_at=generated_at,
                artifact_metrics=metrics,
            )

            artifact = existing_dreams.get(key)
            if artifact is not None and _dream_matches(artifact, body=body, summary=summary, tags=tags, metadata=metadata):
                unchanged += 1
                continue

            is_new = artifact is None
            if artifact is None:
                artifact = Item(
                    source_type="note",
                    source_url=_dream_source_url(key),
                    title=_dream_title(key),
                    summary=summary,
                    raw_content=body,
                    metadata_=metadata,
                    tags=tags,
                    categories=["memory-dream"],
                    tenant_id=tenant_id,
                    status="processing",
                    created_at=datetime.combine(dream_day, time(hour=23, minute=50, tzinfo=timezone.utc)),
                    updated_at=generated_at,
                    idempotency_key=idempotency_key,
                )
                db.add(artifact)
                await db.flush()
                existing_dreams[key] = artifact
            else:
                artifact.source_url = _dream_source_url(key)
                artifact.title = _dream_title(key)
                artifact.summary = summary
                artifact.raw_content = body
                artifact.metadata_ = metadata
                artifact.tags = tags
                artifact.categories = ["memory-dream"]
                artifact.status = "processing"
                artifact.idempotency_key = idempotency_key
                artifact.updated_at = generated_at

            await process_prebuilt_item(
                db,
                item=artifact,
                embedder=embedder,
                llm=llm,
                tenant_id=tenant_id,
                enable_ai_enrichment=False,
            )
            await db.commit()
            if is_new:
                created += 1
            else:
                updated += 1

    for key, artifact in existing_dreams.items():
        if key in desired_keys or artifact.status == "failed":
            continue
        artifact.status = "failed"
        artifact.updated_at = generated_at
        await db.commit()
        deactivated += 1

    return MemoryDreamBatchResult(
        created=created,
        updated=updated,
        unchanged=unchanged,
        deactivated=deactivated,
    )
