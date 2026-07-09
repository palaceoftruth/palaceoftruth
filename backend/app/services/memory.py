from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from time import perf_counter
from typing import Any

from fastapi import HTTPException

from sqlalchemy import Text, and_, bindparam, case, func, literal, or_, select, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job import Job
from app.models.item import Item
from app.models.palace import MemoryEntry, PalaceTenantState
from app.schemas.memory import (
    AgentMemoryRetrieveRequest,
    AgentMemoryRetrieveResponse,
    AgentMemoryRetrieveTrace,
    LegacyMemoryArtifactRequest,
    MEMORY_JOB_PUBLIC_STATUS_MAP,
    MEMORY_JOB_TYPE,
    MemoryArtifactAcceptedResponse,
    MemoryEntryRequest,
    MemoryEntryListItem,
    MemoryEntryListResponse,
    MemoryJobListResponse,
    MemoryJobResponse,
    MemoryQueueHint,
    MemoryWriteContractStatus,
    MemoryRetrievalDoctorAuthShape,
    MemoryRetrievalDoctorCheck,
    MemoryRetrievalDoctorGeneration,
    MemoryRetrievalDoctorProbeReport,
    MemoryRetrievalDoctorProbeTopResult,
    MemoryRetrievalDoctorRankingRoute,
    MemoryRetrievalDoctorRelationshipState,
    MemoryRetrievalDoctorRequest,
    MemoryRetrievalDoctorResponse,
    MemoryRetrievalDoctorWakeupState,
    MemoryScope,
    MemoryScopeListResponse,
    MemoryScopeProfile,
    MemoryScopeProfileUpsertRequest,
    MemoryScopeSummary,
    MemoryRetrieveRequest,
    MemoryRetrieveResponse,
    SemanticRecallItem,
    SemanticRecallRequest,
    SemanticRecallResponse,
    SemanticRecallTrace,
    MemoryWakeupBriefResponse,
    TagsMode,
)
from app.schemas.palace import PalaceRetrieveRequest
from app.schemas.search import split_system_provenance_tags
from app.services.job_progress import record_job_progress_event
from app.services.item_dates import apply_effective_date
from app.services.memory_entries import (
    NormalizedMemoryEntry,
    build_legacy_memory_tags,
    normalize_legacy_memory_artifact,
    normalize_memory_entry,
    request_fingerprint,
    source_project_from_memory_metadata,
)
from app.services.palace import retrieve_palace
from app.services.queue_telemetry import build_worker_backpressure
from app.services.search import SearchService
from app.services.source_trust_summary import get_source_trust_summaries
from app.services.wakeup_briefs import build_wakeup_brief_summary
from app.utils.webhook import validate_webhook_url

MEMORY_SOURCE_TYPE = "note"
STALE_MEMORY_PROCESSING_MINUTES = 20
STALE_MEMORY_QUEUED_MINUTES = 30

_MEMORY_STATUS_QUERY_MAP = {
    "queued": "queued",
    "processing": "processing",
    "complete": "completed",
    "completed": "completed",
    "duplicate": "duplicate",
    "failed": "failed",
    "cancelled": "cancelled",
}


def _memory_scope_profile_from_row(row: Any, *, scope: MemoryScope | None = None) -> MemoryScopeProfile:
    resolved_scope = scope or MemoryScope(type=row["scope_type"], key=row["scope_key"])
    return MemoryScopeProfile(
        scope=resolved_scope,
        retain_mission=row.get("retain_mission") or "",
        quiet_recall=bool(row.get("quiet_recall", False)),
        created_at=row.get("profile_created_at"),
        updated_at=row.get("profile_updated_at"),
        created_by=row.get("created_by"),
        updated_by=row.get("updated_by"),
    )


def _default_memory_scope_profile(scope: MemoryScope) -> MemoryScopeProfile:
    return MemoryScopeProfile(scope=scope, retain_mission="", quiet_recall=False)


@dataclass
class MemoryArtifactAcceptanceResult:
    job: Job
    enqueue_requested: bool
    scope_type: str
    scope_key: str | None
    accepted_as: str
    replayed: bool = False
    source_item_id: uuid.UUID | None = None


def _duplicate_conflict_detail(
    *,
    entry: NormalizedMemoryEntry,
    existing_job: Job,
    conflict_kind: str,
) -> dict[str, Any]:
    return {
        "status": "duplicate_conflict",
        "contract_status": "rejected",
        "message": "Memory entry idempotency key already exists for a different payload or scope",
        "retryable": False,
        "conflict_kind": conflict_kind,
        "idempotency_key": entry.idempotency_key,
        "existing_job_id": str(existing_job.id),
        "existing_source_item_id": str(existing_job.item_id) if existing_job.item_id else None,
        "scope": {"type": entry.scope.type, "key": entry.scope.key},
    }


def _existing_memory_fingerprint(job: Job) -> str | None:
    payload = job.payload if isinstance(job.payload, dict) else {}
    fingerprint = payload.get("request_fingerprint")
    return fingerprint if isinstance(fingerprint, str) and fingerprint else None


def _existing_memory_scope(job: Job) -> tuple[str | None, str | None]:
    payload = job.payload if isinstance(job.payload, dict) else {}
    scope_type = payload.get("scope_type")
    scope_key = payload.get("scope_key")
    return (
        scope_type if isinstance(scope_type, str) and scope_type else None,
        scope_key if isinstance(scope_key, str) and scope_key else None,
    )


def _existing_memory_entry_metadata(item: Item | None) -> dict[str, Any]:
    metadata = getattr(item, "metadata_", None) if item is not None else None
    memory_entry = (metadata or {}).get("memory_entry") if isinstance(metadata, dict) else None
    return memory_entry if isinstance(memory_entry, dict) else {}


def _existing_memory_item_fingerprint(
    *,
    item: Item | None,
    job: Job,
    accepted_as: str,
) -> str | None:
    raw_content = getattr(item, "raw_content", None) if item is not None else None
    if item is None or raw_content is None:
        return None

    memory_entry = _existing_memory_entry_metadata(item)
    scope_type, scope_key = _existing_memory_scope(job)
    scope = memory_entry.get("scope")
    if isinstance(scope, dict):
        scope_type = scope.get("type") if isinstance(scope.get("type"), str) else scope_type
        scope_key = scope.get("key") if scope.get("key") is None or isinstance(scope.get("key"), str) else scope_key

    payload = job.payload if isinstance(job.payload, dict) else {}
    if accepted_as == "legacy_artifact":
        item_metadata = getattr(item, "metadata_", None)
        contract = (item_metadata or {}).get("memory_contract") if isinstance(item_metadata, dict) else None
        if not isinstance(contract, dict):
            return None
        return request_fingerprint(
            {
                "tenant_id": item.tenant_id,
                "company_id": contract.get("company_id"),
                "memory_kind": contract.get("memory_kind"),
                "title": item.title,
                "summary": item.summary,
                "body_sha256": sha256(raw_content.encode()).hexdigest(),
                "tags": item.tags or [],
                "created_by_role": contract.get("created_by_role"),
                "source": contract.get("source"),
                "created_at": contract.get("created_at")
                or (item.created_at.astimezone(timezone.utc).isoformat() if item.created_at else None),
                "scope": {"type": scope_type, "key": scope_key},
                "project_id": contract.get("project_id"),
                "ticket_id": contract.get("ticket_id"),
                "task_id": contract.get("task_id"),
                "outcome": contract.get("outcome"),
                "review_status": contract.get("review_status"),
                "repo_ref": contract.get("repo_ref"),
                "inputs": contract.get("inputs"),
                "outputs": contract.get("outputs"),
                "enable_ai_enrichment": bool(payload.get("enable_ai_enrichment", False)),
                "relationship_policy": payload.get("relationship_policy", "immediate"),
                "accepted_as": "legacy_artifact",
            }
        )

    return request_fingerprint(
        {
            "tenant_id": item.tenant_id,
            "title": item.title,
            "body_sha256": sha256(raw_content.encode()).hexdigest(),
            "summary": item.summary,
            "source": memory_entry.get("source"),
            "created_at": memory_entry.get("created_at")
            or (item.created_at.astimezone(timezone.utc).isoformat() if item.created_at else None),
            "tags": item.tags or [],
            "scope": {"type": scope_type, "key": scope_key},
            "source_url": item.source_url,
            "created_by_role": memory_entry.get("created_by_role"),
            "metadata": memory_entry.get("metadata"),
            "valid_from": memory_entry.get("valid_from"),
            "valid_until": memory_entry.get("valid_until"),
            "supersedes_entry_id": memory_entry.get("supersedes_entry_id"),
            "fact_kind": memory_entry.get("fact_kind"),
            "enable_ai_enrichment": bool(payload.get("enable_ai_enrichment", False)),
            "relationship_policy": payload.get("relationship_policy", "immediate"),
            "accepted_as": "canonical",
        }
    )


async def _ensure_duplicate_replay_matches(db: AsyncSession, *, entry: NormalizedMemoryEntry, existing_job: Job) -> None:
    existing_scope_type, existing_scope_key = _existing_memory_scope(existing_job)
    if existing_scope_type and (existing_scope_type != entry.scope.type or existing_scope_key != entry.scope.key):
        raise HTTPException(
            status_code=409,
            detail=_duplicate_conflict_detail(
                entry=entry,
                existing_job=existing_job,
                conflict_kind="scope_mismatch",
            ),
        )

    fingerprint = _existing_memory_fingerprint(existing_job)
    if fingerprint is None:
        existing_item = await db.get(Item, existing_job.item_id) if existing_job.item_id else None
        fingerprint = _existing_memory_item_fingerprint(
            item=existing_item,
            job=existing_job,
            accepted_as=entry.accepted_as,
        )
    if fingerprint is not None and fingerprint != entry.request_fingerprint:
        raise HTTPException(
            status_code=409,
            detail=_duplicate_conflict_detail(
                entry=entry,
                existing_job=existing_job,
                conflict_kind="payload_mismatch",
            ),
        )
    if fingerprint is None:
        raise HTTPException(
            status_code=409,
            detail=_duplicate_conflict_detail(
                entry=entry,
                existing_job=existing_job,
                conflict_kind="unverifiable_existing_payload",
            ),
        )


async def _accept_duplicate_memory_replay(
    db: AsyncSession,
    *,
    entry: NormalizedMemoryEntry,
    existing_job: Job,
    webhook_url: str | None,
    signing_key: str | None,
) -> MemoryArtifactAcceptanceResult:
    await _ensure_duplicate_replay_matches(db, entry=entry, existing_job=existing_job)
    webhook_metadata_updated = False
    if webhook_url and not existing_job.webhook_url:
        existing_job.webhook_url = webhook_url
        existing_job.signing_key = signing_key
        webhook_metadata_updated = True
    revived = await revive_retryable_memory_job(db, job=existing_job)
    # Persist webhook metadata for duplicate replays even when the job did not
    # need stale-job recovery; otherwise later webhook dispatch reads stale DB state.
    if not revived and webhook_metadata_updated:
        await db.commit()
        await db.refresh(existing_job)
    return MemoryArtifactAcceptanceResult(
        job=existing_job,
        enqueue_requested=revived,
        scope_type=entry.scope.type,
        scope_key=entry.scope.key,
        accepted_as=entry.accepted_as,
        replayed=True,
        source_item_id=existing_job.item_id,
    )


@dataclass(frozen=True)
class DelegatedAgentMemoryReadPolicy:
    tenant_id: str
    read_agent_scope_keys: tuple[str, ...]
    policy_id: str | None = None
    policy_source: str | None = "in_memory"
    subject_agent_scope_key: str | None = None
    allow_all_agent_scopes: bool = False
    require_access_reason: bool = True
    max_cross_agent_scopes: int = 5
    max_results_per_scope: int | None = None


@dataclass(frozen=True)
class DelegatedAgentMemoryDecision:
    caller_agent_scope_key: str | None
    requested_agent_scope_keys: tuple[str, ...]
    authorized_agent_scope_keys: tuple[str, ...]
    denied_agent_scope_keys: tuple[str, ...]
    policy_id: str | None
    policy_source: str | None
    access_reason_required: bool
    access_reason_present: bool
    decision: str
    deny_reasons: tuple[str, ...]
    max_results_per_scope: int | None = None


@dataclass(frozen=True)
class AgentScopePatternResolution:
    requested_patterns: tuple[str, ...] = ()
    discovered_keys: tuple[str, ...] = ()
    matched_keys: tuple[str, ...] = ()
    selected_keys: tuple[str, ...] = ()
    skipped_keys: tuple[str, ...] = ()
    skip_reasons: tuple[str, ...] = ()
    truncated: bool = False


def _policy_string(value: object, *, field: str, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise ValueError(f"{field} is required")
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-blank string")
    return value.strip()


def _policy_bool(value: object, *, field: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _policy_int(
    value: object,
    *,
    field: str,
    default: int | None,
    min_value: int | None = None,
) -> int | None:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    if min_value is not None and value < min_value:
        raise ValueError(f"{field} must be at least {min_value}")
    return value


def _policy_scope_keys(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list of strings")
    keys = _dedupe_scope_keys(tuple(_policy_string(item, field=field, required=True) or "" for item in value))
    if not keys:
        raise ValueError(f"{field} must include at least one scope key")
    return keys


def parse_delegated_agent_memory_read_policies(raw: str) -> tuple[DelegatedAgentMemoryReadPolicy, ...]:
    """Parse deployment-owned delegated memory read policies from JSON config."""
    if not raw.strip():
        return ()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("delegated agent memory policies must be valid JSON") from exc
    if not isinstance(payload, list):
        raise ValueError("delegated agent memory policies must be a JSON list")

    policies: list[DelegatedAgentMemoryReadPolicy] = []
    for index, entry in enumerate(payload):
        if not isinstance(entry, dict):
            raise ValueError(f"delegated agent memory policy {index} must be an object")
        policies.append(
            DelegatedAgentMemoryReadPolicy(
                tenant_id=_policy_string(entry.get("tenant_id"), field=f"policy {index} tenant_id", required=True)
                or "",
                read_agent_scope_keys=_policy_scope_keys(
                    entry.get("read_agent_scope_keys"),
                    field=f"policy {index} read_agent_scope_keys",
                ),
                policy_id=_policy_string(entry.get("policy_id"), field=f"policy {index} policy_id"),
                policy_source=_policy_string(
                    entry.get("policy_source"),
                    field=f"policy {index} policy_source",
                )
                or "deployment_config",
                subject_agent_scope_key=_policy_string(
                    entry.get("subject_agent_scope_key"),
                    field=f"policy {index} subject_agent_scope_key",
                ),
                allow_all_agent_scopes=_policy_bool(
                    entry.get("allow_all_agent_scopes"),
                    field=f"policy {index} allow_all_agent_scopes",
                    default=False,
                ),
                require_access_reason=_policy_bool(
                    entry.get("require_access_reason"),
                    field=f"policy {index} require_access_reason",
                    default=True,
                ),
                max_cross_agent_scopes=_policy_int(
                    entry.get("max_cross_agent_scopes"),
                    field=f"policy {index} max_cross_agent_scopes",
                    default=5,
                    min_value=0,
                )
                or 0,
                max_results_per_scope=_policy_int(
                    entry.get("max_results_per_scope"),
                    field=f"policy {index} max_results_per_scope",
                    default=None,
                    min_value=1,
                ),
            )
        )
    return tuple(policies)


def delegated_agent_memory_policy_from_config(
    *,
    tenant_id: str,
    agent_scope_key: str | None,
    raw_policies: str,
) -> DelegatedAgentMemoryReadPolicy | None:
    caller_key = agent_scope_key.strip() if agent_scope_key else None
    for policy in parse_delegated_agent_memory_read_policies(raw_policies):
        if policy.tenant_id != tenant_id:
            continue
        if policy.subject_agent_scope_key and policy.subject_agent_scope_key != caller_key:
            continue
        return policy
    return None


def build_memory_tags(body: LegacyMemoryArtifactRequest) -> list[str]:
    return build_legacy_memory_tags(body)


def build_memory_idempotency_key(body: LegacyMemoryArtifactRequest) -> str:
    return normalize_legacy_memory_artifact(body).idempotency_key


def serialize_memory_job(job: Job) -> MemoryJobResponse:
    mapped_status = MEMORY_JOB_PUBLIC_STATUS_MAP.get(job.status, job.status)
    contract_status = memory_job_contract_status(job)
    retryable = contract_status in {"retryable_degraded", "dependency_unavailable"}
    return MemoryJobResponse(
        job_id=job.id,
        status=mapped_status,
        contract_status=contract_status,
        error_message=job.error_message,
        duplicate_of=job.duplicate_of,
        created_at=job.created_at,
        completed_at=job.completed_at,
        retryable=retryable,
        retry_after_seconds=30 if retryable else None,
    )


def memory_job_contract_status(job: Job) -> MemoryWriteContractStatus:
    payload = job.payload or {}
    explicit_status = payload.get("contract_status")
    if explicit_status in {"dependency_unavailable", "permanent_tenant_mismatch"}:
        return explicit_status
    if job.status in {"completed", "duplicate"}:
        return "completed"
    if job.status in {"queued", "processing"}:
        if is_stale_memory_job(job):
            return "retryable_degraded"
        return job.status
    if job.status in {"failed", "cancelled"}:
        return "retryable_degraded"
    return "retryable_degraded"


def _item_metadata(entry: NormalizedMemoryEntry) -> dict:
    metadata = dict(entry.metadata)
    metadata.setdefault("memory_entry", {})
    metadata["memory_entry"]["source_type"] = MEMORY_SOURCE_TYPE
    metadata["memory_entry"]["source_url"] = entry.source_url
    return metadata


async def _add_memory_entry_row(db: AsyncSession, *, item: Item, entry: NormalizedMemoryEntry) -> MemoryEntry:
    superseded: MemoryEntry | None = None
    if entry.supersedes_entry_id is not None:
        superseded = await db.scalar(
            select(MemoryEntry)
            .where(MemoryEntry.tenant_id == entry.tenant_id)
            .where(MemoryEntry.id == entry.supersedes_entry_id)
            .limit(1)
        )
        if superseded is None:
            await db.rollback()
            raise HTTPException(
                status_code=422,
                detail={
                    "status": "invalid_supersession",
                    "message": "supersedes_entry_id does not refer to an existing memory entry in this tenant",
                    "supersedes_entry_id": str(entry.supersedes_entry_id),
                    "retryable": False,
                },
            )
    memory_entry = MemoryEntry(
        tenant_id=entry.tenant_id,
        item_id=item.id,
        scope_type=entry.scope.type,
        scope_key=entry.scope.key if entry.scope.type != "tenant_shared" else None,
        source=entry.source,
        source_url=entry.source_url,
        created_by_role=(entry.metadata.get("memory_entry") or {}).get("created_by_role"),
        idempotency_key=entry.idempotency_key,
        valid_from=entry.valid_from,
        valid_until=entry.valid_until,
        supersedes_entry_id=entry.supersedes_entry_id,
        fact_kind=entry.fact_kind,
        metadata_=(entry.metadata.get("memory_entry") or {}).get("metadata") or {},
        created_at=entry.created_at,
        updated_at=entry.created_at,
    )
    db.add(memory_entry)
    await db.flush()
    if superseded is not None:
        superseded.superseded_by_entry_id = memory_entry.id
        superseded.updated_at = entry.created_at
    return memory_entry


def is_stale_memory_job(job: Job, *, now: datetime | None = None) -> bool:
    if job.status not in {"queued", "processing"} or job.created_at is None:
        return False

    current_time = now or datetime.now(timezone.utc)
    max_age = (
        timedelta(minutes=STALE_MEMORY_PROCESSING_MINUTES)
        if job.status == "processing"
        else timedelta(minutes=STALE_MEMORY_QUEUED_MINUTES)
    )
    return current_time - job.created_at >= max_age


async def revive_stale_memory_job(db: AsyncSession, *, job: Job) -> bool:
    if not is_stale_memory_job(job):
        return False

    item = await db.get(Item, job.item_id) if job.item_id else None
    if item is None or not item.raw_content:
        return False

    job.status = "queued"
    job.progress = 0
    job.error_message = None
    job.completed_at = None
    item.status = "processing"
    await db.commit()
    await db.refresh(job)
    return True


async def revive_dependency_unavailable_memory_job(db: AsyncSession, *, job: Job) -> bool:
    payload = dict(job.payload or {})
    if job.status != "failed" or payload.get("contract_status") != "dependency_unavailable":
        return False

    item = await db.get(Item, job.item_id) if job.item_id else None
    if item is None or not item.raw_content:
        return False

    payload.pop("contract_status", None)
    job.payload = payload
    job.status = "queued"
    job.progress = 0
    job.error_message = None
    job.completed_at = None
    item.status = "processing"
    await db.commit()
    await db.refresh(job)
    return True


async def revive_retryable_memory_job(db: AsyncSession, *, job: Job) -> bool:
    if await revive_stale_memory_job(db, job=job):
        return True
    return await revive_dependency_unavailable_memory_job(db, job=job)


async def accept_memory_entry(
    db: AsyncSession,
    *,
    entry: NormalizedMemoryEntry,
    signing_key: str | None,
    admission_audit: dict[str, Any] | None = None,
) -> MemoryArtifactAcceptanceResult:
    webhook_url = validate_webhook_url(entry.webhook_url) if entry.webhook_url else None

    existing_job = await db.scalar(
        select(Job)
        .where(Job.tenant_id == entry.tenant_id)
        .where(Job.job_type == MEMORY_JOB_TYPE)
        .where(Job.payload["idempotency_key"].astext == entry.idempotency_key)
        .order_by(Job.created_at.desc())
        .limit(1)
    )
    if existing_job:
        return await _accept_duplicate_memory_replay(
            db,
            entry=entry,
            existing_job=existing_job,
            webhook_url=webhook_url,
            signing_key=signing_key,
        )

    item = Item(
        source_type=MEMORY_SOURCE_TYPE,
        source_url=entry.source_url,
        title=entry.title,
        summary=entry.summary,
        raw_content=entry.body,
        metadata_=_item_metadata(entry),
        tags=entry.tags,
        categories=[],
        tenant_id=entry.tenant_id,
        status="processing",
        created_at=entry.created_at,
        updated_at=entry.created_at,
        idempotency_key=entry.idempotency_key,
    )
    apply_effective_date(item, fallback_created_at=entry.created_at)
    db.add(item)

    try:
        await db.flush()
        memory_entry = await _add_memory_entry_row(db, item=item, entry=entry)
        job_payload: dict[str, Any] = {
            "idempotency_key": entry.idempotency_key,
            "enable_ai_enrichment": entry.enable_ai_enrichment,
            "scope_type": entry.scope.type,
            "scope_key": entry.scope.key,
            "accepted_as": entry.accepted_as,
            "relationship_policy": entry.relationship_policy,
            "request_fingerprint": entry.request_fingerprint,
            "memory_entry_id": str(memory_entry.id),
        }
        if admission_audit is not None:
            job_payload["admission"] = admission_audit
        job = Job(
            item_id=item.id,
            job_type=MEMORY_JOB_TYPE,
            status="queued",
            progress=0,
            tenant_id=entry.tenant_id,
            webhook_url=webhook_url,
            signing_key=signing_key if webhook_url else None,
            payload=job_payload,
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)
        return MemoryArtifactAcceptanceResult(
            job=job,
            enqueue_requested=True,
            scope_type=entry.scope.type,
            scope_key=entry.scope.key,
            accepted_as=entry.accepted_as,
            source_item_id=item.id,
        )
    except IntegrityError:
        await db.rollback()
        existing_job = await db.scalar(
            select(Job)
            .where(Job.tenant_id == entry.tenant_id)
            .where(Job.job_type == MEMORY_JOB_TYPE)
            .where(Job.payload["idempotency_key"].astext == entry.idempotency_key)
            .order_by(Job.created_at.desc())
            .limit(1)
        )
        if existing_job:
            return await _accept_duplicate_memory_replay(
                db,
                entry=entry,
                existing_job=existing_job,
                webhook_url=webhook_url,
                signing_key=signing_key,
            )
        raise HTTPException(
            status_code=409,
            detail={
                "status": "duplicate_conflict",
                "contract_status": "rejected",
                "message": "Memory entry already exists but no existing job metadata was available",
                "retryable": False,
                "conflict_kind": "missing_existing_job",
                "idempotency_key": entry.idempotency_key,
                "scope": {"type": entry.scope.type, "key": entry.scope.key},
            },
        )


async def accept_memory_artifact(
    db: AsyncSession,
    *,
    body: LegacyMemoryArtifactRequest,
    signing_key: str | None,
) -> MemoryArtifactAcceptanceResult:
    return await accept_memory_entry(
        db,
        entry=normalize_legacy_memory_artifact(body),
        signing_key=signing_key,
        admission_audit=None,
    )


async def accept_canonical_memory_entry(
    db: AsyncSession,
    *,
    body: MemoryEntryRequest,
    signing_key: str | None,
    admission_audit: dict[str, Any] | None = None,
) -> MemoryArtifactAcceptanceResult:
    return await accept_memory_entry(
        db,
        entry=normalize_memory_entry(body),
        signing_key=signing_key,
        admission_audit=admission_audit,
    )


def build_memory_acceptance_response(
    result: MemoryArtifactAcceptanceResult,
    *,
    poll_url: str | None = None,
    queue: MemoryQueueHint | None = None,
) -> MemoryArtifactAcceptedResponse:
    serialized = serialize_memory_job(result.job)
    queue_retry_after_seconds = queue.retry_after_seconds if queue else None
    retry_after_seconds = queue_retry_after_seconds or serialized.retry_after_seconds
    contract_status: MemoryWriteContractStatus = "accepted"
    if queue and queue.state in {"backpressure", "saturated"}:
        contract_status = "retryable_degraded"
    elif result.replayed and serialized.contract_status == "completed":
        contract_status = "completed"
    elif serialized.contract_status in {"retryable_degraded", "dependency_unavailable", "permanent_tenant_mismatch"}:
        contract_status = serialized.contract_status
    return MemoryArtifactAcceptedResponse(
        job_id=serialized.job_id,
        status=serialized.status,
        contract_status=contract_status,
        replayed=result.replayed,
        source_item_id=result.source_item_id or result.job.item_id,
        scope={"type": result.scope_type, "key": result.scope_key},
        accepted_as=result.accepted_as,
        poll_url=poll_url,
        poll_after_seconds=queue.poll_after_seconds if queue else 5,
        retryable=serialized.retryable or queue_retry_after_seconds is not None,
        retry_after_seconds=retry_after_seconds,
        queue=queue,
    )


def _memory_entry_scope(item: Item, memory_entry: MemoryEntry | None = None) -> MemoryScope | None:
    if memory_entry is not None:
        try:
            return MemoryScope(type=memory_entry.scope_type, key=memory_entry.scope_key)
        except ValueError:
            return None
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


def _memory_entry_source(item: Item, memory_entry: MemoryEntry | None = None) -> str | None:
    if memory_entry is not None:
        return memory_entry.source if memory_entry.source and memory_entry.source.strip() else None
    memory_entry = (item.metadata_ or {}).get("memory_entry")
    if not isinstance(memory_entry, dict):
        return None
    source = memory_entry.get("source")
    return source if isinstance(source, str) and source.strip() else None


def _memory_entry_source_project(item: Item, memory_entry: MemoryEntry | None = None) -> str | None:
    if memory_entry is not None:
        return source_project_from_memory_metadata({"memory_entry": {"metadata": memory_entry.metadata_ or {}}})
    return source_project_from_memory_metadata(item.metadata_)


def _serialize_memory_entry_list_item(
    item: Item,
    job: Job | None,
    memory_entry: MemoryEntry | None = None,
) -> MemoryEntryListItem | None:
    scope = _memory_entry_scope(item, memory_entry)
    if scope is None:
        return None
    mapped_job_status = MEMORY_JOB_PUBLIC_STATUS_MAP.get(job.status, job.status) if job else None
    tags = item.tags or []
    system_tags, semantic_tags = split_system_provenance_tags(tags)
    return MemoryEntryListItem(
        entry_id=memory_entry.id if memory_entry else None,
        source_item_id=item.id,
        title=item.title,
        summary=item.summary,
        source=_memory_entry_source(item, memory_entry),
        source_url=memory_entry.source_url if memory_entry else item.source_url,
        scope=scope,
        tags=tags,
        system_tags=system_tags,
        semantic_tags=semantic_tags,
        source_project=_memory_entry_source_project(item, memory_entry),
        valid_from=memory_entry.valid_from if memory_entry else None,
        valid_until=memory_entry.valid_until if memory_entry else None,
        supersedes_entry_id=memory_entry.supersedes_entry_id if memory_entry else None,
        superseded_by_entry_id=memory_entry.superseded_by_entry_id if memory_entry else None,
        fact_kind=memory_entry.fact_kind if memory_entry else None,
        created_at=item.created_at,
        updated_at=item.updated_at,
        readiness_state=item.status,
        job_id=job.id if job else None,
        job_status=mapped_job_status,
    )


def _json_text_path(column, *path: str):
    expression = column
    for key in path[:-1]:
        expression = expression[key]
    return expression[path[-1]].astext


async def list_memory_entries(
    db: AsyncSession,
    *,
    tenant_id: str,
    scope: MemoryScope,
    tags: list[str] | None = None,
    tags_mode: TagsMode = "any",
    limit: int = 20,
    cursor: datetime | None = None,
) -> MemoryEntryListResponse:
    scope_type_expr = func.coalesce(MemoryEntry.scope_type, _json_text_path(Item.metadata_, "memory_entry", "scope", "type"))
    scope_key_expr = func.coalesce(MemoryEntry.scope_key, _json_text_path(Item.metadata_, "memory_entry", "scope", "key"))
    memory_entry_expr = Item.metadata_["memory_entry"]

    query = (
        select(Item, Job, MemoryEntry)
        .outerjoin(
            MemoryEntry,
            and_(
                MemoryEntry.item_id == Item.id,
                MemoryEntry.tenant_id == tenant_id,
            ),
        )
        .outerjoin(
            Job,
            and_(
                Job.item_id == Item.id,
                Job.tenant_id == tenant_id,
                Job.job_type == MEMORY_JOB_TYPE,
            ),
        )
        .where(Item.tenant_id == tenant_id)
        .where(Item.deleted_at.is_(None))
        .where(Item.status != "deleted")
        .where(or_(MemoryEntry.id.is_not(None), memory_entry_expr.is_not(None)))
        .where(scope_type_expr == scope.type)
    )
    if scope.key is None:
        query = query.where(or_(scope_key_expr.is_(None), scope_key_expr == "null"))
    else:
        query = query.where(scope_key_expr == scope.key)
    if tags:
        tag_param = bindparam("memory_entry_tags", tags, type_=ARRAY(Text))
        if tags_mode == "all":
            query = query.where(Item.tags.op("@>")(tag_param))
        else:
            query = query.where(Item.tags.op("&&")(tag_param))
    if cursor:
        query = query.where(Item.created_at < cursor)

    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar_one()
    rows = (
        await db.execute(
            query.order_by(Item.created_at.desc(), Item.id.desc()).limit(limit + 1)
        )
    ).all()

    entries: list[MemoryEntryListItem] = []
    for item, job, memory_entry in rows[:limit]:
        entry = _serialize_memory_entry_list_item(item, job, memory_entry)
        if entry is not None:
            entries.append(entry)

    next_cursor = rows[limit][0].created_at if len(rows) > limit else None
    return MemoryEntryListResponse(
        entries=entries,
        total=total,
        limit=limit,
        next_cursor=next_cursor,
    )


async def list_memory_scopes(
    db: AsyncSession,
    *,
    tenant_id: str,
    limit: int = 50,
    sample_limit: int = 8,
) -> MemoryScopeListResponse:
    result = await db.execute(
        text(
            """
            WITH memory_items AS (
                SELECT
                    COALESCE(me.scope_type, i.metadata->'memory_entry'->'scope'->>'type', 'tenant_shared') AS scope_type,
                    CASE
                        WHEN COALESCE(me.scope_type, i.metadata->'memory_entry'->'scope'->>'type', 'tenant_shared') = 'tenant_shared'
                            THEN NULL
                        ELSE COALESCE(me.scope_key, NULLIF(i.metadata->'memory_entry'->'scope'->>'key', ''))
                    END AS scope_key,
                    i.created_at,
                    i.updated_at,
                    COALESCE(i.tags, ARRAY[]::varchar[]) AS tags,
                    COALESCE(me.source, NULLIF(i.metadata->'memory_entry'->>'source', '')) AS source
                FROM items i
                LEFT JOIN memory_entries me
                  ON me.item_id = i.id
                 AND me.tenant_id = :tenant_id
                WHERE i.tenant_id = :tenant_id
                  AND i.deleted_at IS NULL
                  AND i.status != 'deleted'
                  AND (me.id IS NOT NULL OR i.metadata->'memory_entry' IS NOT NULL)
            ),
            grouped AS (
                SELECT
                    scope_type,
                    scope_key,
                    COUNT(*) AS entry_count,
                    MAX(created_at) AS latest_created_at,
                    MAX(updated_at) AS latest_updated_at
                FROM memory_items
                GROUP BY scope_type, scope_key
            ),
            profiles AS (
                SELECT
                    scope_type,
                    scope_key,
                    retain_mission,
                    quiet_recall,
                    created_at AS profile_created_at,
                    updated_at AS profile_updated_at,
                    created_by,
                    updated_by
                FROM memory_scope_profiles
                WHERE tenant_id = :tenant_id
            )
            SELECT
                COALESCE(g.scope_type, p.scope_type) AS scope_type,
                CASE
                    WHEN COALESCE(g.scope_type, p.scope_type) = 'tenant_shared' THEN NULL
                    ELSE COALESCE(g.scope_key, p.scope_key)
                END AS scope_key,
                COALESCE(g.entry_count, 0) AS entry_count,
                g.latest_created_at,
                g.latest_updated_at,
                COALESCE(p.retain_mission, '') AS retain_mission,
                COALESCE(p.quiet_recall, false) AS quiet_recall,
                p.profile_created_at,
                p.profile_updated_at,
                p.created_by,
                p.updated_by,
                COALESCE(
                    (
                        SELECT array_agg(tag ORDER BY tag)
                        FROM (
                            SELECT DISTINCT unnest(mi.tags) AS tag
                            FROM memory_items mi
                            WHERE mi.scope_type = g.scope_type
                              AND mi.scope_key IS NOT DISTINCT FROM g.scope_key
                            LIMIT :sample_limit
                        ) tag_rows
                    ),
                    ARRAY[]::text[]
                ) AS tags,
                COALESCE(
                    (
                        SELECT array_agg(source ORDER BY source)
                        FROM (
                            SELECT DISTINCT mi.source AS source
                            FROM memory_items mi
                            WHERE mi.scope_type = g.scope_type
                              AND mi.scope_key IS NOT DISTINCT FROM g.scope_key
                              AND mi.source IS NOT NULL
                            LIMIT :sample_limit
                        ) source_rows
                    ),
                    ARRAY[]::text[]
                ) AS sources
            FROM grouped g
            FULL OUTER JOIN profiles p
              ON p.scope_type = g.scope_type
             AND p.scope_key IS NOT DISTINCT FROM g.scope_key
            ORDER BY GREATEST(g.latest_updated_at, p.profile_updated_at) DESC NULLS LAST,
                     COALESCE(g.scope_type, p.scope_type),
                     COALESCE(g.scope_key, p.scope_key) NULLS FIRST
            LIMIT :limit
            """
        ),
        {
            "tenant_id": tenant_id,
            "limit": limit,
            "sample_limit": sample_limit,
        },
    )
    rows = result.mappings().all()
    scopes: list[MemoryScopeSummary] = []
    for row in rows:
        scope_type = row["scope_type"] or "tenant_shared"
        scope_key = row["scope_key"] if scope_type != "tenant_shared" else None
        scope = MemoryScope(type=scope_type, key=scope_key)
        scopes.append(
            MemoryScopeSummary(
                scope=scope,
                entry_count=int(row["entry_count"] or 0),
                latest_created_at=row["latest_created_at"],
                latest_updated_at=row["latest_updated_at"],
                tags=list(row["tags"] or []),
                sources=list(row["sources"] or []),
                profile=_memory_scope_profile_from_row(row, scope=scope),
            )
        )
    return MemoryScopeListResponse(scopes=scopes, total=len(scopes), limit=limit)


async def get_memory_scope_profile(
    db: AsyncSession,
    *,
    tenant_id: str,
    scope: MemoryScope,
) -> MemoryScopeProfile:
    result = await db.execute(
        text(
            """
            SELECT
                scope_type,
                scope_key,
                retain_mission,
                quiet_recall,
                created_at AS profile_created_at,
                updated_at AS profile_updated_at,
                created_by,
                updated_by
            FROM memory_scope_profiles
            WHERE tenant_id = :tenant_id
              AND scope_type = :scope_type
              AND scope_key IS NOT DISTINCT FROM :scope_key
            """
        ),
        {
            "tenant_id": tenant_id,
            "scope_type": scope.type,
            "scope_key": scope.key if scope.type != "tenant_shared" else None,
        },
    )
    row = result.mappings().one_or_none()
    if row is None:
        return _default_memory_scope_profile(scope)
    return _memory_scope_profile_from_row(row, scope=scope)


async def upsert_memory_scope_profile(
    db: AsyncSession,
    *,
    tenant_id: str,
    body: MemoryScopeProfileUpsertRequest,
) -> MemoryScopeProfile:
    scope_key = body.scope.key if body.scope.type != "tenant_shared" else None
    result = await db.execute(
        text(
            """
            INSERT INTO memory_scope_profiles (
                tenant_id,
                scope_type,
                scope_key,
                retain_mission,
                quiet_recall,
                created_by,
                updated_by
            )
            VALUES (
                :tenant_id,
                :scope_type,
                :scope_key,
                :retain_mission,
                :quiet_recall,
                :updated_by,
                :updated_by
            )
            ON CONFLICT (tenant_id, scope_type, (coalesce(scope_key, '')))
            DO UPDATE SET
                retain_mission = EXCLUDED.retain_mission,
                quiet_recall = EXCLUDED.quiet_recall,
                updated_by = EXCLUDED.updated_by,
                updated_at = now()
            RETURNING
                scope_type,
                scope_key,
                retain_mission,
                quiet_recall,
                created_at AS profile_created_at,
                updated_at AS profile_updated_at,
                created_by,
                updated_by
            """
        ),
        {
            "tenant_id": tenant_id,
            "scope_type": body.scope.type,
            "scope_key": scope_key,
            "retain_mission": body.retain_mission,
            "quiet_recall": body.quiet_recall,
            "updated_by": body.updated_by,
        },
    )
    await db.commit()
    return _memory_scope_profile_from_row(result.mappings().one(), scope=body.scope)


async def retrieve_memory(
    db: AsyncSession,
    *,
    embedder,
    tenant_id: str,
    body: MemoryRetrieveRequest,
    query_vector: list[float] | None = None,
) -> MemoryRetrieveResponse:
    result = await retrieve_palace(
        db,
        tenant_id=tenant_id,
        embedder=embedder,
        body=PalaceRetrieveRequest(
            query=body.query,
            room_id=body.room_id,
            limit=body.limit,
            candidate_limit=body.candidate_limit,
            include_neighbor_chunks=body.include_neighbor_chunks,
            neighbor_chunk_window=body.neighbor_chunk_window,
            context_budget_chars=body.context_budget_chars,
            include_derived_artifacts=body.include_derived_artifacts,
            retrieval_lens=body.retrieval_lens,
            scope_type=body.scope.type,
            scope_key=body.scope.key,
            tags=body.tags,
            tags_mode=body.tags_mode,
            min_score=body.min_score,
            date_from=body.date_from,
            date_to=body.date_to,
        ),
        query_vector=query_vector,
    )
    return MemoryRetrieveResponse(
        scope=body.scope,
        routed_room_id=result.routed_room_id,
        redirected_from_room_id=result.redirected_from_room_id,
        trace=result.trace,
        results=result.results,
        total=result.total,
    )


def _append_scope_once(scopes: list[MemoryScope], scope: MemoryScope) -> None:
    if all(existing.type != scope.type or existing.key != scope.key for existing in scopes):
        scopes.append(scope)


_SEMANTIC_RECALL_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_-]{1,}")
_SEMANTIC_RECALL_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "at",
    "did",
    "do",
    "does",
    "for",
    "from",
    "i",
    "last",
    "me",
    "of",
    "on",
    "please",
    "show",
    "tell",
    "the",
    "this",
    "ticket",
    "tickets",
    "to",
    "was",
    "week",
    "what",
    "when",
    "which",
    "who",
    "why",
}


def _semantic_recall_tokens(query: str) -> set[str]:
    return {
        token
        for token in _SEMANTIC_RECALL_TOKEN_RE.findall(query.casefold())
        if len(token) > 1 and token not in _SEMANTIC_RECALL_STOPWORDS
    }


def _semantic_recall_score(item: Item, *, query_tokens: set[str]) -> float:
    if not query_tokens:
        return 0.0
    haystack = " ".join(
        part.casefold()
        for part in (
            item.title,
            item.summary or "",
            item.raw_content or "",
            " ".join(item.tags or []),
        )
        if part
    )
    if not haystack:
        return 0.0
    matched = sum(1 for token in query_tokens if token in haystack)
    return round(matched / len(query_tokens), 4)


def _normalize_aware_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _semantic_temporal_status(
    memory_entry: MemoryEntry,
    *,
    now: datetime,
    valid_at: datetime | None,
) -> str:
    valid_from = _normalize_aware_datetime(memory_entry.valid_from)
    valid_until = _normalize_aware_datetime(memory_entry.valid_until)
    reference = _normalize_aware_datetime(valid_at) or now
    if (
        memory_entry.superseded_by_entry_id is not None
        and (valid_at is None or (valid_until is not None and valid_until <= reference))
    ):
        return "superseded"
    if valid_from is not None and valid_from > reference:
        return "future"
    if valid_until is not None and valid_until <= reference:
        return "historical"
    return "current"


def _semantic_temporal_rank(status: str) -> int:
    return {
        "current": 0,
        "superseded": 1,
        "historical": 2,
        "future": 3,
    }.get(status, 4)


def _serialize_semantic_recall_item(
    item: Item,
    memory_entry: MemoryEntry,
    *,
    score: float,
    temporal_status: str,
) -> SemanticRecallItem:
    tags = item.tags or []
    system_tags, semantic_tags = split_system_provenance_tags(tags)
    return SemanticRecallItem(
        entry_id=memory_entry.id,
        source_item_id=item.id,
        title=item.title,
        summary=item.summary,
        body=item.raw_content,
        source=memory_entry.source,
        source_url=memory_entry.source_url or item.source_url,
        scope=MemoryScope(type=memory_entry.scope_type, key=memory_entry.scope_key),
        tags=tags,
        system_tags=system_tags,
        semantic_tags=semantic_tags,
        created_at=item.created_at,
        valid_from=memory_entry.valid_from,
        valid_until=memory_entry.valid_until,
        supersedes_entry_id=memory_entry.supersedes_entry_id,
        superseded_by_entry_id=memory_entry.superseded_by_entry_id,
        fact_kind=memory_entry.fact_kind,
        score=score,
        temporal_status=temporal_status,
    )


def _apply_semantic_budget(
    items: list[SemanticRecallItem],
    *,
    budget_chars: int | None,
) -> tuple[list[SemanticRecallItem], bool]:
    if budget_chars is None:
        return items, False
    selected: list[SemanticRecallItem] = []
    used_chars = 0
    for item in items:
        estimated_chars = len(item.title) + len(item.summary or "") + len(item.body or "")
        if used_chars + estimated_chars > budget_chars:
            if not selected:
                title_budget = max(0, budget_chars)
                title = _truncate_semantic_text(item.title, title_budget)
                summary_budget = max(0, budget_chars - len(title))
                summary = _truncate_semantic_text(item.summary or "", summary_budget) or None
                body_budget = max(0, budget_chars - len(title) - len(summary or ""))
                body = _truncate_semantic_text(item.body or "", body_budget) or None
                item = item.model_copy(update={"title": title, "summary": summary, "body": body})
                selected.append(item)
            return selected, True
        selected.append(item)
        used_chars += estimated_chars
    return selected, False


def _truncate_semantic_text(value: str, budget_chars: int) -> str:
    if budget_chars <= 0:
        return ""
    if len(value) <= budget_chars:
        return value
    suffix = "..." if budget_chars >= 3 else ""
    return value[: max(0, budget_chars - 3)].rstrip() + suffix


def _semantic_context_budget_chars(body: SemanticRecallRequest) -> int | None:
    token_budget_chars = body.recall_max_tokens * 4 if body.recall_max_tokens is not None else None
    budgets = [budget for budget in (token_budget_chars, body.context_budget_chars) if budget is not None]
    return min(budgets) if budgets else None


def _semantic_sql_score_expression(query_tokens: set[str]):
    haystack = func.lower(
        func.concat(
            Item.title,
            " ",
            func.coalesce(Item.summary, ""),
            " ",
            func.coalesce(Item.raw_content, ""),
            " ",
            func.coalesce(func.array_to_string(Item.tags, " "), ""),
        )
    )
    if not query_tokens:
        return literal(0)
    expression = 0
    for token in sorted(query_tokens):
        expression = expression + case((haystack.contains(token, autoescape=True), 1), else_=0)
    return expression


def _semantic_temporal_rank_expression(reference: datetime, *, valid_at: datetime | None):
    superseded_condition = MemoryEntry.superseded_by_entry_id.is_not(None)
    if valid_at is not None:
        superseded_condition = and_(
            superseded_condition,
            MemoryEntry.valid_until.is_not(None),
            MemoryEntry.valid_until <= reference,
        )
    return case(
        (superseded_condition, 1),
        (and_(MemoryEntry.valid_from.is_not(None), MemoryEntry.valid_from > reference), 3),
        (and_(MemoryEntry.valid_until.is_not(None), MemoryEntry.valid_until <= reference), 2),
        else_=0,
    )


def _semantic_source_rank(item: Item) -> int:
    metadata = item.metadata_ or {}
    if any(isinstance(metadata.get(key), dict) for key in ("diary_rollup", "wakeup_brief", "memory_dream")):
        return 2
    if item.raw_content and item.raw_content.strip():
        return 0
    return 1


def _semantic_source_rank_expression():
    derived_metadata = or_(
        Item.metadata_.op("?")("diary_rollup"),
        Item.metadata_.op("?")("wakeup_brief"),
        Item.metadata_.op("?")("memory_dream"),
    )
    has_source_body = func.length(func.btrim(func.coalesce(Item.raw_content, ""))) > 0
    return case(
        (derived_metadata, 2),
        (has_source_body, 0),
        else_=1,
    )


async def semantic_recall_memory(
    db: AsyncSession,
    *,
    tenant_id: str,
    body: SemanticRecallRequest,
) -> SemanticRecallResponse:
    scope = MemoryScope(type=body.scope_type, key=body.scope_key)
    candidate_limit = body.candidate_limit or 50
    query_tokens = _semantic_recall_tokens(body.query)
    sql_score = _semantic_sql_score_expression(query_tokens).label("semantic_score")
    valid_at = _normalize_aware_datetime(body.valid_at)
    now = datetime.now(timezone.utc)
    temporal_rank = _semantic_temporal_rank_expression(valid_at or now, valid_at=valid_at)
    source_rank = _semantic_source_rank_expression()

    query = (
        select(Item, MemoryEntry, sql_score)
        .join(
            MemoryEntry,
            and_(
                MemoryEntry.item_id == Item.id,
                MemoryEntry.tenant_id == tenant_id,
            ),
        )
        .where(Item.tenant_id == tenant_id)
        .where(Item.deleted_at.is_(None))
        .where(Item.status != "deleted")
        .where(MemoryEntry.scope_type == scope.type)
    )
    if scope.key is None:
        query = query.where(MemoryEntry.scope_key.is_(None))
    else:
        query = query.where(MemoryEntry.scope_key == scope.key)
    if body.fact_kind_filter:
        query = query.where(MemoryEntry.fact_kind.in_(body.fact_kind_filter))
    if valid_at is not None:
        query = query.where(or_(MemoryEntry.valid_from.is_(None), MemoryEntry.valid_from <= valid_at))
        query = query.where(or_(MemoryEntry.valid_until.is_(None), MemoryEntry.valid_until > valid_at))
    if body.date_from is not None:
        query = query.where(Item.created_at >= body.date_from)
    if body.date_to is not None:
        query = query.where(Item.created_at <= body.date_to)
    if query_tokens:
        query = query.where(sql_score > 0)
        if body.score_threshold is not None:
            query = query.where(sql_score >= body.score_threshold * len(query_tokens))

    count_query = select(func.count()).select_from(query.subquery())
    total_considered = (await db.execute(count_query)).scalar_one()
    rows = (
        await db.execute(
            query.order_by(
                temporal_rank.asc(),
                source_rank.asc(),
                sql_score.desc(),
                MemoryEntry.valid_from.desc().nulls_last(),
                Item.created_at.desc(),
                Item.id.desc(),
            ).limit(candidate_limit)
        )
    ).all()

    ranked: list[tuple[SemanticRecallItem, int, int]] = []
    for row in rows:
        item, memory_entry = row[0], row[1]
        if len(row) > 2:
            raw_score = float(row[2] or 0)
            score = round(raw_score / len(query_tokens), 4) if query_tokens else 0.0
        else:
            score = _semantic_recall_score(item, query_tokens=query_tokens)
        if query_tokens and score <= 0:
            continue
        if body.score_threshold is not None and score < body.score_threshold:
            continue
        temporal_status = _semantic_temporal_status(memory_entry, now=now, valid_at=valid_at)
        serialized = _serialize_semantic_recall_item(
            item,
            memory_entry,
            score=score,
            temporal_status=temporal_status,
        )
        ranked.append((serialized, _semantic_temporal_rank(temporal_status), _semantic_source_rank(item)))

    ranked.sort(
        key=lambda entry: (
            entry[1],
            entry[2],
            -entry[0].score,
            entry[0].valid_from is None,
            -(entry[0].valid_from or entry[0].created_at).timestamp(),
            -entry[0].created_at.timestamp(),
            str(entry[0].entry_id),
        )
    )
    selected = [item for item, _, _ in ranked[: body.top_k]]
    budget = _semantic_context_budget_chars(body)
    selected, budget_truncated = _apply_semantic_budget(selected, budget_chars=budget)
    trace = SemanticRecallTrace(
        searched_scope=scope,
        valid_at=valid_at,
        fact_kind_filter=list(body.fact_kind_filter or []),
        total_considered=total_considered,
        candidate_limit=candidate_limit,
        display_limit=body.top_k,
        score_threshold=body.score_threshold,
        date_from=body.date_from,
        date_to=body.date_to,
        budget_truncated=budget_truncated,
    )
    return SemanticRecallResponse(
        scope=scope,
        items=selected,
        total=len(selected),
        total_considered=total_considered,
        trace=trace,
    )


def _result_tags(result: object) -> set[str]:
    tags: set[str] = set()
    for attr in ("tags", "system_tags", "semantic_tags"):
        for tag in getattr(result, attr, None) or []:
            normalized = str(tag).strip().casefold()
            if normalized:
                tags.add(normalized)
    return tags


def _looks_like_agent_conversation_recall(result: object, *, preferred_workspace_keys: set[str]) -> bool:
    if not preferred_workspace_keys:
        return False

    tags = _result_tags(result)
    if "scope-agent" not in tags:
        return False

    title = str(getattr(result, "title", "") or "").casefold()
    chunk_text = str(getattr(result, "chunk_text", "") or "").casefold()
    text = f"{title}\n{chunk_text}"
    return any(
        marker in text
        for marker in (
            "# conversation turn",
            "[andrew]",
            "asked:",
            "assistant:",
            "user:",
        )
    )


def _agent_memory_result_priority(result: object, *, preferred_workspace_keys: set[str]) -> int:
    if _looks_like_agent_conversation_recall(
        result,
        preferred_workspace_keys=preferred_workspace_keys,
    ):
        return 1
    return 0


def _has_preferred_agent_scope(result: object, preferred_agent_keys: set[str]) -> bool:
    if not preferred_agent_keys:
        return False

    retrieved_scope_type = str(getattr(result, "retrieved_scope_type", "") or "").casefold()
    retrieved_scope_key = str(getattr(result, "retrieved_scope_key", "") or "").casefold()
    retrieved_scope_label = str(getattr(result, "retrieved_scope_label", "") or "").casefold()
    if retrieved_scope_type == "agent" and retrieved_scope_key in preferred_agent_keys:
        return True
    if retrieved_scope_label in {f"agent/{key}" for key in preferred_agent_keys}:
        return True
    return False


def _agent_memory_scope_priority(
    result: object,
    *,
    preferred_workspace_keys: set[str],
    preferred_agent_keys: set[str],
) -> int:
    tags = _result_tags(result)
    for key in preferred_workspace_keys:
        if f"workspace-{key}" in tags:
            return 0
    if _has_preferred_agent_scope(result, preferred_agent_keys):
        return 0
    if "scope-tenant_shared" in tags:
        return 2
    return 3


def _agent_memory_trace_warning(warning: str) -> str:
    if "Global fallback used" in warning and "room-scoped retrieval" in warning:
        return "Selected scoped retrieval reported low route confidence."
    return warning


def _merge_search_results(
    results_by_route: list[list],
    *,
    preferred_workspace_keys: set[str] | None = None,
    preferred_agent_keys: set[str] | None = None,
) -> list:
    preferred_workspace_keys = preferred_workspace_keys or set()
    preferred_agent_keys = preferred_agent_keys or set()
    best_by_item_id: dict[uuid.UUID, object] = {}
    order: list[uuid.UUID] = []
    for results in results_by_route:
        for result in results:
            item_id = result.item_id
            existing = best_by_item_id.get(item_id)
            if existing is None:
                order.append(item_id)
                best_by_item_id[item_id] = result
                continue
            existing_score = getattr(existing, "score", None)
            incoming_score = getattr(result, "score", None)
            if incoming_score is not None and (existing_score is None or incoming_score > existing_score):
                best_by_item_id[item_id] = result
    return sorted(
        (best_by_item_id[item_id] for item_id in order),
        key=lambda result: (
            _agent_memory_scope_priority(
                result,
                preferred_workspace_keys=preferred_workspace_keys,
                preferred_agent_keys=preferred_agent_keys,
            ),
            _agent_memory_result_priority(
                result,
                preferred_workspace_keys=preferred_workspace_keys,
            ),
            getattr(result, "score", None) is None,
            -(getattr(result, "score", 0.0) or 0.0),
        ),
    )


def _effective_agent_memory_budgets(body: AgentMemoryRetrieveRequest) -> tuple[int, int, int]:
    display_limit = body.display_limit or body.limit
    selected_candidate_limit = body.candidate_limit or min(50, max(display_limit * 4, body.limit))
    broad_candidate_limit = body.broad_candidate_limit or selected_candidate_limit
    return selected_candidate_limit, broad_candidate_limit, display_limit


def _apply_context_budget(results: list, budget_chars: int | None) -> tuple[list, bool]:
    if budget_chars is None:
        return results, False

    selected: list = []
    used_chars = 0
    for result in results:
        title = str(getattr(result, "title", "") or "")
        summary = str(getattr(result, "summary", "") or "")
        chunk = str(getattr(result, "chunk_text", "") or "")
        estimated_chars = len(title) + len(summary) + len(chunk)
        if used_chars + estimated_chars > budget_chars:
            if not selected:
                remaining = max(0, budget_chars - len(title) - len(summary))
                if len(chunk) > remaining:
                    suffix = "..." if remaining >= 3 else ""
                    chunk = chunk[: max(0, remaining - 3)].rstrip() + suffix
                    result = result.model_copy(update={"chunk_text": chunk})
                selected.append(result)
            return selected, True
        selected.append(result)
        used_chars += estimated_chars
        if used_chars >= budget_chars:
            return selected, len(selected) < len(results)
    return selected, False


async def _agent_memory_query_vector(embedder, query: str) -> tuple[list[float] | None, bool]:
    embed_single = getattr(embedder, "embed_single", None)
    if embed_single is None:
        return None, False
    return await embed_single(query), True


def _has_preferred_workspace_tag(result: object, preferred_workspace_keys: set[str]) -> bool:
    tags = _result_tags(result)
    return any(f"workspace-{key}" in tags for key in preferred_workspace_keys)


def _scoped_results_satisfy_agent_memory_request(
    results: list,
    *,
    display_limit: int,
    preferred_workspace_keys: set[str],
    fallback_used: bool,
    warnings: list[str],
) -> bool:
    if fallback_used or warnings or not preferred_workspace_keys:
        return False
    displayed = results[:display_limit]
    if len(displayed) < display_limit:
        return False
    return all(
        _has_preferred_workspace_tag(result, preferred_workspace_keys)
        for result in displayed
    )


def _agent_memory_selected_scopes(body: AgentMemoryRetrieveRequest) -> list[MemoryScope]:
    scopes: list[MemoryScope] = []
    if body.workspace_strict:
        for workspace_key in body.workspace_scope_keys:
            _append_scope_once(scopes, MemoryScope(type="workspace", key=workspace_key))
        return scopes

    if body.agent_scope_key:
        _append_scope_once(scopes, MemoryScope(type="agent", key=body.agent_scope_key))
    for workspace_key in body.workspace_scope_keys:
        _append_scope_once(scopes, MemoryScope(type="workspace", key=workspace_key))
    if body.session_scope_key:
        _append_scope_once(scopes, MemoryScope(type="session", key=body.session_scope_key))
    return scopes


def _dedupe_scope_keys(keys: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    deduped: list[str] = []
    seen: set[str] = set()
    for key in keys:
        stripped = key.strip()
        folded = stripped.casefold()
        if not stripped or folded in seen:
            continue
        seen.add(folded)
        deduped.append(stripped)
    return tuple(deduped)


_AGENT_SCOPE_QUERY_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_-]{1,}")


def _agent_scope_pattern_matches(pattern: str, key: str) -> bool:
    normalized_pattern = pattern.strip().casefold()
    normalized_key = key.strip().casefold()
    if normalized_pattern.startswith("agent/"):
        normalized_pattern = normalized_pattern.removeprefix("agent/")
    if normalized_pattern in {"*", "agent/*"}:
        return True
    if normalized_pattern.endswith("*"):
        return normalized_key.startswith(normalized_pattern[:-1])
    return normalized_key == normalized_pattern


def _agent_scope_relevance_score(
    summary: MemoryScopeSummary,
    *,
    query_tokens: set[str],
) -> int:
    key = (summary.scope.key or "").casefold()
    haystack_parts = [key, *summary.tags, *summary.sources]
    haystack = " ".join(part.casefold() for part in haystack_parts if part)
    score = 0
    for token in query_tokens:
        if token == key:
            score += 4
        elif token in key or key in token:
            score += 3
        elif token in haystack:
            score += 1
    return score


async def _resolve_agent_scope_patterns(
    db: AsyncSession,
    *,
    tenant_id: str,
    body: AgentMemoryRetrieveRequest,
) -> AgentScopePatternResolution:
    requested_patterns = _dedupe_scope_keys(body.include_agent_scope_patterns)
    if not requested_patterns:
        return AgentScopePatternResolution()

    scope_response = await list_memory_scopes(
        db,
        tenant_id=tenant_id,
        limit=max(100, body.agent_scope_pattern_limit * 10),
        sample_limit=5,
    )
    agent_summaries = [summary for summary in scope_response.scopes if summary.scope.type == "agent" and summary.scope.key]
    discovered_keys = _dedupe_scope_keys([summary.scope.key or "" for summary in agent_summaries])
    matched_summaries = [
        summary
        for summary in agent_summaries
        if any(_agent_scope_pattern_matches(pattern, summary.scope.key or "") for pattern in requested_patterns)
    ]
    caller_key = body.agent_scope_key.strip().casefold() if body.agent_scope_key else None
    query_tokens = {
        token
        for token in _AGENT_SCOPE_QUERY_TOKEN_RE.findall(body.query.casefold())
        if len(token) > 1
    }
    ranked_matches = sorted(
        enumerate(matched_summaries),
        key=lambda entry: (
            -_agent_scope_relevance_score(entry[1], query_tokens=query_tokens),
            entry[0],
        ),
    )
    matched_keys = _dedupe_scope_keys([summary.scope.key or "" for summary in matched_summaries])
    candidate_keys = [
        summary.scope.key or ""
        for _, summary in ranked_matches
        if (summary.scope.key or "").casefold() != caller_key
    ]
    selected_keys = list(_dedupe_scope_keys(candidate_keys)[: body.agent_scope_pattern_limit])
    selected_set = {key.casefold() for key in selected_keys}
    skipped_keys = [key for key in matched_keys if key.casefold() not in selected_set]
    skip_reasons: list[str] = []
    if caller_key and any(key.casefold() == caller_key for key in matched_keys):
        skip_reasons.append("caller_agent_scope_excluded")
    truncated = len(skipped_keys) > 0
    if truncated:
        skip_reasons.append("agent_scope_pattern_limit_exceeded")
    if not matched_keys:
        skip_reasons.append("no_agent_scopes_matched_patterns")
    return AgentScopePatternResolution(
        requested_patterns=requested_patterns,
        discovered_keys=discovered_keys,
        matched_keys=matched_keys,
        selected_keys=tuple(selected_keys),
        skipped_keys=tuple(skipped_keys),
        skip_reasons=tuple(skip_reasons),
        truncated=truncated,
    )


def _evaluate_delegated_agent_memory_policy(
    *,
    tenant_id: str,
    body: AgentMemoryRetrieveRequest,
    policy: DelegatedAgentMemoryReadPolicy | None,
    pattern_resolution: AgentScopePatternResolution | None = None,
) -> DelegatedAgentMemoryDecision:
    caller_agent_key = body.agent_scope_key.strip() if body.agent_scope_key else None
    requested = list(_dedupe_scope_keys(body.include_agent_scope_keys))
    if pattern_resolution is not None:
        requested.extend(pattern_resolution.selected_keys)
    if policy is not None and body.include_all_permitted_agent_scopes:
        requested.extend(policy.read_agent_scope_keys)
    requested_keys = tuple(key for key in _dedupe_scope_keys(tuple(requested)) if key != caller_agent_key)
    access_reason_present = bool(body.access_reason and body.access_reason.strip())
    if not requested_keys:
        return DelegatedAgentMemoryDecision(
            caller_agent_scope_key=caller_agent_key,
            requested_agent_scope_keys=(),
            authorized_agent_scope_keys=(),
            denied_agent_scope_keys=(),
            policy_id=policy.policy_id if policy else None,
            policy_source=policy.policy_source if policy else None,
            access_reason_required=bool(policy and policy.require_access_reason),
            access_reason_present=access_reason_present,
            decision="not_requested",
            deny_reasons=(),
            max_results_per_scope=policy.max_results_per_scope if policy else None,
        )
    if policy is None:
        return DelegatedAgentMemoryDecision(
            caller_agent_scope_key=caller_agent_key,
            requested_agent_scope_keys=requested_keys,
            authorized_agent_scope_keys=(),
            denied_agent_scope_keys=requested_keys,
            policy_id=None,
            policy_source=None,
            access_reason_required=False,
            access_reason_present=access_reason_present,
            decision="denied",
            deny_reasons=("no_delegated_agent_policy",),
        )

    deny_reasons: list[str] = []
    if policy.tenant_id != tenant_id:
        deny_reasons.append("policy_tenant_mismatch")
    if policy.subject_agent_scope_key and policy.subject_agent_scope_key != caller_agent_key:
        deny_reasons.append("caller_agent_scope_mismatch")
    if body.workspace_strict:
        deny_reasons.append("workspace_strict_blocks_agent_scopes")
    if policy.require_access_reason and not access_reason_present:
        deny_reasons.append("access_reason_required")

    allowed_keys = set(_dedupe_scope_keys(policy.read_agent_scope_keys))
    authorized: list[str] = []
    denied: list[str] = []
    if deny_reasons:
        denied = list(requested_keys)
    else:
        for key in requested_keys:
            if policy.allow_all_agent_scopes or key in allowed_keys:
                authorized.append(key)
            else:
                denied.append(key)
        if policy.max_cross_agent_scopes >= 0 and len(authorized) > policy.max_cross_agent_scopes:
            denied.extend(authorized[policy.max_cross_agent_scopes :])
            authorized = authorized[: policy.max_cross_agent_scopes]
            deny_reasons.append("max_cross_agent_scopes_exceeded")
        if denied and "agent_scope_not_allowlisted" not in deny_reasons:
            deny_reasons.append("agent_scope_not_allowlisted")

    if authorized and denied:
        decision = "partial"
    elif authorized:
        decision = "allowed"
    else:
        decision = "denied"
    return DelegatedAgentMemoryDecision(
        caller_agent_scope_key=caller_agent_key,
        requested_agent_scope_keys=requested_keys,
        authorized_agent_scope_keys=tuple(authorized),
        denied_agent_scope_keys=tuple(denied),
        policy_id=policy.policy_id,
        policy_source=policy.policy_source,
        access_reason_required=policy.require_access_reason,
        access_reason_present=access_reason_present,
        decision=decision,
        deny_reasons=tuple(deny_reasons),
        max_results_per_scope=policy.max_results_per_scope,
    )


def _scope_label_from_memory_scope(scope: MemoryScope) -> str:
    if scope.type == "tenant_shared":
        return "tenant_shared"
    return f"{scope.type}/{scope.key}"


def _route_result_counts_by_scope(scopes: list[MemoryScope], route_results: list[list]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for scope, results in zip(scopes, route_results):
        scope_label = _scope_label_from_memory_scope(scope)
        counts[scope_label] = counts.get(scope_label, 0) + len(results)
    return counts


def _should_search_initial_tenant_shared(body: AgentMemoryRetrieveRequest) -> bool:
    return body.include_tenant_shared and body.tenant_shared_policy == "always"


def _should_search_tenant_shared_fallback(
    body: AgentMemoryRetrieveRequest,
    *,
    selected_scope_result_count: int,
) -> bool:
    return (
        body.include_tenant_shared
        and body.tenant_shared_policy == "fallback_only"
        and selected_scope_result_count == 0
    )


def _should_search_broad_corpus(body: AgentMemoryRetrieveRequest) -> tuple[bool, str | None]:
    if not body.include_broad_corpus or body.broad_corpus_policy == "disabled":
        return False, "disabled_by_request"
    if body.workspace_strict and body.broad_corpus_policy != "enabled":
        return False, "workspace_strict_requires_explicit_broad_corpus_policy"
    return True, None


def _duration_ms(started_at: float) -> int:
    return max(0, round((perf_counter() - started_at) * 1000))


async def retrieve_agent_memory(
    db: AsyncSession,
    *,
    embedder,
    tenant_id: str,
    body: AgentMemoryRetrieveRequest,
    delegated_policy: DelegatedAgentMemoryReadPolicy | None = None,
) -> AgentMemoryRetrieveResponse:
    started_at = perf_counter()
    selected_candidate_limit, broad_candidate_limit, display_limit = _effective_agent_memory_budgets(
        body
    )
    pattern_resolution = await _resolve_agent_scope_patterns(db, tenant_id=tenant_id, body=body)
    delegated_decision = _evaluate_delegated_agent_memory_policy(
        tenant_id=tenant_id,
        body=body,
        policy=delegated_policy,
        pattern_resolution=pattern_resolution,
    )
    scopes = _agent_memory_selected_scopes(body)
    if not body.workspace_strict:
        for agent_scope_key in delegated_decision.authorized_agent_scope_keys:
            _append_scope_once(scopes, MemoryScope(type="agent", key=agent_scope_key))
    if _should_search_initial_tenant_shared(body):
        _append_scope_once(scopes, MemoryScope(type="tenant_shared"))

    route_results: list[list] = []
    selected_scope_warnings: list[str] = []
    selected_scope_fallback_used = False
    query_vector, query_embedding_reused = await _agent_memory_query_vector(embedder, body.query)
    selected_scopes_started_at = perf_counter()
    for scope in scopes:
        route_candidate_limit = selected_candidate_limit
        if (
            scope.type == "agent"
            and scope.key in delegated_decision.authorized_agent_scope_keys
            and delegated_decision.max_results_per_scope is not None
        ):
            route_candidate_limit = min(selected_candidate_limit, delegated_decision.max_results_per_scope)
        response = await retrieve_memory(
            db,
            embedder=embedder,
            tenant_id=tenant_id,
            body=MemoryRetrieveRequest(
                query=body.query,
                limit=route_candidate_limit,
                tags=body.tags,
                tags_mode=body.tags_mode,
                min_score=body.min_score,
                date_from=body.date_from,
                date_to=body.date_to,
                include_derived_artifacts=body.include_derived_artifacts,
                retrieval_lens=body.retrieval_lens,
                scope=scope,
            ),
            query_vector=query_vector,
        )
        route_results.append(response.results)
        selected_scope_fallback_used = selected_scope_fallback_used or response.trace.fallback_used
        if response.trace.completeness_warning:
            warning = _agent_memory_trace_warning(response.trace.completeness_warning)
            if warning not in selected_scope_warnings:
                selected_scope_warnings.append(warning)
    selected_scope_duration_ms = _duration_ms(selected_scopes_started_at)

    broad_corpus_searched = False
    broad_corpus_skipped_reason = None
    broad_corpus_duration_ms = None
    selected_route_count = len(scopes)
    selected_scope_result_count = sum(len(results) for results in route_results[:selected_route_count])
    tenant_shared_fallback_used = False
    if _should_search_tenant_shared_fallback(
        body,
        selected_scope_result_count=selected_scope_result_count,
    ):
        tenant_shared_scope = MemoryScope(type="tenant_shared")
        _append_scope_once(scopes, tenant_shared_scope)
        response = await retrieve_memory(
            db,
            embedder=embedder,
            tenant_id=tenant_id,
            body=MemoryRetrieveRequest(
                query=body.query,
                limit=selected_candidate_limit,
                tags=body.tags,
                tags_mode=body.tags_mode,
                min_score=body.min_score,
                date_from=body.date_from,
                date_to=body.date_to,
                include_derived_artifacts=body.include_derived_artifacts,
                retrieval_lens=body.retrieval_lens,
                scope=tenant_shared_scope,
            ),
            query_vector=query_vector,
        )
        route_results.append(response.results)
        selected_scope_result_count += len(response.results)
        tenant_shared_fallback_used = True
        selected_scope_fallback_used = selected_scope_fallback_used or response.trace.fallback_used
        if response.trace.completeness_warning:
            warning = _agent_memory_trace_warning(response.trace.completeness_warning)
            if warning not in selected_scope_warnings:
                selected_scope_warnings.append(warning)
    workspace_scope_exhausted = (
        body.workspace_strict
        and bool(body.workspace_scope_keys)
        and sum(
            len(results)
            for scope, results in zip(scopes, route_results)
            if scope.type == "workspace"
        )
        == 0
    )
    preferred_workspace_keys = {key.casefold() for key in body.workspace_scope_keys}
    preferred_agent_keys = {key.casefold() for key in delegated_decision.authorized_agent_scope_keys}
    if not preferred_agent_keys and body.agent_scope_key:
        preferred_agent_keys.add(body.agent_scope_key.casefold())
    scoped_deduped_results = _merge_search_results(
        route_results,
        preferred_workspace_keys=preferred_workspace_keys,
        preferred_agent_keys=preferred_agent_keys,
    )
    allow_broad_corpus, broad_policy_skip_reason = _should_search_broad_corpus(body)
    if allow_broad_corpus and _scoped_results_satisfy_agent_memory_request(
        scoped_deduped_results,
        display_limit=display_limit,
        preferred_workspace_keys=preferred_workspace_keys,
        fallback_used=selected_scope_fallback_used,
        warnings=selected_scope_warnings,
    ):
        broad_corpus_skipped_reason = "preferred_workspace_results_satisfied_display_limit"
    elif allow_broad_corpus:
        broad_started_at = perf_counter()
        broad_corpus_searched = True
        search_service = SearchService(db, embedder, tenant_id=tenant_id)
        route_results.append(
            await search_service.vector_search(
                query=body.query,
                limit=broad_candidate_limit,
                retrieval_lens=body.retrieval_lens,
                tags=body.tags,
                tags_mode=body.tags_mode,
                min_score=body.min_score,
                date_from=body.date_from,
                date_to=body.date_to,
                exclude_private_memory_scopes=True,
                include_derived_artifacts=body.include_derived_artifacts,
                query_vector=query_vector,
            )
        )
        broad_corpus_duration_ms = _duration_ms(broad_started_at)
    elif broad_policy_skip_reason is not None:
        broad_corpus_skipped_reason = broad_policy_skip_reason

    broad_result_count = len(route_results[-1]) if broad_corpus_searched and route_results else 0
    merge_started_at = perf_counter()
    deduped_results = _merge_search_results(
        route_results,
        preferred_workspace_keys=preferred_workspace_keys,
        preferred_agent_keys=preferred_agent_keys,
    )
    displayed_results = deduped_results[:display_limit]
    displayed_results, context_budget_truncated = _apply_context_budget(
        displayed_results,
        body.context_budget_chars,
    )
    merge_duration_ms = _duration_ms(merge_started_at)
    return AgentMemoryRetrieveResponse(
        scopes=scopes,
        trace=AgentMemoryRetrieveTrace(
            searched_scopes=scopes,
            caller_agent_scope_key=delegated_decision.caller_agent_scope_key,
            requested_agent_scope_keys=list(delegated_decision.requested_agent_scope_keys),
            requested_agent_scope_patterns=list(pattern_resolution.requested_patterns),
            discovered_agent_scope_keys=list(pattern_resolution.discovered_keys),
            matched_agent_scope_keys=list(pattern_resolution.matched_keys),
            selected_agent_scope_keys=list(pattern_resolution.selected_keys),
            skipped_agent_scope_keys=list(pattern_resolution.skipped_keys),
            agent_scope_pattern_limit=body.agent_scope_pattern_limit,
            agent_scope_pattern_truncated=pattern_resolution.truncated,
            agent_scope_pattern_skip_reasons=list(pattern_resolution.skip_reasons),
            authorized_agent_scope_keys=list(delegated_decision.authorized_agent_scope_keys),
            denied_agent_scope_keys=list(delegated_decision.denied_agent_scope_keys),
            delegated_agent_policy_id=delegated_decision.policy_id,
            delegated_agent_policy_source=delegated_decision.policy_source,
            delegated_agent_decision=delegated_decision.decision,
            delegated_agent_deny_reasons=list(delegated_decision.deny_reasons),
            access_reason_required=delegated_decision.access_reason_required,
            access_reason_present=delegated_decision.access_reason_present,
            result_counts_by_scope=_route_result_counts_by_scope(scopes, route_results),
            workspace_strict=body.workspace_strict,
            workspace_scope_exhausted=workspace_scope_exhausted,
            tenant_shared_policy=body.tenant_shared_policy,
            tenant_shared_fallback_used=tenant_shared_fallback_used,
            broad_corpus_policy=body.broad_corpus_policy,
            broad_corpus_searched=broad_corpus_searched,
            broad_corpus_skipped_reason=broad_corpus_skipped_reason,
            excluded_scope_types=["agent", "workspace", "session"],
            selected_scope_candidate_limit=selected_candidate_limit,
            broad_candidate_limit=broad_candidate_limit,
            display_limit=display_limit,
            context_budget_chars=body.context_budget_chars,
            query_embedding_reused=query_embedding_reused,
            selected_scope_query_count=len(scopes),
            selected_scope_result_count=selected_scope_result_count,
            selected_scope_fallback_used=selected_scope_fallback_used,
            selected_scope_completeness_warnings=selected_scope_warnings,
            broad_result_count=broad_result_count,
            deduped_result_count=len(deduped_results),
            selected_scope_duration_ms=selected_scope_duration_ms,
            broad_corpus_duration_ms=broad_corpus_duration_ms,
            merge_duration_ms=merge_duration_ms,
            total_duration_ms=_duration_ms(started_at),
            budget_truncated=len(deduped_results) > display_limit,
            context_budget_truncated=context_budget_truncated,
            fallback_used=selected_scope_fallback_used,
            completeness_warnings=selected_scope_warnings,
        ),
        results=displayed_results,
        total=len(displayed_results),
    )


def _query_fingerprint(query: str) -> str:
    normalized = " ".join(query.casefold().split())
    return sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _status_from_checks(checks: list[MemoryRetrievalDoctorCheck]) -> str:
    if any(check.status == "unhealthy" for check in checks):
        return "unhealthy"
    if any(check.status == "degraded" for check in checks):
        return "degraded"
    return "ok"


def _queue_check(queue_health: object | None) -> MemoryRetrievalDoctorCheck:
    if queue_health is None:
        return MemoryRetrievalDoctorCheck(
            name="queue_health",
            status="degraded",
            reasons=["worker queue telemetry unavailable"],
        )
    queues = getattr(queue_health, "queues", []) or []
    telemetry_errors = [
        f"{queue.key}: {queue.telemetry_error}"
        for queue in queues
        if getattr(queue, "telemetry_error", None)
    ]
    if telemetry_errors:
        return MemoryRetrievalDoctorCheck(
            name="queue_health",
            status="degraded",
            reasons=telemetry_errors,
        )
    stalled = [
        f"{queue.key} queued_depth={queue.queued_depth}"
        for queue in queues
        if getattr(queue, "queued_depth", 0) > 0
    ]
    return MemoryRetrievalDoctorCheck(
        name="queue_health",
        status="degraded" if stalled else "ok",
        reasons=stalled,
    )


def _wakeup_check(wakeup: MemoryRetrievalDoctorWakeupState) -> MemoryRetrievalDoctorCheck:
    if wakeup.stale:
        return MemoryRetrievalDoctorCheck(
            name="wakeup_briefs",
            status="degraded",
            reasons=[f"{wakeup.stale} stale wake-up brief(s)"],
        )
    return MemoryRetrievalDoctorCheck(name="wakeup_briefs", status="ok")


def _relationship_check(relationships: MemoryRetrievalDoctorRelationshipState) -> MemoryRetrievalDoctorCheck:
    if relationships.deferred_memory_candidates > 0:
        return MemoryRetrievalDoctorCheck(
            name="relationships",
            status="degraded",
            reasons=[f"{relationships.deferred_memory_candidates} memory item(s) await relationship backfill"],
        )
    return MemoryRetrievalDoctorCheck(name="relationships", status="ok")


async def _build_relationship_doctor_state(
    db: AsyncSession,
    *,
    tenant_id: str,
) -> MemoryRetrievalDoctorRelationshipState:
    relationship_edges = await db.scalar(
        text(
            """
            SELECT COUNT(*)
            FROM item_relationships r
            JOIN items s ON s.id = r.source_item_id
            JOIN items t ON t.id = r.target_item_id
            WHERE s.tenant_id = :tenant_id
              AND t.tenant_id = :tenant_id
              AND s.status = 'ready'
              AND t.status = 'ready'
              AND s.deleted_at IS NULL
              AND t.deleted_at IS NULL
            """
        ),
        {"tenant_id": tenant_id},
    )
    orphaned_ready_items = await db.scalar(
        text(
            """
            SELECT COUNT(*)
            FROM items i
            WHERE i.tenant_id = :tenant_id
              AND i.status = 'ready'
              AND i.deleted_at IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM item_relationships r
                  WHERE r.source_item_id = i.id OR r.target_item_id = i.id
              )
            """
        ),
        {"tenant_id": tenant_id},
    )
    deferred_memory_candidates = await db.scalar(
        text(
            """
            SELECT COUNT(*)
            FROM items i
            WHERE i.tenant_id = :tenant_id
              AND i.status = 'ready'
              AND i.deleted_at IS NULL
              AND i.metadata ? 'memory_entry'
              AND NOT EXISTS (
                  SELECT 1 FROM item_relationships r
                  WHERE r.source_item_id = i.id OR r.target_item_id = i.id
              )
            """
        ),
        {"tenant_id": tenant_id},
    )
    return MemoryRetrievalDoctorRelationshipState(
        relationship_edges=int(relationship_edges or 0),
        ready_items_without_relationships=int(orphaned_ready_items or 0),
        deferred_memory_candidates=int(deferred_memory_candidates or 0),
    )


def _doctor_selected_scopes(body: MemoryRetrievalDoctorRequest) -> list[MemoryScope]:
    scopes: list[MemoryScope] = []
    if body.agent_scope_key:
        _append_scope_once(scopes, MemoryScope(type="agent", key=body.agent_scope_key))
    for workspace_key in body.workspace_scope_keys:
        _append_scope_once(scopes, MemoryScope(type="workspace", key=workspace_key))
    if body.session_scope_key:
        _append_scope_once(scopes, MemoryScope(type="session", key=body.session_scope_key))
    if body.include_tenant_shared:
        _append_scope_once(scopes, MemoryScope(type="tenant_shared"))
    return scopes


async def build_memory_retrieval_doctor(
    db: AsyncSession,
    *,
    embedder,
    tenant_id: str,
    body: MemoryRetrievalDoctorRequest,
    auth: MemoryRetrievalDoctorAuthShape,
    arq_pool=None,
) -> MemoryRetrievalDoctorResponse:
    state = await db.get(PalaceTenantState, tenant_id)
    backlog_generation = 0
    if state is not None:
        backlog_generation = max(state.dirty_generation - state.indexed_generation, 0)
    generation = MemoryRetrievalDoctorGeneration(
        dirty_generation=state.dirty_generation if state is not None else 0,
        indexed_generation=state.indexed_generation if state is not None else 0,
        backlog_generation=backlog_generation,
    )
    queue_health = await build_worker_backpressure(arq_pool)
    wakeup_summary = await build_wakeup_brief_summary(
        db,
        tenant_id=tenant_id,
        indexed_generation=generation.indexed_generation,
    )
    wakeup = MemoryRetrievalDoctorWakeupState(
        fresh=int(wakeup_summary.get("fresh", 0)),
        stale=int(wakeup_summary.get("stale", 0)),
        generated_for_day=(
            str(wakeup_summary["generated_for_day"])
            if wakeup_summary.get("generated_for_day")
            else None
        ),
        last_refreshed_at=wakeup_summary.get("last_refreshed_at"),
    )
    relationships = await _build_relationship_doctor_state(db, tenant_id=tenant_id)

    checks = [
        _queue_check(queue_health),
        _wakeup_check(wakeup),
        _relationship_check(relationships),
    ]
    if generation.backlog_generation > 0:
        checks.append(
            MemoryRetrievalDoctorCheck(
                name="indexed_generation",
                status="degraded",
                reasons=[f"indexed generation trails dirty generation by {generation.backlog_generation}"],
            )
        )
    else:
        checks.append(MemoryRetrievalDoctorCheck(name="indexed_generation", status="ok"))

    probes: list[MemoryRetrievalDoctorProbeReport] = []
    for index, probe in enumerate(body.sample_probes):
        response = await retrieve_memory(
            db,
            embedder=embedder,
            tenant_id=tenant_id,
            body=MemoryRetrieveRequest(
                query=probe.query,
                limit=probe.limit,
                tags=probe.tags,
                tags_mode=probe.tags_mode,
                scope=probe.scope,
            ),
        )
        expected_ids = set(probe.expected_item_ids)
        expected_top_rank = next(
            (rank for rank, result in enumerate(response.results, start=1) if result.item_id in expected_ids),
            None,
        )
        reasons: list[str] = []
        if response.total == 0:
            reasons.append("probe returned no results")
        if expected_ids and expected_top_rank is None:
            reasons.append("expected item was not returned")
        if response.trace.fallback_used:
            reasons.append("fallback was used")
        if response.trace.completeness_warning:
            reasons.append(response.trace.completeness_warning)

        ranking_routes = [
            MemoryRetrievalDoctorRankingRoute(
                route=trace.route,
                candidate_limit=trace.candidate_limit,
                candidate_count=trace.candidate_count,
                result_count=trace.result_count,
                source_ranking_enabled=trace.source_ranking_enabled,
                fallback_used=bool(trace.routing.get("fallback_used")) if trace.routing else None,
                global_merge_rescued_results=bool(trace.routing.get("global_merge_rescued_results")) if trace.routing else None,
            )
            for trace in response.trace.ranking_traces
        ]
        probes.append(
            MemoryRetrievalDoctorProbeReport(
                probe_index=index,
                query_fingerprint=_query_fingerprint(probe.query),
                scope=probe.scope,
                tags=probe.tags,
                status=(
                    "unhealthy"
                    if response.total == 0 or (expected_ids and expected_top_rank is None)
                    else "degraded" if response.trace.fallback_used else "ok"
                ),
                reasons=reasons,
                route_confidence=response.trace.route_confidence,
                route_score=response.trace.route_score,
                route_candidate_count=response.trace.route_candidate_count,
                route_room_candidate_count=response.trace.route_room_candidate_count,
                route_global_candidate_count=response.trace.route_global_candidate_count,
                fallback_used=response.trace.fallback_used,
                global_merge_rescued_results=response.trace.global_merge_rescued_results,
                selected_scope_result_count=response.total,
                deduped_result_count=response.total,
                budget_truncated=response.total > probe.limit,
                ranking_routes=ranking_routes,
                top_results=[
                    MemoryRetrievalDoctorProbeTopResult(
                        rank=rank,
                        item_id=result.item_id,
                        source_type=result.source_type,
                        score=result.score,
                        tags=result.tags,
                        expected_match=result.item_id in expected_ids,
                    )
                    for rank, result in enumerate(response.results[: body.display_limit], start=1)
                ],
                expected_top_rank=expected_top_rank,
            )
        )
    checks.extend(
        MemoryRetrievalDoctorCheck(
            name=f"probe_{probe.probe_index}",
            status=probe.status,
            reasons=probe.reasons,
        )
        for probe in probes
    )

    return MemoryRetrievalDoctorResponse(
        status=_status_from_checks(checks),
        tenant_id=tenant_id,
        auth=auth,
        selected_scopes=_doctor_selected_scopes(body),
        generation=generation,
        queue_health=queue_health,
        wakeup_briefs=wakeup,
        relationships=relationships,
        probes=probes,
        checks=checks,
    )


def _metadata_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


async def get_memory_wakeup_brief(
    db: AsyncSession,
    *,
    tenant_id: str,
    scope_type: str = "tenant",
    scope_key: str | None = None,
) -> MemoryWakeupBriefResponse:
    if scope_type not in {"tenant", "wing"}:
        raise HTTPException(status_code=422, detail="scope_type must be tenant or wing")
    if scope_type == "tenant" and scope_key is not None:
        raise HTTPException(status_code=422, detail="scope_key must be omitted for tenant wake-up briefs")
    if scope_type == "wing" and (scope_key is None or not scope_key.strip()):
        raise HTTPException(status_code=422, detail="scope_key is required for wing wake-up briefs")

    normalized_scope_key = scope_key.strip() if isinstance(scope_key, str) else None
    state = await db.get(PalaceTenantState, tenant_id)
    indexed_generation = state.indexed_generation if state is not None else 0
    rows = (
        await db.execute(
            select(Item)
            .where(Item.tenant_id == tenant_id)
            .where(Item.status == "ready")
            .where(Item.deleted_at.is_(None))
            .order_by(Item.updated_at.desc(), Item.id.desc())
        )
    ).scalars().all()

    candidates: list[tuple[str, int, datetime, Item, dict]] = []
    for item in rows:
        brief = (item.metadata_ or {}).get("wakeup_brief")
        if not isinstance(brief, dict):
            continue
        if brief.get("scope_type") != scope_type:
            continue
        if (brief.get("scope_key") if isinstance(brief.get("scope_key"), str) else None) != normalized_scope_key:
            continue
        if not isinstance(brief.get("day"), str):
            continue
        if not item.raw_content:
            continue
        generation = _metadata_int(brief.get("generation"))
        candidates.append((brief["day"], generation, item.updated_at, item, brief))

    if not candidates:
        raise HTTPException(status_code=404, detail="Wake-up brief not found")

    _day, generation, _updated_at, item, brief = max(candidates, key=lambda candidate: candidate[:3])
    stale = generation < indexed_generation
    source_trust = (await get_source_trust_summaries(db, tenant_id=tenant_id, item_ids=[item.id])).get(item.id)
    return MemoryWakeupBriefResponse(
        source_item_id=item.id,
        title=item.title,
        summary=item.summary,
        body=item.raw_content,
        source_url=item.source_url,
        day=brief["day"],
        scope_type=scope_type,
        scope_key=normalized_scope_key,
        generation=generation,
        indexed_generation=indexed_generation,
        freshness="stale" if stale else "fresh",
        stale=stale,
        room_count=_metadata_int(brief.get("room_count")),
        diary_count=_metadata_int(brief.get("diary_count")),
        fact_count=_metadata_int(brief.get("fact_count")),
        updated_at=item.updated_at,
        source_trust=source_trust.__dict__ if source_trust is not None else None,
    )


async def list_memory_jobs(
    db: AsyncSession,
    *,
    tenant_id: str,
    status: str | None = None,
    page: int = 1,
    per_page: int = 20,
) -> MemoryJobListResponse:
    query = (
        select(Job)
        .where(Job.tenant_id == tenant_id)
        .where(Job.job_type == MEMORY_JOB_TYPE)
        .order_by(Job.created_at.desc())
    )
    if status:
        normalized_status = _MEMORY_STATUS_QUERY_MAP.get(status)
        if normalized_status is None:
            raise HTTPException(status_code=422, detail=f"Unsupported memory job status filter: {status}")
        query = query.where(Job.status == normalized_status)

    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar_one()
    jobs = (
        await db.execute(
            query.offset((page - 1) * per_page).limit(per_page)
        )
    ).scalars().all()
    return MemoryJobListResponse(
        jobs=[serialize_memory_job(job) for job in jobs],
        total=total,
    )


async def retry_memory_job(
    db: AsyncSession,
    *,
    tenant_id: str,
    job_id: uuid.UUID,
) -> Job:
    job = await db.get(Job, job_id)
    if not job or job.tenant_id != tenant_id or job.job_type != MEMORY_JOB_TYPE:
        raise HTTPException(status_code=404, detail="Memory job not found")
    if job.status not in ("failed", "cancelled"):
        raise HTTPException(status_code=409, detail=f"Memory job is {job.status}; only failed or cancelled jobs can be retried")

    item = await db.get(Item, job.item_id) if job.item_id else None
    if not item or not item.raw_content:
        raise HTTPException(status_code=409, detail="Memory source note content is unavailable; re-submit the memory entry")

    job.status = "queued"
    job.progress = 0
    job.error_message = None
    job.completed_at = None
    job.duplicate_of = None
    payload = dict(job.payload or {})
    payload.pop("contract_status", None)
    job.payload = payload
    item.status = "processing"
    item.updated_at = datetime.now(timezone.utc)
    await record_job_progress_event(
        db,
        job=job,
        phase="retry",
        status="queued",
        progress=0,
        message="Memory job retry requested",
    )
    await db.commit()
    await db.refresh(job)
    return job
