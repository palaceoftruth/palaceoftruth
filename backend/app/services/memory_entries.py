from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.schemas.memory import LegacyMemoryArtifactRequest, MemoryEntryRequest, MemoryScope


_BASE_TAGS_BY_KIND = {
    "task_retrospective": ["retrospective", "agent-retrospective"],
    "content_approval": ["content-memory", "content-approved"],
    "founder_note": ["content-memory", "founder-note"],
}


@dataclass(frozen=True)
class NormalizedMemoryEntry:
    tenant_id: str
    title: str
    body: str
    summary: str | None
    source: str
    created_at: datetime
    tags: list[str]
    scope: MemoryScope
    metadata: dict[str, Any]
    idempotency_key: str
    webhook_url: str | None
    enable_ai_enrichment: bool
    relationship_policy: str
    accepted_as: str
    request_fingerprint: str
    source_url: str | None = None


def _dedupe_tags(tags: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for tag in tags:
        if tag not in seen:
            seen.add(tag)
            ordered.append(tag)
    return ordered


def normalize_source_project(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return normalized or None


def source_project_from_memory_metadata(item_metadata: dict[str, Any] | None) -> str | None:
    memory_entry = (item_metadata or {}).get("memory_entry")
    if not isinstance(memory_entry, dict):
        return None
    client_metadata = memory_entry.get("metadata")
    if not isinstance(client_metadata, dict):
        return None
    return normalize_source_project(client_metadata.get("agent_workspace"))


def _scope_tags(scope: MemoryScope) -> list[str]:
    tags = [f"scope-{scope.type}"]
    if scope.key:
        tags.append(f"{scope.type}-{scope.key}")
    return tags


def _canonical_idempotency_key(body: MemoryEntryRequest) -> str:
    if body.idempotency_key:
        return body.idempotency_key
    identity = {
        "tenant_id": body.tenant_id,
        "source": body.source,
        "source_url": body.source_url,
        "scope_type": body.scope.type,
        "scope_key": body.scope.key,
        "created_at": body.created_at.astimezone(timezone.utc).isoformat(),
        "title": body.title,
        "body_sha256": hashlib.sha256(body.body.encode()).hexdigest(),
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _legacy_idempotency_key(body: LegacyMemoryArtifactRequest) -> str:
    identity: dict[str, str | None] = {
        "tenant_id": body.tenant_id,
        "company_id": body.company_id,
        "memory_kind": body.memory_kind,
        "source": body.source,
        "project_id": body.project_id,
        "ticket_id": body.ticket_id,
        "task_id": body.task_id,
        "outcome": body.outcome,
        "review_status": body.review_status,
        "repo_ref": body.repo_ref,
    }
    if not any((body.project_id, body.ticket_id, body.task_id)):
        identity["created_at"] = body.created_at.astimezone(timezone.utc).isoformat()
        identity["title"] = body.title
        identity["body_sha256"] = hashlib.sha256(body.body.encode()).hexdigest()

    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def request_fingerprint(entry: dict[str, Any]) -> str:
    canonical = json.dumps(entry, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def build_legacy_memory_tags(body: LegacyMemoryArtifactRequest) -> list[str]:
    tags = list(_BASE_TAGS_BY_KIND[body.memory_kind])
    tags.extend(body.tags)
    if body.project_id:
        tags.append(f"project-{body.project_id}")
    if body.ticket_id:
        tags.append(f"ticket-{body.ticket_id}")
    if body.task_id:
        tags.append(f"task-{body.task_id}")
    if body.outcome:
        tags.append(f"outcome-{body.outcome}")
    if body.review_status:
        tags.append(f"review-{body.review_status}")
    return _dedupe_tags(tags)


def normalize_memory_entry(body: MemoryEntryRequest) -> NormalizedMemoryEntry:
    idempotency_key = _canonical_idempotency_key(body)
    normalized_tags = _dedupe_tags([*body.tags, *_scope_tags(body.scope)])
    entry_request_fingerprint = request_fingerprint(
        {
            "tenant_id": body.tenant_id,
            "title": body.title,
            "body_sha256": hashlib.sha256(body.body.encode()).hexdigest(),
            "summary": body.summary,
            "source": body.source,
            "created_at": body.created_at.astimezone(timezone.utc).isoformat(),
            "tags": normalized_tags,
            "scope": body.scope.model_dump(mode="json"),
            "source_url": body.source_url,
            "created_by_role": body.created_by_role,
            "metadata": body.metadata,
            "enable_ai_enrichment": body.enable_ai_enrichment,
            "relationship_policy": body.relationship_policy,
            "accepted_as": "canonical",
        }
    )
    metadata = {
        "memory_entry": {
            "schema_version": 1,
            "source": body.source,
            "source_url": body.source_url,
            "created_at": body.created_at.astimezone(timezone.utc).isoformat(),
            "created_by_role": body.created_by_role,
            "scope": body.scope.model_dump(mode="json"),
            "metadata": body.metadata,
            "idempotency_key": idempotency_key,
        }
    }
    return NormalizedMemoryEntry(
        tenant_id=body.tenant_id,
        title=body.title,
        body=body.body,
        summary=body.summary,
        source=body.source,
        created_at=body.created_at,
        tags=normalized_tags,
        scope=body.scope,
        metadata=metadata,
        idempotency_key=idempotency_key,
        webhook_url=body.webhook_url,
        enable_ai_enrichment=body.enable_ai_enrichment,
        relationship_policy=body.relationship_policy,
        accepted_as="canonical",
        request_fingerprint=entry_request_fingerprint,
        source_url=body.source_url,
    )


def normalize_legacy_memory_artifact(body: LegacyMemoryArtifactRequest) -> NormalizedMemoryEntry:
    idempotency_key = _legacy_idempotency_key(body)
    scope = MemoryScope(
        type="workspace" if body.project_id else "tenant_shared",
        key=body.project_id if body.project_id else None,
    )
    normalized_tags = _dedupe_tags([*build_legacy_memory_tags(body), *_scope_tags(scope)])
    entry_request_fingerprint = request_fingerprint(
        {
            "tenant_id": body.tenant_id,
            "company_id": body.company_id,
            "memory_kind": body.memory_kind,
            "title": body.title,
            "summary": body.summary,
            "body_sha256": hashlib.sha256(body.body.encode()).hexdigest(),
            "tags": normalized_tags,
            "created_by_role": body.created_by_role,
            "source": body.source,
            "created_at": body.created_at.astimezone(timezone.utc).isoformat(),
            "scope": scope.model_dump(mode="json"),
            "project_id": body.project_id,
            "ticket_id": body.ticket_id,
            "task_id": body.task_id,
            "outcome": body.outcome,
            "review_status": body.review_status,
            "repo_ref": body.repo_ref,
            "inputs": body.inputs,
            "outputs": body.outputs,
            "enable_ai_enrichment": body.enable_ai_enrichment,
            "relationship_policy": body.relationship_policy,
            "accepted_as": "legacy_artifact",
        }
    )
    metadata = {
        "memory_entry": {
            "schema_version": 1,
            "source": body.source,
            "created_at": body.created_at.astimezone(timezone.utc).isoformat(),
            "created_by_role": body.created_by_role,
            "scope": scope.model_dump(mode="json"),
            "idempotency_key": idempotency_key,
            "legacy_kind": body.memory_kind,
        },
        "memory_contract": {
            "tenant_id": body.tenant_id,
            "company_id": body.company_id,
            "memory_kind": body.memory_kind,
            "created_by_role": body.created_by_role,
            "source": body.source,
            "created_at": body.created_at.astimezone(timezone.utc).isoformat(),
            "project_id": body.project_id,
            "ticket_id": body.ticket_id,
            "task_id": body.task_id,
            "outcome": body.outcome,
            "review_status": body.review_status,
            "repo_ref": body.repo_ref,
            "inputs": body.inputs,
            "outputs": body.outputs,
            "idempotency_key": idempotency_key,
        },
    }
    return NormalizedMemoryEntry(
        tenant_id=body.tenant_id,
        title=body.title,
        body=body.body,
        summary=body.summary,
        source=body.source,
        created_at=body.created_at,
        tags=normalized_tags,
        scope=scope,
        metadata=metadata,
        idempotency_key=idempotency_key,
        webhook_url=body.webhook_url,
        enable_ai_enrichment=body.enable_ai_enrichment,
        relationship_policy=body.relationship_policy,
        accepted_as="legacy_artifact",
        request_fingerprint=entry_request_fingerprint,
    )
