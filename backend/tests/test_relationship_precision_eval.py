from pathlib import Path

import pytest

from app.services.relationship_precision_eval import (
    RelationshipPrecisionEvalError,
    evaluate_relationship_precision,
    load_relationship_eval_fixture,
    load_relationship_eval_outputs,
    one_sided_binomial_lower_bound,
    zero_event_upper_bound,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "relationship_precision"
HOLDOUT_PATH = FIXTURE_DIR / "sar_1245_locked_holdout_v2.json"
HOLDOUT_SHA256 = "4a942eda5fe29dd0301536454c88e7aa28df695e4af485a4075e70020a8e38b5"
CALIBRATION_PATH = FIXTURE_DIR / "sar_1245_locked_holdout_v1.json"
REPLAY_PATH = FIXTURE_DIR / "sar_1245_failed_canary_replay_v1.json"
IDENTITY = {
    "provider": "openrouter",
    "requested_model": "openai/gpt-4.1",
    "model": "openai/gpt-4.1",
    "prompt_version": "relationship-classification-v3",
    "prompt_sha256": "29a94d7ea504d82fea8af88509ce911555ba4ff1b134a01231eb785b91b5902d",
    "classifier_schema_version": 2,
    "temperature": 0.0,
    "seed": 1083,
}


def _perfect_outputs(fixture: dict) -> dict:
    return {
        "schema_version": 1,
        "output_set_id": "perfect-locked-output",
        "fixture_id": fixture["fixture_id"],
        "fixture_sha256": fixture["fixture_sha256"],
        "outputs": [
            {
                "case_id": case["id"],
                "relationship": case["expected_relationship"],
                "confidence": 0.95,
                "schema_valid": True,
                "identity": dict(IDENTITY),
            }
            for case in fixture["cases"]
        ],
    }


def test_holdout_fixture_has_exact_unique_counts_and_required_strata() -> None:
    fixture = load_relationship_eval_fixture(
        HOLDOUT_PATH,
        expected_sha256=HOLDOUT_SHA256,
    )
    cases = fixture["cases"]

    assert len(cases) == 101
    assert len({case["id"] for case in cases}) == 101
    assert sum(case["stratum"] == "known_failed_pair" for case in cases) == 7
    assert sum(
        case["expected_relationship"] == "none"
        and case["stratum"] != "known_failed_pair"
        for case in cases
    ) == 59
    assert sum(case["expected_relationship"] != "none" for case in cases) == 35
    assert {
        case["expected_relationship"]
        for case in cases
        if case["expected_relationship"] != "none"
    } == {
        "related_to",
        "expands_on",
        "contradicts",
        "prerequisite_of",
        "example_of",
    }


def test_locked_holdout_digest_fails_closed() -> None:
    with pytest.raises(RelationshipPrecisionEvalError, match="digest mismatch"):
        load_relationship_eval_fixture(HOLDOUT_PATH, expected_sha256="0" * 64)


def test_exact_one_sided_sample_bounds_match_locked_gate() -> None:
    assert zero_event_upper_bound(59) == pytest.approx(0.0495076099)
    assert one_sided_binomial_lower_bound(35, 35) == pytest.approx(0.917968149)
    assert one_sided_binomial_lower_bound(34, 35) < 0.9


def test_perfect_locked_outputs_pass_all_semantic_and_identity_gates() -> None:
    fixture = load_relationship_eval_fixture(HOLDOUT_PATH)
    report = evaluate_relationship_precision(
        fixture,
        _perfect_outputs(fixture),
        extraction_threshold=0.7,
    )

    assert report["passed"] is True
    assert all(report["gates"].values())
    assert report["confusion_matrix"] == {"tp": 35, "fp": 0, "tn": 59, "fn": 0}
    assert report["metrics"]["false_positive_rate_upper_one_sided_95"] < 0.05
    assert report["metrics"]["recall_lower_one_sided_95"] >= 0.9
    assert report["metrics"]["known_failed_pair_passed"] == 7
    assert report["identity_strata"] == [
        {"identity": IDENTITY, "complete": True, "count": 101}
    ]


def test_false_positive_and_directional_regression_fail_locked_gate() -> None:
    fixture = load_relationship_eval_fixture(HOLDOUT_PATH)
    outputs = _perfect_outputs(fixture)
    negative = next(
        output for output in outputs["outputs"] if output["case_id"] == "v2-neg-lex-01"
    )
    negative["relationship"] = "related_to"
    directional_cases = [
        output
        for output in outputs["outputs"]
        if output["case_id"].startswith("v2-pos-")
        and output["relationship"] != "related_to"
    ][:8]
    for output in directional_cases:
        output["relationship"] = "related_to"

    report = evaluate_relationship_precision(fixture, outputs, extraction_threshold=0.7)

    assert report["passed"] is False
    assert report["confusion_matrix"]["fp"] == 1
    assert report["gates"]["zero_semantic_false_positives"] is False
    assert report["gates"]["directional_accuracy_at_least_eighty_percent"] is False


def test_mixed_or_missing_actual_identity_fails_locked_gate() -> None:
    fixture = load_relationship_eval_fixture(HOLDOUT_PATH)
    outputs = _perfect_outputs(fixture)
    outputs["outputs"][0]["identity"]["model"] = "unknown"
    outputs["outputs"][1]["identity"]["model"] = "fallback/model"

    report = evaluate_relationship_precision(fixture, outputs, extraction_threshold=0.7)

    assert report["passed"] is False
    assert report["gates"]["single_complete_inference_identity"] is False
    assert len(report["identity_strata"]) == 3


def test_failed_sar1083_replay_cannot_be_hidden_by_extraction_threshold() -> None:
    fixture = load_relationship_eval_fixture(CALIBRATION_PATH)
    replay = load_relationship_eval_outputs(REPLAY_PATH)

    at_old_threshold = evaluate_relationship_precision(
        fixture,
        replay,
        extraction_threshold=0.5,
    )
    at_new_threshold = evaluate_relationship_precision(
        fixture,
        replay,
        extraction_threshold=0.7,
    )

    assert at_old_threshold["passed"] is False
    assert at_new_threshold["passed"] is False
    assert at_old_threshold["metrics"]["known_failed_pair_false_positives"] == 3
    assert at_new_threshold["metrics"]["known_failed_pair_false_positives"] == 3
    assert at_old_threshold["metrics"]["persistence_eligible_predictions"] == 2
    assert at_new_threshold["metrics"]["persistence_eligible_predictions"] == 0


def test_below_threshold_semantic_error_remains_a_false_positive() -> None:
    fixture = load_relationship_eval_fixture(HOLDOUT_PATH)
    outputs = _perfect_outputs(fixture)
    candidate = next(
        output for output in outputs["outputs"] if output["case_id"] == "v2-neg-unrel-01"
    )
    candidate["relationship"] = "related_to"
    candidate["confidence"] = 0.65

    report = evaluate_relationship_precision(fixture, outputs, extraction_threshold=0.7)

    assert report["confusion_matrix"]["fp"] == 1
    assert report["metrics"]["persistence_eligible_false_positives"] == 0
    assert report["passed"] is False
