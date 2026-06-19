from pathlib import Path

import pytest

from app.evals.palace import (
    evaluate_fixture,
    evaluate_reward_fixture,
    load_eval_baseline,
    load_eval_fixture,
    load_reward_eval_fixture,
    validate_reward_fixture,
)


def test_palace_eval_harness_beats_flat_baseline() -> None:
    fixtures_dir = Path(__file__).parent / "fixtures"
    report = evaluate_fixture(load_eval_fixture(fixtures_dir / "palace_eval_fixture.json"))
    baseline = load_eval_baseline(fixtures_dir / "palace_eval_baseline.json")

    assert report["palace"]["accuracy"] >= baseline["min_palace_accuracy"]
    assert report["routing"]["accuracy"] >= baseline["min_route_accuracy"]
    assert report["delta"] >= baseline["min_accuracy_delta"]


def test_palace_eval_fixture_covers_dogfood_retrieval_surfaces() -> None:
    fixtures_dir = Path(__file__).parent / "fixtures"
    fixture = load_eval_fixture(fixtures_dir / "palace_eval_fixture.json")
    item_ids = {item["id"] for item in fixture["items"]}
    source_types = {item.get("source_type", "note") for item in fixture["items"]}

    assert len(fixture["queries"]) >= 7
    assert {"note", "image"} <= source_types

    for query in fixture["queries"]:
        assert query["expected_item_id"] in item_ids
        assert query.get("expected_wing")
        assert query.get("expected_room")


def test_palace_reward_eval_scores_synthetic_retrieval_answer_trajectories() -> None:
    fixtures_dir = Path(__file__).parent / "fixtures"
    report = evaluate_reward_fixture(
        load_reward_eval_fixture(fixtures_dir / "palace_reward_eval_fixture.json")
    )

    assert report["summary"]["case_count"] == 2
    assert report["summary"]["privacy_safe"] is True
    assert report["summary"]["cached_judge_case_count"] == 1
    assert report["summary"]["component_scores"] == {
        "scope_correctness": 1.0,
        "source_coverage": 1.0,
        "freshness": 0.5,
        "stale_memory_demotion": 0.5,
        "citation_traceability": 1.0,
        "abstention": 1.0,
        "privacy_safe_output": 1.0,
    }
    assert report["summary"]["average_deterministic_reward"] == 0.8572
    assert report["summary"]["average_total_reward"] == 0.8492

    supported_case = report["cases"][0]
    assert supported_case["id"] == "fresh-scoped-answer-with-trace"
    assert supported_case["cached_judge_score"] == 0.92
    assert supported_case["components"]["citation_traceability"]["score"] == 1.0

    abstention_case = report["cases"][1]
    assert abstention_case["id"] == "weak-support-abstention"
    assert abstention_case["components"]["abstention"]["score"] == 1.0
    assert abstention_case["components"]["freshness"]["score"] == 0.0


def test_palace_reward_eval_rejects_raw_or_secretish_fixture_content() -> None:
    payload = {
        "schema_version": 1,
        "cases": [
            {
                "id": "unsafe",
                "query": "unsafe",
                "expected_scope": {"type": "workspace", "key": "palaceoftruth"},
                "required_source_ids": ["safe-source"],
                "candidate": {
                    "scope": {"type": "workspace", "key": "palaceoftruth"},
                    "retrieved_source_ids": ["safe-source"],
                    "cited_source_ids": ["safe-source"],
                    "answer_excerpt": "authorization: Bearer abc123",
                    "abstained": False,
                },
            }
        ],
    }

    with pytest.raises(ValueError, match="secret-like"):
        validate_reward_fixture(payload)

    payload["cases"][0]["candidate"]["answer_excerpt"] = "safe excerpt"
    payload["cases"][0]["candidate"]["raw_body"] = "raw source text"
    with pytest.raises(ValueError, match="raw memory body"):
        validate_reward_fixture(payload)


def test_palace_reward_eval_rejects_uncached_judge_without_source_ids() -> None:
    payload = {
        "schema_version": 1,
        "cases": [
            {
                "id": "bad-judge",
                "query": "bad judge",
                "expected_scope": {"type": "workspace", "key": "palaceoftruth"},
                "required_source_ids": ["safe-source"],
                "candidate": {
                    "scope": {"type": "workspace", "key": "palaceoftruth"},
                    "retrieved_source_ids": ["safe-source"],
                    "cited_source_ids": ["safe-source"],
                    "answer_excerpt": "safe excerpt",
                    "cached_judge": {"score": 1.0, "cache_key": "judge:bad"},
                },
            }
        ],
    }

    with pytest.raises(ValueError, match="cached_judge.source_ids"):
        validate_reward_fixture(payload)
