"""Offline-only candidate contract for a freshly authorized SAR-1083 v2 run."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.services.relationship_classification_contract import (
    RELATIONSHIP_PROMPT_SHA256,
    RELATIONSHIP_PROMPT_VERSION,
)
from app.services.relationship_canary_contract import FIXTURE_ID as V1_FIXTURE_ID


CANDIDATE_FIXTURE_ID = "sar-1083-relationship-telemetry-canary-v2-candidate"
CANDIDATE_FIXTURE_SHA256 = "e5d9e0a29a6d20066c6e5131b03a10b652c431bdba819cf00fe701fbf4a890f0"
CANDIDATE_TENANT_ID = "sar-1083-canary-v2"
CANDIDATE_FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "sar_1083_relationship_canary_fixture_v2_candidate.json"
)


class RelationshipCanaryV2CandidateError(ValueError):
    pass


def load_v2_candidate(path: Path = CANDIDATE_FIXTURE_PATH) -> dict[str, Any]:
    fixture_bytes = path.read_bytes()
    if hashlib.sha256(fixture_bytes).hexdigest() != CANDIDATE_FIXTURE_SHA256:
        raise RelationshipCanaryV2CandidateError("candidate fixture digest mismatch")
    payload = json.loads(fixture_bytes)
    metadata = payload.get("artifact_metadata")
    if (
        payload.get("schema_version") != 2
        or payload.get("fixture_id") != CANDIDATE_FIXTURE_ID
        or not isinstance(metadata, dict)
    ):
        raise RelationshipCanaryV2CandidateError("candidate fixture identity mismatch")
    if metadata != {
        "source": "SAR-1245",
        "supersedes_fixture": V1_FIXTURE_ID,
        "tenant_scope": CANDIDATE_TENANT_ID,
        "synthetic": True,
        "operator_approved": False,
        "execution_mode": "offline_candidate",
        "network_calls": False,
        "raw_content_reported": False,
        "cleanup": "retain",
        "prompt_version": RELATIONSHIP_PROMPT_VERSION,
        "prompt_sha256": RELATIONSHIP_PROMPT_SHA256,
    }:
        raise RelationshipCanaryV2CandidateError("candidate safety metadata mismatch")
    if [case.get("id") for case in payload.get("cases", [])] != [
        "valid_related",
        "empty_unrelated",
        "malformed_fallback_observation",
    ]:
        raise RelationshipCanaryV2CandidateError("candidate case identity mismatch")
    aliases = [
        record.get("alias")
        for case in payload["cases"]
        for record in case.get("records", [])
    ]
    if len(aliases) != 6 or len(set(aliases)) != 6:
        raise RelationshipCanaryV2CandidateError("candidate must contain six unique records")
    return payload


def build_v2_candidate_plan() -> dict[str, Any]:
    fixture = load_v2_candidate()
    return {
        "mode": "offline_candidate",
        "fixture_id": CANDIDATE_FIXTURE_ID,
        "fixture_sha256": CANDIDATE_FIXTURE_SHA256,
        "tenant_id": CANDIDATE_TENANT_ID,
        "prompt_version": RELATIONSHIP_PROMPT_VERSION,
        "prompt_sha256": RELATIONSHIP_PROMPT_SHA256,
        "quality_gate": fixture["quality_gate"],
        "record_count": 6,
        "mutations_performed": 0,
        "live_executable": False,
        "operator_approved": False,
        "fresh_authorization_required": True,
    }
