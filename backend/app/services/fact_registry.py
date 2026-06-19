from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.item import Item
from app.models.palace import TemporalFact


FACT_LINE_PREFIX = "fact:"


@dataclass(frozen=True)
class TemporalFactCandidate:
    subject: str
    predicate: str
    object_text: str
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    confidence: float = 1.0
    metadata_json: dict | None = None


@dataclass(frozen=True)
class TemporalFactBatchResult:
    items_scanned: int
    created: int
    updated: int
    unchanged: int
    superseded: int


@dataclass(frozen=True)
class ContradictionSweepResult:
    facts_scanned: int
    contradictions: int
    facts_flagged: int
    facts_cleared: int


def _normalize_datetime(value: datetime | date | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed_date = date.fromisoformat(raw)
        except ValueError:
            return None
        return datetime.combine(parsed_date, time.min, tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _canonical_fact_payload(source_item_id: uuid.UUID, candidate: TemporalFactCandidate) -> dict[str, object]:
    return {
        "object_text": candidate.object_text,
        "predicate": candidate.predicate,
        "source_item_id": str(source_item_id),
        "subject": candidate.subject,
        "valid_from": candidate.valid_from.isoformat() if candidate.valid_from else None,
        "valid_to": candidate.valid_to.isoformat() if candidate.valid_to else None,
    }


def _build_fact_key(source_item_id: uuid.UUID, candidate: TemporalFactCandidate) -> str:
    canonical = json.dumps(
        _canonical_fact_payload(source_item_id, candidate),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _build_source_fingerprint(item: Item) -> str:
    payload = {
        "metadata": (item.metadata_ or {}).get("fact_registry"),
        "raw_content": item.raw_content,
        "summary": item.summary,
        "title": item.title,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _normalize_contradiction_text(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def _validity_windows_overlap(left: TemporalFact, right: TemporalFact) -> bool:
    left_start = left.valid_from or datetime.min.replace(tzinfo=timezone.utc)
    left_end = left.valid_to or datetime.max.replace(tzinfo=timezone.utc)
    right_start = right.valid_from or datetime.min.replace(tzinfo=timezone.utc)
    right_end = right.valid_to or datetime.max.replace(tzinfo=timezone.utc)
    return left_start <= right_end and right_start <= left_end


def _normalize_fact_candidate(raw: dict) -> TemporalFactCandidate | None:
    subject = raw.get("subject")
    predicate = raw.get("predicate")
    object_text = raw.get("object") or raw.get("object_text")
    if not isinstance(subject, str) or not subject.strip():
        return None
    if not isinstance(predicate, str) or not predicate.strip():
        return None
    if not isinstance(object_text, str) or not object_text.strip():
        return None
    confidence = raw.get("confidence", 1.0)
    if not isinstance(confidence, (int, float)):
        confidence = 1.0
    metadata_json = raw.get("metadata")
    if not isinstance(metadata_json, dict):
        metadata_json = {}
    return TemporalFactCandidate(
        subject=subject.strip(),
        predicate=predicate.strip(),
        object_text=object_text.strip(),
        valid_from=_normalize_datetime(raw.get("valid_from")),
        valid_to=_normalize_datetime(raw.get("valid_to")),
        confidence=float(confidence),
        metadata_json=metadata_json,
    )


def extract_fact_candidates(item: Item) -> list[TemporalFactCandidate]:
    candidates: list[TemporalFactCandidate] = []
    fact_metadata = (item.metadata_ or {}).get("fact_registry")
    if isinstance(fact_metadata, dict):
        raw_facts = fact_metadata.get("facts")
        if isinstance(raw_facts, list):
            for raw_fact in raw_facts:
                if isinstance(raw_fact, dict):
                    candidate = _normalize_fact_candidate(raw_fact)
                    if candidate is not None:
                        candidates.append(candidate)

    raw_content = item.raw_content or ""
    for line in raw_content.splitlines():
        stripped = line.strip().lstrip("-").strip()
        if not stripped.lower().startswith(FACT_LINE_PREFIX):
            continue
        payload = stripped[len(FACT_LINE_PREFIX):].strip()
        parts = [part.strip() for part in payload.split("|")]
        if len(parts) < 3:
            continue
        metadata_json: dict[str, object] = {"source": "raw_content"}
        if len(parts) >= 4 and parts[3]:
            metadata_json["parsed_valid_from"] = parts[3]
        if len(parts) >= 5 and parts[4]:
            metadata_json["parsed_valid_to"] = parts[4]
        candidate = TemporalFactCandidate(
            subject=parts[0],
            predicate=parts[1],
            object_text=parts[2],
            valid_from=_normalize_datetime(parts[3]) if len(parts) >= 4 else None,
            valid_to=_normalize_datetime(parts[4]) if len(parts) >= 5 else None,
            metadata_json=metadata_json,
        )
        candidates.append(candidate)

    deduped: dict[tuple[str, str, str, str | None, str | None], TemporalFactCandidate] = {}
    for candidate in candidates:
        deduped[
            (
                candidate.subject,
                candidate.predicate,
                candidate.object_text,
                candidate.valid_from.isoformat() if candidate.valid_from else None,
                candidate.valid_to.isoformat() if candidate.valid_to else None,
            )
        ] = candidate
    return list(deduped.values())


async def list_fact_registry_tenants(db: AsyncSession) -> tuple[str, ...]:
    tenant_ids = (
        await db.execute(
            select(Item.tenant_id)
            .where(Item.status == "ready")
            .where(Item.deleted_at.is_(None))
            .distinct()
            .order_by(Item.tenant_id.asc())
        )
    ).scalars().all()
    return tuple(tenant_ids)


async def extract_temporal_facts(
    db: AsyncSession,
    *,
    tenant_id: str,
) -> TemporalFactBatchResult:
    items = (
        await db.execute(
            select(Item)
            .where(Item.tenant_id == tenant_id)
            .where(Item.status == "ready")
            .where(Item.deleted_at.is_(None))
            .order_by(Item.updated_at.asc(), Item.id.asc())
        )
    ).scalars().all()

    created = 0
    updated = 0
    unchanged = 0
    superseded = 0
    now = datetime.now(timezone.utc)

    for item in items:
        fingerprint = _build_source_fingerprint(item)
        candidates = extract_fact_candidates(item)
        existing_rows = (
            await db.execute(
                select(TemporalFact)
                .where(TemporalFact.tenant_id == tenant_id)
                .where(TemporalFact.source_item_id == item.id)
            )
        ).scalars().all()

        existing_by_key = {row.fact_key: row for row in existing_rows}
        desired_by_key = {
            _build_fact_key(item.id, candidate): candidate
            for candidate in candidates
        }

        if (
            existing_rows
            and all(row.source_fingerprint == fingerprint for row in existing_rows)
            and set(existing_by_key) == set(desired_by_key)
            and all(existing_by_key[key].status == "active" for key in desired_by_key)
        ):
            unchanged += len(desired_by_key)
            continue

        for fact_key, candidate in desired_by_key.items():
            existing = existing_by_key.get(fact_key)
            if existing is None:
                db.add(
                    TemporalFact(
                        tenant_id=tenant_id,
                        source_item_id=item.id,
                        fact_key=fact_key,
                        source_fingerprint=fingerprint,
                        subject=candidate.subject,
                        predicate=candidate.predicate,
                        object_text=candidate.object_text,
                        confidence=candidate.confidence,
                        valid_from=candidate.valid_from,
                        valid_to=candidate.valid_to,
                        status="active",
                        metadata_json=candidate.metadata_json or {},
                        extracted_at=now,
                    )
                )
                created += 1
                continue

            changed = (
                existing.subject != candidate.subject
                or existing.predicate != candidate.predicate
                or existing.object_text != candidate.object_text
                or existing.confidence != candidate.confidence
                or existing.valid_from != candidate.valid_from
                or existing.valid_to != candidate.valid_to
                or existing.metadata_json != (candidate.metadata_json or {})
                or existing.status != "active"
                or existing.source_fingerprint != fingerprint
            )
            existing.subject = candidate.subject
            existing.predicate = candidate.predicate
            existing.object_text = candidate.object_text
            existing.confidence = candidate.confidence
            existing.valid_from = candidate.valid_from
            existing.valid_to = candidate.valid_to
            existing.status = "active"
            existing.metadata_json = candidate.metadata_json or {}
            existing.source_fingerprint = fingerprint
            existing.extracted_at = now
            existing.superseded_at = None
            if changed:
                updated += 1
            else:
                unchanged += 1

        for fact_key, existing in existing_by_key.items():
            if fact_key in desired_by_key or existing.status == "superseded":
                continue
            existing.status = "superseded"
            existing.superseded_at = now
            existing.extracted_at = now
            superseded += 1

    await db.commit()
    return TemporalFactBatchResult(
        items_scanned=len(items),
        created=created,
        updated=updated,
        unchanged=unchanged,
        superseded=superseded,
    )


async def sweep_fact_registry_contradictions(
    db: AsyncSession,
    *,
    tenant_id: str,
) -> ContradictionSweepResult:
    facts = (
        await db.execute(
            select(TemporalFact)
            .where(TemporalFact.tenant_id == tenant_id)
            .where(TemporalFact.status == "active")
            .order_by(TemporalFact.subject.asc(), TemporalFact.predicate.asc(), TemporalFact.id.asc())
        )
    ).scalars().all()

    facts_by_claim: dict[tuple[str, str], list[TemporalFact]] = {}
    for fact in facts:
        claim_key = (
            _normalize_contradiction_text(fact.subject),
            _normalize_contradiction_text(fact.predicate),
        )
        facts_by_claim.setdefault(claim_key, []).append(fact)

    conflicts_by_fact_id: dict[uuid.UUID, set[uuid.UUID]] = {fact.id: set() for fact in facts}
    contradiction_pairs = 0
    for claim_facts in facts_by_claim.values():
        if len(claim_facts) < 2:
            continue
        for index, left in enumerate(claim_facts):
            left_object = _normalize_contradiction_text(left.object_text)
            for right in claim_facts[index + 1:]:
                if left_object == _normalize_contradiction_text(right.object_text):
                    continue
                if not _validity_windows_overlap(left, right):
                    continue
                conflicts_by_fact_id[left.id].add(right.id)
                conflicts_by_fact_id[right.id].add(left.id)
                contradiction_pairs += 1

    now = datetime.now(timezone.utc).isoformat()
    facts_flagged = 0
    facts_cleared = 0
    for fact in facts:
        metadata = dict(fact.metadata_json or {})
        conflict_ids = sorted(str(conflict_id) for conflict_id in conflicts_by_fact_id[fact.id])
        existing_sweep = metadata.get("contradiction_sweep")
        existing_conflict_ids = []
        if isinstance(existing_sweep, dict):
            raw_existing_conflict_ids = existing_sweep.get("conflicting_fact_ids")
            if isinstance(raw_existing_conflict_ids, list):
                existing_conflict_ids = sorted(str(conflict_id) for conflict_id in raw_existing_conflict_ids)

        if conflict_ids:
            if existing_conflict_ids != conflict_ids:
                sweep_metadata = {
                    "checked_at": now,
                    "conflict_count": len(conflict_ids),
                    "conflicting_fact_ids": conflict_ids,
                }
                metadata["contradiction_sweep"] = sweep_metadata
                fact.metadata_json = metadata
                facts_flagged += 1
            continue

        if existing_sweep is not None:
            metadata.pop("contradiction_sweep", None)
            fact.metadata_json = metadata
            facts_cleared += 1

    await db.commit()
    return ContradictionSweepResult(
        facts_scanned=len(facts),
        contradictions=contradiction_pairs,
        facts_flagged=facts_flagged,
        facts_cleared=facts_cleared,
    )


async def build_fact_registry_summary(db: AsyncSession, *, tenant_id: str) -> dict[str, object]:
    counts = (
        await db.execute(
            select(
                TemporalFact.status,
                func.count(TemporalFact.id),
                func.count(func.distinct(TemporalFact.source_item_id)),
                func.max(TemporalFact.extracted_at),
            )
            .where(TemporalFact.tenant_id == tenant_id)
            .group_by(TemporalFact.status)
        )
    ).all()
    active = 0
    superseded = 0
    distinct_sources = 0
    last_extracted_at = None
    for status, count, source_count, extracted_at in counts:
        if status == "active":
            active = count
            distinct_sources = source_count
        elif status == "superseded":
            superseded = count
        if extracted_at is not None and (last_extracted_at is None or extracted_at > last_extracted_at):
            last_extracted_at = extracted_at

    recent_rows = (
        await db.execute(
            select(TemporalFact, Item.title)
            .join(Item, Item.id == TemporalFact.source_item_id)
            .where(TemporalFact.tenant_id == tenant_id)
            .order_by(TemporalFact.extracted_at.desc(), TemporalFact.id.desc())
            .limit(8)
        )
    ).all()
    recent_facts = []
    for fact, item_title in recent_rows:
        recent_facts.append(
            {
                "id": fact.id,
                "source_item_id": fact.source_item_id,
                "source_item_title": item_title,
                "subject": fact.subject,
                "predicate": fact.predicate,
                "object_text": fact.object_text,
                "confidence": fact.confidence,
                "status": fact.status,
                "valid_from": fact.valid_from,
                "valid_to": fact.valid_to,
                "extracted_at": fact.extracted_at,
                "superseded_at": fact.superseded_at,
            }
        )
    return {
        "active": active,
        "superseded": superseded,
        "distinct_sources": distinct_sources,
        "last_extracted_at": last_extracted_at,
        "recent_facts": recent_facts,
    }


async def list_temporal_facts(
    db: AsyncSession,
    *,
    tenant_id: str,
    current_only: bool = True,
    limit: int = 50,
) -> list[dict[str, object]]:
    stmt = (
        select(TemporalFact, Item.title)
        .join(Item, Item.id == TemporalFact.source_item_id)
        .where(TemporalFact.tenant_id == tenant_id)
        .order_by(TemporalFact.extracted_at.desc(), TemporalFact.id.desc())
        .limit(limit)
    )
    if current_only:
        stmt = stmt.where(TemporalFact.status == "active")
    rows = (await db.execute(stmt)).all()
    return [
        {
            "id": fact.id,
            "source_item_id": fact.source_item_id,
            "source_item_title": item_title,
            "subject": fact.subject,
            "predicate": fact.predicate,
            "object_text": fact.object_text,
            "confidence": fact.confidence,
            "status": fact.status,
            "valid_from": fact.valid_from,
            "valid_to": fact.valid_to,
            "extracted_at": fact.extracted_at,
            "superseded_at": fact.superseded_at,
        }
        for fact, item_title in rows
    ]
