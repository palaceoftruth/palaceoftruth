from pathlib import Path
from typing import Any

import pytest

from app.services.agent_memory_eval import evaluate_eval_pack, read_eval_pack


FIXTURE = Path(__file__).parent / "fixtures" / "semantic_memory_v1_eval_plan.json"
EVAL_PACK = Path(__file__).parent / "fixtures" / "semantic_memory_v1_eval_pack.json"
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


def test_semantic_memory_v1_eval_pack_covers_every_ship_gate() -> None:
    plan = _load_fixture()
    pack = read_eval_pack(EVAL_PACK)
    cases_by_gate = {
        str(case.get("v1_gate_id")): case
        for case in pack["cases"]
        if case.get("v1_gate_id")
    }

    assert set(cases_by_gate) == {gate["id"] for gate in plan["v1_gates"]}
    assert all(case["results"] or case["v1_gate_id"] == "empty-recall-contract" for case in cases_by_gate.values())
    assert pack["artifact_metadata"]["required_transports"] == [
        "rest",
        "mcp-http",
        "mcp-stdio",
        "hermes",
    ]


def test_semantic_memory_v1_eval_pack_passes_acceptance_gates() -> None:
    report = evaluate_eval_pack(read_eval_pack(EVAL_PACK), top_k=8)

    assert report["summary"]["passed"] is True
    assert report["summary"]["failure_counts"] == {}
    assert report["summary"]["case_count"] == 10
    assert report["summary"]["forbidden_hit_count"] == 0
    assert report["summary"]["forbidden_context_term_count"] == 0
    assert report["summary"]["provenance_label_accuracy"] == 1.0
    assert report["summary"]["source_coverage_accuracy"] == 1.0
    assert report["summary"]["context_budget_fit_rate"] == 1.0
    assert report["summary"]["current_first_failure_cases"] == []
    assert report["summary"]["expected_top_rank_failure_cases"] == []
    assert report["summary"]["target_comparisons"]["semantic_memory_v1_r8"] == {
        "metric": "R@8",
        "target": 1.0,
        "actual": 1.0,
        "passed": True,
    }


def test_semantic_memory_v1_eval_pack_has_typed_provenance_fields() -> None:
    plan = _load_fixture()
    pack = read_eval_pack(EVAL_PACK)
    case = next(case for case in pack["cases"] if case["v1_gate_id"] == "provenance-presence-typing")
    result = case["results"][0]
    required = plan["v1_gates"][1]["required_hit_fields"]

    assert required == {
        "entry_id": "string",
        "scope_type": "string",
        "scope_key": "string",
        "source": "string",
        "created_at": "datetime",
        "valid_from": "datetime|null",
        "score": "number",
    }
    assert isinstance(result["item_id"], str) and result["item_id"]
    assert result["scope"] == {"type": "agent", "key": "iris"}
    assert isinstance(result["source"], str) and result["source"]
    assert result["created_at"].endswith("Z")
    assert result["valid_from"].endswith("Z")
    assert isinstance(result["score"], int | float)


def test_semantic_memory_v1_iris_canary_has_exact_scope_and_sources() -> None:
    plan = _load_fixture()
    pack = read_eval_pack(EVAL_PACK)
    canary = next(case for case in pack["cases"] if case["id"] == "iris-sar-weekly-answer-cites-five-ticket-sources")

    assert canary["query"] == plan["iris_end_to_end"]["query"]
    assert canary["expected_item_ids"] == plan["iris_end_to_end"]["expected_ticket_ids"]
    assert {result["scope"]["key"] for result in canary["results"]} == {"iris"}
    assert all(result["scope"] == plan["iris_end_to_end"]["expected_scope"] for result in canary["results"])
    assert not any(
        forbidden in canary["hermes_context"]
        for forbidden in plan["iris_end_to_end"]["forbidden_scope_keys"]
    )


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


def test_semantic_recall_request_schema_accepts_valid_at_and_budget() -> None:
    from app.schemas.memory import SemanticRecallRequest

    request = SemanticRecallRequest(**_load_fixture()["sample_requests"]["semantic_recall"])
    assert request.scope_type == "agent"
    assert request.scope_key == "iris"
    assert request.valid_at
    assert request.recall_max_tokens == 1500


def test_semantic_recall_rest_surface_exists() -> None:
    from app.api.memory import semantic_recall_memory_artifacts

    assert semantic_recall_memory_artifacts
