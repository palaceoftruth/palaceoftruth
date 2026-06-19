from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.palace import CandidateCurationArtifact, CandidateCurationArtifactEvent
from app.schemas.curation_artifact import (
    CandidateCurationArtifactCreate,
    CandidateCurationArtifactUpdate,
)


ARTIFACT_STATUSES: frozenset[str] = frozenset(
    {"draft", "proposed", "approved", "rejected", "deprecated", "superseded"}
)
TERMINAL_STATUSES: frozenset[str] = frozenset({"rejected", "deprecated", "superseded"})
DESTRUCTIVE_STATUSES: frozenset[str] = frozenset({"deleted", "purged", "removed"})
APPROVAL_REQUIRED_GATES: tuple[str, ...] = ("approved_by", "approved_at", "decision")
FORBIDDEN_BODY_MARKERS: tuple[str, ...] = (
    "-----begin",
    "api_key=",
    "apikey=",
    "password=",
    "private transcript",
    "raw transcript",
)


class CandidateCurationArtifactError(ValueError):
    """Raised when a candidate artifact would violate the safe curation boundary."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_status(status: str) -> str:
    normalized = status.strip().lower()
    if normalized in DESTRUCTIVE_STATUSES:
        raise CandidateCurationArtifactError("candidate curation artifacts are append-only and cannot be deleted")
    if normalized not in ARTIFACT_STATUSES:
        raise CandidateCurationArtifactError(f"unsupported candidate artifact status: {status}")
    return normalized


def _validate_candidate_body(candidate_body: str) -> None:
    normalized = candidate_body.lower()
    for marker in FORBIDDEN_BODY_MARKERS:
        if marker in normalized:
            raise CandidateCurationArtifactError(
                "candidate_body must be sanitized and must not include raw secrets or private transcript text"
            )


def _validate_privacy_review(privacy_review: dict[str, Any]) -> None:
    if not privacy_review:
        raise CandidateCurationArtifactError("privacy_review is required")
    if privacy_review.get("safe_for_review") is not True:
        raise CandidateCurationArtifactError("privacy_review must confirm safe_for_review is true")
    if privacy_review.get("raw_sensitive_content_excluded") is not True:
        raise CandidateCurationArtifactError(
            "privacy_review must confirm raw_sensitive_content_excluded is true"
        )
    if privacy_review.get("contains_sensitive_content") is not False:
        raise CandidateCurationArtifactError("privacy_review must confirm contains_sensitive_content is false")
    if privacy_review.get("contains_sensitive_content") is True:
        raise CandidateCurationArtifactError("privacy_review reports sensitive content")
    if privacy_review.get("safe_for_review") is False:
        raise CandidateCurationArtifactError("privacy_review reports the candidate is not safe for review")
    if privacy_review.get("raw_sensitive_content_excluded") is False:
        raise CandidateCurationArtifactError("privacy_review must confirm raw sensitive content was excluded")


def _validate_approval(status: str, approval: dict[str, Any]) -> None:
    if status != "approved":
        return
    missing = [key for key in APPROVAL_REQUIRED_GATES if not approval.get(key)]
    if missing:
        raise CandidateCurationArtifactError(
            f"approved candidates require approval fields: {', '.join(missing)}"
        )


def validate_candidate_payload(
    *,
    status: str,
    candidate_body: str,
    privacy_review: dict[str, Any],
    approval: dict[str, Any],
    superseded_by_artifact_id: uuid.UUID | None = None,
) -> str:
    normalized_status = _normalize_status(status)
    _validate_candidate_body(candidate_body)
    _validate_privacy_review(privacy_review)
    _validate_approval(normalized_status, approval)
    if normalized_status == "superseded" and superseded_by_artifact_id is None:
        raise CandidateCurationArtifactError("superseded candidates require superseded_by_artifact_id")
    return normalized_status


def _artifact_snapshot(artifact: CandidateCurationArtifact) -> dict[str, Any]:
    return {
        "id": str(artifact.id),
        "tenant_id": artifact.tenant_id,
        "artifact_kind": artifact.artifact_kind,
        "target_runtime": artifact.target_runtime,
        "target_surface": artifact.target_surface,
        "status": artifact.status,
        "source_item_ids": list(artifact.source_item_ids or []),
        "source_digests": dict(artifact.source_digests or {}),
        "candidate_body": artifact.candidate_body,
        "privacy_review": dict(artifact.privacy_review or {}),
        "eval_summary": dict(artifact.eval_summary or {}),
        "approval": dict(artifact.approval or {}),
        "metadata": dict(artifact.metadata_ or {}),
        "supersedes_artifact_id": str(artifact.supersedes_artifact_id) if artifact.supersedes_artifact_id else None,
        "superseded_by_artifact_id": (
            str(artifact.superseded_by_artifact_id) if artifact.superseded_by_artifact_id else None
        ),
        "deprecated_reason": artifact.deprecated_reason,
        "approved_at": artifact.approved_at.isoformat() if artifact.approved_at else None,
        "deprecated_at": artifact.deprecated_at.isoformat() if artifact.deprecated_at else None,
    }


def _record_artifact_event(
    db: AsyncSession,
    *,
    artifact: CandidateCurationArtifact,
    event_type: str,
    previous_snapshot: dict[str, Any] | None,
) -> None:
    previous_status = previous_snapshot["status"] if previous_snapshot else None
    db.add(
        CandidateCurationArtifactEvent(
            tenant_id=artifact.tenant_id,
            artifact_id=artifact.id,
            event_type=event_type,
            previous_status=previous_status,
            next_status=artifact.status,
            previous_snapshot=previous_snapshot,
            next_snapshot=_artifact_snapshot(artifact),
        )
    )


async def create_candidate_curation_artifact(
    db: AsyncSession,
    *,
    tenant_id: str,
    body: CandidateCurationArtifactCreate,
) -> CandidateCurationArtifact:
    status = validate_candidate_payload(
        status=body.status,
        candidate_body=body.candidate_body,
        privacy_review=body.privacy_review,
        approval=body.approval,
    )
    if body.supersedes_artifact_id is not None:
        await _ensure_lineage_target(db, tenant_id=tenant_id, artifact_id=body.supersedes_artifact_id)
    artifact = CandidateCurationArtifact(
        tenant_id=tenant_id,
        artifact_kind=body.artifact_kind,
        target_runtime=body.target_runtime,
        target_surface=body.target_surface,
        status=status,
        source_item_ids=body.source_item_ids,
        source_digests=body.source_digests,
        candidate_body=body.candidate_body,
        privacy_review=body.privacy_review,
        eval_summary=body.eval_summary,
        approval=body.approval,
        metadata_=body.metadata,
        supersedes_artifact_id=body.supersedes_artifact_id,
    )
    db.add(artifact)
    await db.flush()
    _record_artifact_event(db, artifact=artifact, event_type="created", previous_snapshot=None)
    await db.flush()
    return artifact


async def list_candidate_curation_artifacts(
    db: AsyncSession,
    *,
    tenant_id: str,
    status: str | None = None,
    limit: int = 50,
) -> list[CandidateCurationArtifact]:
    query = select(CandidateCurationArtifact).where(CandidateCurationArtifact.tenant_id == tenant_id)
    if status is not None:
        query = query.where(CandidateCurationArtifact.status == _normalize_status(status))
    rows = (
        await db.execute(
            query.order_by(CandidateCurationArtifact.updated_at.desc()).limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def get_candidate_curation_artifact(
    db: AsyncSession,
    *,
    tenant_id: str,
    artifact_id: uuid.UUID,
) -> CandidateCurationArtifact | None:
    artifact = await db.get(CandidateCurationArtifact, artifact_id)
    if artifact is None or artifact.tenant_id != tenant_id:
        return None
    return artifact


async def _ensure_lineage_target(
    db: AsyncSession,
    *,
    tenant_id: str,
    artifact_id: uuid.UUID,
) -> CandidateCurationArtifact:
    artifact = await db.get(CandidateCurationArtifact, artifact_id)
    if artifact is None or artifact.tenant_id != tenant_id:
        raise CandidateCurationArtifactError("lineage target artifact not found")
    return artifact


async def update_candidate_curation_artifact(
    db: AsyncSession,
    *,
    artifact: CandidateCurationArtifact,
    body: CandidateCurationArtifactUpdate,
) -> CandidateCurationArtifact:
    if artifact.status in TERMINAL_STATUSES and body.status not in (None, artifact.status):
        raise CandidateCurationArtifactError("terminal candidate artifacts cannot move to a new lifecycle status")
    previous_snapshot = _artifact_snapshot(artifact)

    next_status = _normalize_status(body.status) if body.status is not None else artifact.status
    next_privacy_review = body.privacy_review if body.privacy_review is not None else artifact.privacy_review
    next_approval = body.approval if body.approval is not None else artifact.approval
    next_superseded_by = (
        body.superseded_by_artifact_id
        if body.superseded_by_artifact_id is not None
        else artifact.superseded_by_artifact_id
    )
    if body.superseded_by_artifact_id is not None:
        await _ensure_lineage_target(
            db,
            tenant_id=artifact.tenant_id,
            artifact_id=body.superseded_by_artifact_id,
        )
    validate_candidate_payload(
        status=next_status,
        candidate_body=artifact.candidate_body,
        privacy_review=next_privacy_review,
        approval=next_approval,
        superseded_by_artifact_id=next_superseded_by,
    )

    if body.source_item_ids is not None:
        artifact.source_item_ids = body.source_item_ids
    if body.source_digests is not None:
        artifact.source_digests = body.source_digests
    if body.privacy_review is not None:
        artifact.privacy_review = body.privacy_review
    if body.eval_summary is not None:
        artifact.eval_summary = body.eval_summary
    if body.approval is not None:
        artifact.approval = body.approval
    if body.metadata is not None:
        artifact.metadata_ = body.metadata
    if body.superseded_by_artifact_id is not None:
        artifact.superseded_by_artifact_id = body.superseded_by_artifact_id
    if body.deprecated_reason is not None:
        artifact.deprecated_reason = body.deprecated_reason
    if body.status is not None:
        artifact.status = next_status
        if next_status == "approved" and artifact.approved_at is None:
            artifact.approved_at = _utc_now()
        if next_status == "deprecated" and artifact.deprecated_at is None:
            artifact.deprecated_at = _utc_now()
    _record_artifact_event(db, artifact=artifact, event_type="updated", previous_snapshot=previous_snapshot)
    await db.flush()
    return artifact
