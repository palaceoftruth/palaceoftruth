from pathlib import Path
from typing import Any

import pytest


FIXTURE = Path(__file__).parent / "fixtures" / "semantic_memory_v1_eval_plan.json"
SPEC = Path(__file__).resolve().parents[2] / "docs" / "research" / "sar-1034-semantic-memory-v1-spec.md"


def _load_fixture() -> dict[str, Any]:
    import json

    return json.loads(FIXTURE.read_text())


def test_semantic_memory_v1_fixture_defines_canonical_gate_counts() -> None:
    payload = _load_fixture()

    assert payload["schema_version"] == 1
    assert payload["fixture_id"] == "sar-1034-semantic-memory-v1-eval-plan"
    assert len(payload["v1_gates"]) == 10
    assert len(payload["v1_5_deferrals"]) == 2
    assert len(payload["v2_deferrals"]) == 1
    assert all(gate["ship_gate"] is True for gate in payload["v1_gates"])
    assert {gate["id"] for gate in payload["v1_gates"]} == {
        "strict-scope-isolation",
        "provenance-presence-typing",
        "identity-sovereignty",
        "mission-honored",
        "empty-recall-contract",
        "source-vs-summary",
        "workspace-collision",
        "multi-source-aggregation",
        "budget-containment",
        "stale-current-temporal",
    }


def test_semantic_memory_v1_fixture_locks_temporal_vocabulary() -> None:
    payload = _load_fixture()
    vocabulary = payload["vocabulary"]
    temporal_remember = payload["sample_requests"]["temporal_remember"]
    semantic_recall = payload["sample_requests"]["semantic_recall"]

    assert vocabulary["semantic_api_temporal_end_field"] == "valid_until"
    assert vocabulary["existing_palace_temporal_fact_end_field"] == "valid_to"
    assert vocabulary["recall_time_filter"] == "valid_at"
    assert "valid_until" in temporal_remember
    assert "valid_to" not in temporal_remember
    assert "valid_at" in semantic_recall
    assert temporal_remember["fact_kind"] in vocabulary["fact_kind_values"]


def test_semantic_memory_v1_fixture_names_iris_end_to_end_canary() -> None:
    payload = _load_fixture()
    canary = payload["iris_end_to_end"]

    assert canary["expected_scope"] == {"type": "agent", "key": "iris"}
    assert canary["expected_ticket_ids"] == [
        "SAR-1015",
        "SAR-1016",
        "SAR-1017",
        "SAR-1018",
        "SAR-1019",
    ]
    assert {"vera", "eve", "lux", "barbara"} <= set(canary["forbidden_scope_keys"])


def test_semantic_memory_v1_spec_mentions_fixture_and_non_goals() -> None:
    text = SPEC.read_text()

    assert "10 v1 ship gates" in text
    assert "valid_until" in text
    assert "No schema migration in SAR-1034" in text
    assert "backend/tests/fixtures/semantic_memory_v1_eval_plan.json" in text


def test_semantic_scope_profile_service_exists_for_retain_mission() -> None:
    from app.services.semantic_scope_profiles import SemanticScopeProfileService

    assert SemanticScopeProfileService


@pytest.mark.xfail(strict=True, reason="SAR-1036+ must add first-class temporal semantic memory entries.")
def test_semantic_memory_entry_schema_accepts_valid_until() -> None:
    from app.schemas.memory import SemanticMemoryEntryCreate

    payload = _load_fixture()["sample_requests"]["temporal_remember"]
    entry = SemanticMemoryEntryCreate(**payload)
    assert entry.valid_until is None


@pytest.mark.xfail(strict=True, reason="SAR-1038+ must expose strict semantic recall over REST/MCP.")
def test_semantic_recall_request_schema_accepts_valid_at_and_budget() -> None:
    from app.schemas.memory import SemanticRecallRequest

    request = SemanticRecallRequest(**_load_fixture()["sample_requests"]["semantic_recall"])
    assert request.valid_at
    assert request.recall_max_tokens == 1500
