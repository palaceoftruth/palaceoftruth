"""Pure offline evaluator for SAR-1245 relationship-classification precision."""

from __future__ import annotations

import json
import hashlib
import math
from collections import Counter
from pathlib import Path
from typing import Any


class RelationshipPrecisionEvalError(ValueError):
    pass


def one_sided_binomial_lower_bound(
    successes: int,
    total: int,
    *,
    confidence: float = 0.95,
) -> float:
    """Return the exact Clopper-Pearson one-sided lower bound."""

    _validate_binomial_inputs(successes, total, confidence)
    if successes == 0:
        return 0.0
    alpha = 1.0 - confidence
    low = 0.0
    high = 1.0
    for _ in range(100):
        midpoint = (low + high) / 2
        tail = sum(
            math.comb(total, value)
            * midpoint**value
            * (1 - midpoint) ** (total - value)
            for value in range(successes, total + 1)
        )
        if tail < alpha:
            low = midpoint
        else:
            high = midpoint
    return (low + high) / 2


def zero_event_upper_bound(total: int, *, confidence: float = 0.95) -> float:
    """Return the exact one-sided upper event-rate bound for zero events."""

    _validate_binomial_inputs(0, total, confidence)
    return 1 - (1 - confidence) ** (1 / total)


def load_relationship_eval_fixture(
    path: Path,
    *,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except OSError as exc:
        raise RelationshipPrecisionEvalError(f"cannot read relationship eval fixture: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RelationshipPrecisionEvalError(
            f"relationship eval fixture must be JSON: {exc.msg}"
        ) from exc
    fixture_sha256 = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None and fixture_sha256 != expected_sha256:
        raise RelationshipPrecisionEvalError(
            f"relationship eval fixture digest mismatch: {fixture_sha256}"
        )
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RelationshipPrecisionEvalError("relationship eval fixture schema_version must be 1")
    fixture_id = payload.get("fixture_id")
    if not isinstance(fixture_id, str) or not fixture_id.strip():
        raise RelationshipPrecisionEvalError("relationship eval fixture_id is required")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise RelationshipPrecisionEvalError("relationship eval fixture must include cases")
    seen: set[str] = set()
    for case in cases:
        if not isinstance(case, dict):
            raise RelationshipPrecisionEvalError("relationship eval cases must be objects")
        required = {
            "id",
            "stratum",
            "expected_relationship",
            "title_a",
            "summary_a",
            "title_b",
            "summary_b",
        }
        if set(case) != required:
            raise RelationshipPrecisionEvalError(
                f"relationship eval case fields do not match the contract: {case.get('id')}"
            )
        case_id = case["id"]
        if not isinstance(case_id, str) or not case_id or case_id in seen:
            raise RelationshipPrecisionEvalError("relationship eval case IDs must be unique")
        seen.add(case_id)
        if case["expected_relationship"] not in {
            "related_to",
            "expands_on",
            "contradicts",
            "prerequisite_of",
            "example_of",
            "none",
        }:
            raise RelationshipPrecisionEvalError(f"unsupported expected relationship: {case_id}")
        if not all(
            isinstance(case[field], str) and case[field].strip()
            for field in required - {"id", "expected_relationship"}
        ):
            raise RelationshipPrecisionEvalError(f"relationship eval case text is invalid: {case_id}")
    payload["fixture_sha256"] = fixture_sha256
    return payload


def load_relationship_eval_outputs(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RelationshipPrecisionEvalError(f"cannot read relationship eval outputs: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RelationshipPrecisionEvalError(
            f"relationship eval outputs must be JSON: {exc.msg}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RelationshipPrecisionEvalError("relationship eval outputs schema_version must be 1")
    outputs = payload.get("outputs")
    if not isinstance(outputs, list):
        raise RelationshipPrecisionEvalError("relationship eval outputs must include outputs")
    return payload


def evaluate_relationship_precision(
    fixture: dict[str, Any],
    outputs_payload: dict[str, Any],
    *,
    extraction_threshold: float,
) -> dict[str, Any]:
    """Score semantic quality separately from persistence eligibility."""

    if not 0 <= extraction_threshold <= 1:
        raise RelationshipPrecisionEvalError("extraction_threshold must be between 0 and 1")
    cases = fixture["cases"]
    outputs = outputs_payload["outputs"]
    if outputs_payload.get("fixture_id") != fixture["fixture_id"]:
        raise RelationshipPrecisionEvalError("output fixture_id does not match fixture")
    if outputs_payload.get("fixture_sha256") != fixture.get("fixture_sha256"):
        raise RelationshipPrecisionEvalError("output fixture_sha256 does not match fixture")
    output_by_case: dict[str, dict[str, Any]] = {}
    for output in outputs:
        if not isinstance(output, dict) or not isinstance(output.get("case_id"), str):
            raise RelationshipPrecisionEvalError("every output must include case_id")
        if output["case_id"] in output_by_case:
            raise RelationshipPrecisionEvalError(f"duplicate output: {output['case_id']}")
        output_by_case[output["case_id"]] = output

    counts: Counter[str] = Counter()
    strata: Counter[tuple[Any, ...]] = Counter()
    failures: list[str] = []
    case_reports: list[dict[str, Any]] = []
    known_case_total = 0
    known_case_passed = 0
    for case in cases:
        output = output_by_case.get(case["id"])
        if output is None:
            failures.append(f"missing_output:{case['id']}")
            counts["schema_failures"] += 1
            continue
        relationship = output.get("relationship")
        confidence = output.get("confidence")
        schema_valid = (
            output.get("schema_valid") is True
            and relationship
            in {
                "related_to",
                "expands_on",
                "contradicts",
                "prerequisite_of",
                "example_of",
                "none",
            }
            and isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
            and 0 <= float(confidence) <= 1
        )
        if not schema_valid:
            counts["schema_failures"] += 1
            failures.append(f"schema_invalid:{case['id']}")
            continue

        expected_positive = case["expected_relationship"] != "none"
        predicted_positive = relationship != "none"
        known_failed_pair = case["stratum"] == "known_failed_pair"
        if not known_failed_pair:
            if expected_positive and predicted_positive:
                counts["tp"] += 1
            elif expected_positive:
                counts["fn"] += 1
            elif predicted_positive:
                counts["fp"] += 1
            else:
                counts["tn"] += 1
            if expected_positive:
                counts["positive_total"] += 1
                if relationship == case["expected_relationship"]:
                    counts["directional_correct"] += 1
            else:
                counts["negative_total"] += 1
        if predicted_positive and float(confidence) >= extraction_threshold:
            counts["persistence_eligible_predictions"] += 1
            if not expected_positive and not known_failed_pair:
                counts["persistence_eligible_false_positives"] += 1
        if known_failed_pair:
            known_case_total += 1
            if relationship == "none":
                known_case_passed += 1
            else:
                counts["known_failed_pair_false_positives"] += 1

        identity = output.get("identity")
        identity_tuple = _identity_tuple(identity)
        strata[identity_tuple] += 1
        case_reports.append(
            {
                "case_id": case["id"],
                "expected_relationship": case["expected_relationship"],
                "relationship": relationship,
                "confidence": float(confidence),
                "semantic_correct": relationship == case["expected_relationship"],
                "persistence_eligible": predicted_positive
                and float(confidence) >= extraction_threshold,
                "identity": identity,
            }
        )

    total_outputs = len(cases)
    schema_valid_count = total_outputs - counts["schema_failures"]
    negative_total = counts["negative_total"]
    positive_total = counts["positive_total"]
    recall = counts["tp"] / positive_total if positive_total else 0.0
    precision_denominator = counts["tp"] + counts["fp"]
    precision = counts["tp"] / precision_denominator if precision_denominator else 0.0
    fpr = counts["fp"] / negative_total if negative_total else 1.0
    directional_accuracy = (
        counts["directional_correct"] / positive_total if positive_total else 0.0
    )
    fpr_upper = (
        zero_event_upper_bound(negative_total)
        if negative_total and counts["fp"] == 0
        else 1.0
    )
    recall_lower = (
        one_sided_binomial_lower_bound(counts["tp"], positive_total)
        if positive_total
        else 0.0
    )
    identity_complete = len(strata) == 1 and next(iter(strata), ("unknown",))[0] is True

    gates = {
        "negative_sample_floor": negative_total >= 59,
        "zero_semantic_false_positives": counts["fp"] == 0,
        "fpr_upper_below_five_percent": fpr_upper < 0.05,
        "positive_sample_floor": positive_total >= 35,
        "recall_lower_at_least_ninety_percent": recall_lower >= 0.9,
        "directional_accuracy_at_least_eighty_percent": directional_accuracy >= 0.8,
        "schema_validity": schema_valid_count == total_outputs,
        "known_failed_pair_stability": known_case_total == 7 and known_case_passed == 7,
        "single_complete_inference_identity": identity_complete,
        "output_completeness": len(output_by_case) == len(cases),
    }
    for gate, passed in gates.items():
        if not passed:
            failures.append(gate)

    return {
        "schema_version": 1,
        "fixture_id": fixture["fixture_id"],
        "output_set_id": outputs_payload.get("output_set_id"),
        "passed": all(gates.values()),
        "gates": gates,
        "failures": sorted(set(failures)),
        "threshold": extraction_threshold,
        "confusion_matrix": {
            "tp": counts["tp"],
            "fp": counts["fp"],
            "tn": counts["tn"],
            "fn": counts["fn"],
        },
        "metrics": {
            "precision": precision,
            "recall": recall,
            "specificity": 1 - fpr,
            "false_positive_rate": fpr,
            "false_positive_rate_upper_one_sided_95": fpr_upper,
            "recall_lower_one_sided_95": recall_lower,
            "directional_accuracy": directional_accuracy,
            "schema_validity": schema_valid_count / total_outputs,
            "known_failed_pair_passed": known_case_passed,
            "known_failed_pair_total": known_case_total,
            "known_failed_pair_false_positives": counts[
                "known_failed_pair_false_positives"
            ],
            "persistence_eligible_predictions": counts["persistence_eligible_predictions"],
            "persistence_eligible_false_positives": counts[
                "persistence_eligible_false_positives"
            ],
        },
        "identity_strata": [
            {"identity": dict(identity[1]), "complete": identity[0], "count": count}
            for identity, count in sorted(strata.items(), key=lambda entry: repr(entry[0]))
        ],
        "cases": case_reports,
    }


def _identity_tuple(identity: Any) -> tuple[bool, tuple[tuple[str, Any], ...]]:
    required = {
        "provider",
        "requested_model",
        "model",
        "prompt_version",
        "prompt_sha256",
        "classifier_schema_version",
        "temperature",
        "seed",
    }
    if not isinstance(identity, dict):
        return False, (("status", "missing"),)
    complete = (
        set(identity) == required
        and all(
            identity[field] is not None and identity[field] != "unknown"
            for field in required - {"seed"}
        )
        and (identity["seed"] is None or isinstance(identity["seed"], int))
    )
    return complete, tuple(sorted(identity.items()))


def _validate_binomial_inputs(successes: int, total: int, confidence: float) -> None:
    if total <= 0 or successes < 0 or successes > total:
        raise RelationshipPrecisionEvalError("invalid binomial sample")
    if not 0 < confidence < 1:
        raise RelationshipPrecisionEvalError("confidence must be between 0 and 1")
