from __future__ import annotations

import uuid
from typing import Any, Protocol

from app.services.candidate_curation_scoring import (
    CandidateCurationScoringError,
    score_candidate_fixture_pack,
)
from app.services.curation_artifacts import CandidateCurationArtifactError, validate_candidate_payload


class CandidatePromotionArtifact(Protocol):
    id: uuid.UUID
    artifact_kind: str
    target_runtime: str
    target_surface: str
    status: str
    source_item_ids: list[str]
    source_digests: dict[str, str]
    candidate_body: str
    privacy_review: dict[str, Any]
    eval_summary: dict[str, Any]
    approval: dict[str, Any]
    metadata_: dict[str, Any]


PROMOTION_ALLOWED_STATUSES = frozenset({"approved", "promoted"})


def render_candidate_promotion_handoff(artifact: CandidatePromotionArtifact) -> dict[str, Any]:
    """Render a promotion handoff for an approved candidate without applying it."""

    if artifact.status not in PROMOTION_ALLOWED_STATUSES:
        raise CandidateCurationArtifactError("only promoted candidate curation artifacts can render promotion handoffs")

    validate_candidate_payload(
        status=artifact.status,
        candidate_body=artifact.candidate_body,
        privacy_review=dict(artifact.privacy_review or {}),
        approval=dict(artifact.approval or {}),
        source_item_ids=list(artifact.source_item_ids or []),
        source_digests=dict(artifact.source_digests or {}),
        metadata=dict(artifact.metadata_ or {}),
    )
    approval = dict(artifact.approval or {})
    if str(approval.get("decision") or "").strip().lower() != "approved":
        raise CandidateCurationArtifactError("promotion handoff requires approval.decision to be approved")
    promotion_target = str(approval.get("promotion_target") or "").strip()
    if not promotion_target:
        raise CandidateCurationArtifactError("promotion handoff requires approval.promotion_target")

    try:
        scoring_report = score_candidate_fixture_pack({"artifacts": [_artifact_to_scoring_row(artifact)]})
    except CandidateCurationScoringError as exc:
        raise CandidateCurationArtifactError(f"candidate score evidence is invalid: {exc}") from exc
    score = scoring_report["artifacts"][0]
    if not score["promotion_ready"]:
        failures = ", ".join(score["failure_case_ids"]) or "unknown gate failure"
        raise CandidateCurationArtifactError(f"candidate is not promotion-ready: {failures}")

    rollback_notes = _rollback_notes(artifact)
    handoff = _render_markdown_handoff(
        artifact=artifact,
        promotion_target=promotion_target,
        approval=approval,
        score=score,
        rollback_notes=rollback_notes,
    )
    return {
        "artifact_id": artifact.id,
        "target_runtime": artifact.target_runtime,
        "target_surface": artifact.target_surface,
        "promotion_target": promotion_target,
        "source_item_ids": list(artifact.source_item_ids or []),
        "source_digests": dict(artifact.source_digests or {}),
        "approval": approval,
        "gate_evidence": {
            "promotion_ready": score["promotion_ready"],
            "scores": score["scores"],
            "failure_case_ids": score["failure_case_ids"],
            "scoring_report_kind": scoring_report["report_kind"],
            "mutating": False,
        },
        "rollback_or_deprecation_notes": rollback_notes,
        "rendered_handoff": handoff,
    }


def _artifact_to_scoring_row(artifact: CandidatePromotionArtifact) -> dict[str, Any]:
    return {
        "artifact_id": str(artifact.id),
        "artifact_kind": artifact.artifact_kind,
        "target_runtime": artifact.target_runtime,
        "target_surface": artifact.target_surface,
        "status": artifact.status,
        "source_item_ids": list(artifact.source_item_ids or []),
        "source_digests": dict(artifact.source_digests or {}),
        "candidate_body": artifact.candidate_body,
        "privacy_review": dict(artifact.privacy_review or {}),
        "eval_summary": dict(artifact.eval_summary or {}),
        "metadata": dict(artifact.metadata_ or {}),
    }


def _rollback_notes(artifact: CandidatePromotionArtifact) -> list[str]:
    metadata = dict(artifact.metadata_ or {})
    notes = metadata.get("rollback_or_deprecation_notes") or metadata.get("rollback_notes")
    if isinstance(notes, list):
        cleaned = [str(note).strip() for note in notes if str(note).strip()]
        if cleaned:
            return cleaned
    if isinstance(notes, str) and notes.strip():
        return [notes.strip()]
    return [
        "Apply the rendered change only through a reviewed PR or explicit runtime workflow.",
        "If the promoted guidance regresses behavior, revert the PR or mark this candidate deprecated in Palace.",
    ]


def _render_markdown_handoff(
    *,
    artifact: CandidatePromotionArtifact,
    promotion_target: str,
    approval: dict[str, Any],
    score: dict[str, Any],
    rollback_notes: list[str],
) -> str:
    source_ids = "\n".join(f"- {item_id}" for item_id in artifact.source_item_ids)
    source_digests = "\n".join(f"- {key}: {value}" for key, value in artifact.source_digests.items())
    score_lines = "\n".join(
        f"- {key}: {value:.1f}" for key, value in sorted(score["scores"].items())
    )
    rollback_lines = "\n".join(f"- {note}" for note in rollback_notes)
    return f"""# Candidate Promotion Handoff

Artifact: {artifact.id}
Target runtime: {artifact.target_runtime}
Target surface: {artifact.target_surface}
Promotion target: {promotion_target}

## Source Lineage
{source_ids}

## Source Digests
{source_digests}

## Passing Gate Evidence
Approval decision: {approval["decision"]}
Approved by: {approval["approved_by"]}
Approved at: {approval["approved_at"]}
Promotion ready: {score["promotion_ready"]}
{score_lines}

## Candidate Body
{artifact.candidate_body}

## Rollback Or Deprecation Notes
{rollback_lines}

## Required Human/Agent Action
- Open a normal PR or explicit runtime workflow for the target surface.
- Do not apply this candidate automatically from Palace.
- Re-run repo-specific tests, prompt checks, compatibility checks, and privacy review before merging.
"""
