from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.palace import CandidateCurationArtifact, CandidateCurationArtifactEvent
from app.schemas.curation_artifact import (
    CandidateCurationArtifactCreate,
    ReviewInboxActionRequest,
    ReviewInboxItemOut,
    ReviewInboxResponse,
    ReviewInboxSummaryOut,
    CandidateCurationArtifactUpdate,
    CandidateCurationArtifactOut,
)


ARTIFACT_STATUSES: frozenset[str] = frozenset(
    {
        "draft",
        "needs_source",
        "reviewable",
        "promoted",
        "proposed",
        "approved",
        "rejected",
        "stale",
        "deprecated",
        "superseded",
    }
)
TERMINAL_STATUSES: frozenset[str] = frozenset({"rejected", "stale", "deprecated", "superseded"})
DESTRUCTIVE_STATUSES: frozenset[str] = frozenset({"deleted", "purged", "removed"})
APPROVAL_REQUIRED_GATES: tuple[str, ...] = ("approved_by", "approved_at", "decision")
SOURCE_REQUIRED_STATUSES: frozenset[str] = frozenset({"reviewable", "promoted", "proposed", "approved"})
STRICT_DIGEST_COVERAGE_STATUSES: frozenset[str] = frozenset({"reviewable", "promoted", "approved"})
PROMOTED_STATUSES: frozenset[str] = frozenset({"promoted", "approved"})
REVIEW_INBOX_STATUSES: frozenset[str] = frozenset({"draft", "needs_source", "reviewable", "proposed", "stale"})
REVIEW_INBOX_ACCEPT_STATUSES: frozenset[str] = frozenset({"reviewable", "proposed"})
SAFE_BATCH_REVIEW_ACTIONS: frozenset[str] = frozenset({"pin", "defer"})
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
    if status not in PROMOTED_STATUSES:
        return
    missing = [key for key in APPROVAL_REQUIRED_GATES if not approval.get(key)]
    if missing:
        raise CandidateCurationArtifactError(
            f"promoted candidates require approval fields: {', '.join(missing)}"
        )


def _validate_source_support(
    *,
    status: str,
    source_item_ids: list[str],
    source_digests: dict[str, str],
    metadata: dict[str, Any],
) -> None:
    if status in SOURCE_REQUIRED_STATUSES and (not source_item_ids or not source_digests):
        raise CandidateCurationArtifactError(
            f"{status} generated insights require source_item_ids and source_digests"
        )
    if status in STRICT_DIGEST_COVERAGE_STATUSES:
        missing_digests = [item_id for item_id in source_item_ids if item_id not in source_digests]
        if missing_digests:
            raise CandidateCurationArtifactError(
                "source_digests must include a stable digest for each source_item_id"
            )
    if status in PROMOTED_STATUSES:
        if metadata.get("source_evidence_stale") is True:
            raise CandidateCurationArtifactError("promoted generated insights require fresh source evidence")
        if metadata.get("source_conflicts") is True:
            raise CandidateCurationArtifactError(
                "promoted generated insights require conflict-free source evidence"
            )


def validate_candidate_payload(
    *,
    status: str,
    candidate_body: str,
    privacy_review: dict[str, Any],
    approval: dict[str, Any],
    source_item_ids: list[str] | None = None,
    source_digests: dict[str, str] | None = None,
    metadata: dict[str, Any] | None = None,
    superseded_by_artifact_id: uuid.UUID | None = None,
) -> str:
    normalized_status = _normalize_status(status)
    _validate_candidate_body(candidate_body)
    _validate_privacy_review(privacy_review)
    _validate_approval(normalized_status, approval)
    _validate_source_support(
        status=normalized_status,
        source_item_ids=list(source_item_ids or []),
        source_digests=dict(source_digests or {}),
        metadata=dict(metadata or {}),
    )
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


def _review_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    review = metadata.get("review_inbox")
    return dict(review) if isinstance(review, dict) else {}


def _confidence_from_eval_summary(eval_summary: dict[str, Any]) -> float | None:
    for key in ("confidence", "score", "evidence_coverage"):
        value = eval_summary.get(key)
        if isinstance(value, (int, float)):
            return max(0.0, min(1.0, float(value)))
    compatibility = eval_summary.get("compatibility")
    if isinstance(compatibility, dict) and compatibility.get("passed") is True:
        return 1.0
    return None


def _freshness_for_artifact(artifact: CandidateCurationArtifact) -> str:
    metadata = dict(artifact.metadata_ or {})
    if artifact.status == "needs_source" or not artifact.source_item_ids:
        return "needs_source"
    if metadata.get("source_conflicts") is True:
        return "conflicting"
    if metadata.get("source_evidence_stale") is True or artifact.status == "stale":
        return "stale"
    return "fresh"


def _suggested_action(artifact: CandidateCurationArtifact) -> str:
    freshness = _freshness_for_artifact(artifact)
    if freshness == "needs_source":
        return "open_source"
    if freshness in {"conflicting", "stale"}:
        return "defer"
    if artifact.status in {"reviewable", "proposed"}:
        return "accept"
    return "pin"


def _review_inbox_item(artifact: CandidateCurationArtifact) -> ReviewInboxItemOut:
    metadata = dict(artifact.metadata_ or {})
    review = _review_metadata(metadata)
    source_item_ids = list(artifact.source_item_ids or [])
    return ReviewInboxItemOut(
        artifact=CandidateCurationArtifactOut.model_validate(artifact),
        suggested_action=_suggested_action(artifact),
        confidence=_confidence_from_eval_summary(dict(artifact.eval_summary or {})),
        source_count=len(source_item_ids),
        freshness=_freshness_for_artifact(artifact),  # type: ignore[arg-type]
        affected_scope=f"{artifact.target_runtime}:{artifact.target_surface}",
        pinned=review.get("pinned") is True,
        deferred=review.get("deferred") is True,
        reversible_actions=["pin", "defer"],
    )


def _review_inbox_metadata(
    artifact: CandidateCurationArtifact,
    *,
    action: str,
    actor: str,
    note: str | None,
    defer_until: datetime | None = None,
) -> dict[str, Any]:
    metadata = dict(artifact.metadata_ or {})
    review = _review_metadata(metadata)
    review["last_action"] = action
    review["last_actor"] = actor
    review["last_note"] = note
    review["last_action_at"] = _utc_now().isoformat()
    if action == "pin":
        review["pinned"] = True
    elif action == "defer":
        review["deferred"] = True
        if defer_until is not None:
            review["defer_until"] = defer_until.isoformat()
    elif action in {"accept", "reject"}:
        review["resolved"] = True
    metadata["review_inbox"] = review
    return metadata


async def list_review_inbox(
    db: AsyncSession,
    *,
    tenant_id: str,
    include_deferred: bool = False,
    limit: int = 50,
) -> ReviewInboxResponse:
    query = (
        select(CandidateCurationArtifact)
        .where(CandidateCurationArtifact.tenant_id == tenant_id)
        .where(CandidateCurationArtifact.status.in_(REVIEW_INBOX_STATUSES))
    )
    if not include_deferred:
        deferred_flag = CandidateCurationArtifact.metadata_["review_inbox"]["deferred"].as_boolean()
        query = query.where(or_(deferred_flag.is_(None), deferred_flag.is_not(True)))
    rows = (
        await db.execute(
            query.order_by(CandidateCurationArtifact.updated_at.desc()).limit(limit)
        )
    ).scalars().all()
    items = [_review_inbox_item(row) for row in rows]
    return ReviewInboxResponse(
        items=items,
        summary=ReviewInboxSummaryOut(
            total=len(items),
            needs_source=sum(1 for item in items if item.freshness == "needs_source"),
            conflicting=sum(1 for item in items if item.freshness == "conflicting"),
            stale=sum(1 for item in items if item.freshness == "stale"),
            pinned=sum(1 for item in items if item.pinned),
            deferred=sum(1 for item in items if item.deferred),
        ),
    )


async def apply_review_inbox_action(
    db: AsyncSession,
    *,
    tenant_id: str,
    body: ReviewInboxActionRequest,
) -> list[CandidateCurationArtifact]:
    if len(body.artifact_ids) > 1 and body.action not in SAFE_BATCH_REVIEW_ACTIONS:
        raise CandidateCurationArtifactError("batch review inbox actions are limited to pin and defer")

    artifacts: list[CandidateCurationArtifact] = []
    for artifact_id in body.artifact_ids:
        artifact = await get_candidate_curation_artifact(db, tenant_id=tenant_id, artifact_id=artifact_id)
        if artifact is None:
            raise CandidateCurationArtifactError("review inbox artifact not found")
        if artifact.status not in REVIEW_INBOX_STATUSES:
            raise CandidateCurationArtifactError("artifact is not currently reviewable in the inbox")
        artifacts.append(artifact)

    updated: list[CandidateCurationArtifact] = []
    for artifact in artifacts:
        if body.action == "accept" and artifact.status not in REVIEW_INBOX_ACCEPT_STATUSES:
            raise CandidateCurationArtifactError("only reviewable or proposed inbox artifacts can be accepted")
        metadata = _review_inbox_metadata(
            artifact,
            action=body.action,
            actor=body.actor,
            note=body.note,
            defer_until=body.defer_until,
        )
        update = CandidateCurationArtifactUpdate(metadata=metadata)
        if body.action == "accept":
            update.status = "promoted"
            update.approval = {
                **dict(artifact.approval or {}),
                "approved_by": body.actor,
                "approved_at": _utc_now().isoformat(),
                "decision": "approved",
                "promotion_target": dict(artifact.approval or {}).get("promotion_target")
                or artifact.target_surface,
                "note": body.note,
            }
        elif body.action == "reject":
            update.status = "rejected"
            update.approval = {
                **dict(artifact.approval or {}),
                "approved_by": body.actor,
                "approved_at": _utc_now().isoformat(),
                "decision": "rejected",
                "note": body.note,
            }
        updated.append(await update_candidate_curation_artifact(db, artifact=artifact, body=update))
    return updated


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
        source_item_ids=body.source_item_ids,
        source_digests=body.source_digests,
        metadata=body.metadata,
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
    next_source_item_ids = body.source_item_ids if body.source_item_ids is not None else artifact.source_item_ids
    next_source_digests = body.source_digests if body.source_digests is not None else artifact.source_digests
    next_metadata = body.metadata if body.metadata is not None else artifact.metadata_
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
        source_item_ids=next_source_item_ids,
        source_digests=next_source_digests,
        metadata=next_metadata,
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
        if next_status in PROMOTED_STATUSES and artifact.approved_at is None:
            artifact.approved_at = _utc_now()
        if next_status in {"deprecated", "stale"} and artifact.deprecated_at is None:
            artifact.deprecated_at = _utc_now()
    _record_artifact_event(db, artifact=artifact, event_type="updated", previous_snapshot=previous_snapshot)
    await db.flush()
    return artifact
