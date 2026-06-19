from __future__ import annotations

import json
import re
from hashlib import sha256
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class CandidateCurationScoringError(ValueError):
    pass


SECRETISH_PATTERN = re.compile(
    r"(?i)(api[_-]?key|authorization|bearer|secret|token|password)\s*[:=]\s*(?:Bearer\s+)?\S+"
)
PRIVATE_CONTEXT_MARKERS = (
    "-----begin",
    "private key",
    "private-key",
    "private transcript",
    "raw transcript",
    "sensitive user content",
    "production data",
)
REQUIRED_COMPATIBILITY_TRANSPORTS = ("codex", "hermes", "rest", "mcp")
SCORE_KEYS = (
    "evidence_coverage",
    "privacy_safety",
    "freshness",
    "interference",
    "compatibility",
    "regression_cases",
    "promotion_readiness",
)


@dataclass(frozen=True)
class CandidateScore:
    artifact_id: str
    scores: dict[str, float]
    failure_case_ids: tuple[str, ...]
    promotion_ready: bool
    expected_promotion_ready: bool | None


def read_candidate_fixture_pack(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise CandidateCurationScoringError(f"candidate fixture pack does not exist: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CandidateCurationScoringError(f"{path}: invalid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise CandidateCurationScoringError(f"{path}: candidate fixture pack must be a JSON object")
    return payload


def score_candidate_fixture_pack(payload: dict[str, Any]) -> dict[str, Any]:
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise CandidateCurationScoringError("candidate fixture pack requires a non-empty artifacts list")

    scores = [_score_candidate(_ensure_object(row, f"artifacts[{index}]")) for index, row in enumerate(artifacts)]
    failure_case_ids = sorted({case_id for score in scores for case_id in score.failure_case_ids})
    mismatches = [
        score.artifact_id
        for score in scores
        if score.expected_promotion_ready is not None
        and score.expected_promotion_ready != score.promotion_ready
    ]
    report = {
        "schema_version": 1,
        "report_kind": "candidate_curation_scoring",
        "mutating": False,
        "prints_raw_memory_bodies": False,
        "pack_id": str(payload.get("pack_id") or "candidate-curation-fixtures"),
        "candidate_count": len(scores),
        "promotion_ready_count": sum(1 for score in scores if score.promotion_ready),
        "blocked_promotion_count": sum(1 for score in scores if not score.promotion_ready),
        "failure_case_ids": failure_case_ids,
        "expectation_mismatch_artifact_ids": mismatches,
        "passed": not mismatches,
        "artifacts": [
            {
                "artifact_id": score.artifact_id,
                "promotion_ready": score.promotion_ready,
                "scores": score.scores,
                "failure_case_ids": list(score.failure_case_ids),
            }
            for score in scores
        ],
        "gate_counts": {
            key: {
                "passed": sum(1 for score in scores if score.scores[key] == 1.0),
                "failed": sum(1 for score in scores if score.scores[key] == 0.0),
            }
            for key in SCORE_KEYS
        },
    }
    return report


def candidate_curation_report_to_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True)


def _score_candidate(candidate: dict[str, Any]) -> CandidateScore:
    artifact_id = _report_safe_id(_required_text(candidate, "artifact_id"))
    status = str(candidate.get("status") or "draft").strip().lower()
    candidate_body = str(candidate.get("candidate_body") or "")
    privacy_review = _object_or_empty(candidate.get("privacy_review"))
    eval_summary = _object_or_empty(candidate.get("eval_summary"))
    metadata = _object_or_empty(candidate.get("metadata"))

    scores = {
        "evidence_coverage": _score_evidence(candidate),
        "privacy_safety": _score_privacy(candidate_body, privacy_review),
        "freshness": _score_freshness(status, metadata),
        "interference": _score_interference(eval_summary, metadata),
        "compatibility": _score_compatibility(eval_summary),
        "regression_cases": _score_regression_cases(eval_summary),
    }
    failing_gates = tuple(key for key, value in scores.items() if value < 1.0)
    failure_case_ids = tuple(_failure_case_ids(artifact_id, candidate, eval_summary, failing_gates))
    promotion_ready = not failing_gates and status in {"draft", "proposed", "approved"}
    scores["promotion_readiness"] = 1.0 if promotion_ready else 0.0
    expected = candidate.get("expected_promotion_ready")
    if expected is not None and not isinstance(expected, bool):
        raise CandidateCurationScoringError(f"{artifact_id}: expected_promotion_ready must be boolean")
    return CandidateScore(
        artifact_id=artifact_id,
        scores=scores,
        failure_case_ids=failure_case_ids,
        promotion_ready=promotion_ready,
        expected_promotion_ready=expected,
    )


def _score_evidence(candidate: dict[str, Any]) -> float:
    source_item_ids = candidate.get("source_item_ids")
    source_digests = candidate.get("source_digests")
    return 1.0 if _non_empty_string_list(source_item_ids) and _non_empty_string_dict(source_digests) else 0.0


def _score_privacy(candidate_body: str, privacy_review: dict[str, Any]) -> float:
    normalized = candidate_body.lower()
    markers_present = any(marker in normalized for marker in PRIVATE_CONTEXT_MARKERS)
    if SECRETISH_PATTERN.search(candidate_body) or markers_present:
        return 0.0
    required_flags = (
        privacy_review.get("safe_for_review") is True,
        privacy_review.get("raw_sensitive_content_excluded") is True,
        privacy_review.get("contains_sensitive_content") is False,
    )
    return 1.0 if all(required_flags) else 0.0


def _score_freshness(status: str, metadata: dict[str, Any]) -> float:
    stale_flags = (
        status in {"deprecated", "superseded", "rejected"},
        metadata.get("superseded_by_newer_guidance") is True,
        metadata.get("source_evidence_stale") is True,
    )
    return 0.0 if any(stale_flags) else 1.0


def _score_interference(eval_summary: dict[str, Any], metadata: dict[str, Any]) -> float:
    interference = _object_or_empty(eval_summary.get("interference"))
    failed = (
        interference.get("overrides_newer_guidance") is True
        or interference.get("overrides_more_specific_guidance") is True
        or metadata.get("interferes_with_runtime_guidance") is True
    )
    return 0.0 if failed else 1.0


def _score_compatibility(eval_summary: dict[str, Any]) -> float:
    compatibility = _object_or_empty(eval_summary.get("compatibility"))
    if compatibility.get("passed") is False:
        return 0.0
    failed_transports = compatibility.get("failed_transports")
    if isinstance(failed_transports, list) and failed_transports:
        return 0.0
    transport_results = _object_or_empty(compatibility.get("transport_results"))
    if transport_results:
        required = set(REQUIRED_COMPATIBILITY_TRANSPORTS)
        seen = {key for key, value in transport_results.items() if value == "pass" or value is True}
        return 1.0 if required <= seen else 0.0
    return 1.0 if compatibility.get("passed") is True else 0.0


def _score_regression_cases(eval_summary: dict[str, Any]) -> float:
    regression_cases = eval_summary.get("regression_cases")
    if not isinstance(regression_cases, list) or not regression_cases:
        return 0.0
    for row in regression_cases:
        case = _ensure_object(row, "regression_cases[]")
        if case.get("passed") is not True:
            return 0.0
    return 1.0


def _failure_case_ids(
    artifact_id: str,
    candidate: dict[str, Any],
    eval_summary: dict[str, Any],
    failing_gates: tuple[str, ...],
) -> list[str]:
    explicit = candidate.get("failure_case_ids")
    ids: list[str] = []
    if isinstance(explicit, list):
        ids.extend(_report_safe_id(str(value)) for value in explicit if str(value).strip())
    regression_cases = eval_summary.get("regression_cases")
    if isinstance(regression_cases, list):
        for row in regression_cases:
            if isinstance(row, dict) and row.get("passed") is not True and row.get("case_id"):
                ids.append(_report_safe_id(str(row["case_id"])))
    if not ids:
        ids.extend(f"{artifact_id}:{gate}" for gate in failing_gates)
    return sorted(set(ids))


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CandidateCurationScoringError(f"candidate artifact requires {key}")
    return value.strip()


def _report_safe_id(value: str) -> str:
    stripped = value.strip()
    if SECRETISH_PATTERN.search(stripped) or any(marker in stripped.lower() for marker in PRIVATE_CONTEXT_MARKERS):
        return f"redacted:{sha256(stripped.encode('utf-8')).hexdigest()[:12]}"
    return stripped


def _ensure_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CandidateCurationScoringError(f"{label} must be an object")
    return value


def _object_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _non_empty_string_list(value: Any) -> bool:
    return isinstance(value, list) and any(isinstance(row, str) and row.strip() for row in value)


def _non_empty_string_dict(value: Any) -> bool:
    return isinstance(value, dict) and any(
        isinstance(key, str)
        and key.strip()
        and isinstance(row, str)
        and row.strip()
        for key, row in value.items()
    )
