import json

import pytest

from app.services.relationship_canary_contract import (
    FIXTURE_ID as V1_FIXTURE_ID,
    FIXTURE_SHA256 as V1_FIXTURE_SHA256,
    load_validated_fixture,
)
from app.services.relationship_canary_v2_candidate import (
    CANDIDATE_FIXTURE_ID,
    CANDIDATE_FIXTURE_SHA256,
    CANDIDATE_TENANT_ID,
    RelationshipCanaryV2CandidateError,
    build_v2_candidate_plan,
    load_v2_candidate,
)


def test_v2_candidate_has_new_identity_and_no_live_authorization() -> None:
    fixture = load_v2_candidate()
    plan = build_v2_candidate_plan()

    assert fixture["fixture_id"] == CANDIDATE_FIXTURE_ID
    assert fixture["fixture_id"] != V1_FIXTURE_ID
    assert fixture["artifact_metadata"]["operator_approved"] is False
    assert fixture["artifact_metadata"]["tenant_scope"] == CANDIDATE_TENANT_ID
    assert plan["fixture_sha256"] == CANDIDATE_FIXTURE_SHA256
    assert plan["mode"] == "offline_candidate"
    assert plan["live_executable"] is False
    assert plan["fresh_authorization_required"] is True
    assert plan["mutations_performed"] == 0


def test_v2_candidate_retains_failed_hard_negative_and_prompt_identity() -> None:
    fixture = load_v2_candidate()
    unrelated = next(case for case in fixture["cases"] if case["id"] == "empty_unrelated")

    assert unrelated["records"][0]["title"] == "Canary seasonal palette"
    assert unrelated["records"][1]["title"] == "Canary archival index"
    assert "unrelated to colors" in unrelated["records"][1]["summary"]
    assert fixture["artifact_metadata"]["prompt_version"] == "relationship-classification-v3"
    assert len(fixture["artifact_metadata"]["prompt_sha256"]) == 64
    assert fixture["quality_gate"]["independent_negative_pairs"] == 59
    assert fixture["quality_gate"]["maximum_false_positives"] == 0


def test_v2_candidate_tampering_fails_before_execution(tmp_path) -> None:
    fixture = load_v2_candidate()
    fixture["artifact_metadata"]["operator_approved"] = True
    tampered = tmp_path / "candidate.json"
    tampered.write_text(json.dumps(fixture), encoding="utf-8")

    with pytest.raises(RelationshipCanaryV2CandidateError, match="digest"):
        load_v2_candidate(tampered)


def test_v1_fixture_identity_and_digest_remain_unchanged() -> None:
    fixture = load_validated_fixture()

    assert fixture["fixture_id"] == "sar-1083-relationship-telemetry-canary-v1"
    assert V1_FIXTURE_SHA256 == "b6a716154e495466112ca112714549e0c8d4ae8b7c5556247db79d4148a9b3d7"
