"""Pure, zero-dependency contract for the compiled SAR-1083 canary."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

TASK_ID = "SAR-1083"
FIXTURE_ID = "sar-1083-relationship-telemetry-canary-v1"
FIXTURE_SHA256 = "b6a716154e495466112ca112714549e0c8d4ae8b7c5556247db79d4148a9b3d7"
FIXTURE_COMMIT = "a2fc173bbceb24ed881dcd8e77fd1df3b9e47c15"
TARGET_CLUSTER = "k3s-lab"
TARGET_NAMESPACE = "palace-sarvent"
TENANT_ID = "sar-1083-canary"
SCOPE_PAYLOAD = {"type": "workspace", "key": "sar-1083-canary"}
EXPECTED_CASE_IDS = (
    "valid_related",
    "empty_unrelated",
    "malformed_fallback_observation",
)
EXPECTED_ALIASES = (
    "telemetry-vocabulary",
    "metrics-reference",
    "seasonal-palette",
    "archival-index",
    "fallback-vocabulary",
    "fallback-reference",
)
CLASSIFICATION_SAMPLE_COUNTS = {
    "valid_related": 7,
    "empty_unrelated": 7,
    "malformed_fallback_observation": 6,
}
FIXTURE_PATH = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sar_1083_relationship_canary_fixture.json"


class RelationshipCanaryContractError(RuntimeError):
    """The compiled canary contract or live target failed closed."""


def _exact_keys(payload: dict[str, Any], expected: set[str], *, label: str) -> None:
    if set(payload) != expected:
        raise RelationshipCanaryContractError(f"{label} fields do not match the compiled contract")


def load_validated_fixture(path: Path = FIXTURE_PATH) -> dict[str, Any]:
    fixture_bytes = path.read_bytes()
    if hashlib.sha256(fixture_bytes).hexdigest() != FIXTURE_SHA256:
        raise RelationshipCanaryContractError("fixture digest does not match the approved artifact")
    fixture = json.loads(fixture_bytes)
    _exact_keys(fixture, {"schema_version", "fixture_id", "artifact_metadata", "cases"}, label="fixture")
    if fixture["schema_version"] != 1 or fixture["fixture_id"] != FIXTURE_ID:
        raise RelationshipCanaryContractError("fixture identity does not match the approved artifact")
    if fixture["artifact_metadata"] != {
        "source": TASK_ID,
        "tenant_scope": "canary",
        "synthetic": True,
        "operator_approved": True,
        "network_calls": False,
        "raw_content_reported": False,
        "cleanup": "retain",
    }:
        raise RelationshipCanaryContractError("fixture safety metadata does not match the approved artifact")

    cases = fixture["cases"]
    if not isinstance(cases, list) or tuple(case.get("id") for case in cases) != EXPECTED_CASE_IDS:
        raise RelationshipCanaryContractError("fixture cases do not match the approved artifact")
    aliases: list[str] = []
    for case in cases:
        expected_case_keys = (
            {"id", "records", "expected_observations"}
            if case["id"] == "malformed_fallback_observation"
            else {"id", "records", "expected"}
        )
        _exact_keys(case, expected_case_keys, label=f"case {case['id']}")
        records = case["records"]
        if not isinstance(records, list) or len(records) != 2:
            raise RelationshipCanaryContractError("every canary case must contain exactly two records")
        for record in records:
            _exact_keys(record, {"alias", "title", "summary"}, label="record")
            if not all(isinstance(record[field], str) and record[field].strip() for field in record):
                raise RelationshipCanaryContractError("record fields must be non-empty strings")
            if "http" in record["summary"].lower():
                raise RelationshipCanaryContractError("record summary contains a disallowed network reference")
            aliases.append(record["alias"])
    if tuple(aliases) != EXPECTED_ALIASES or len(set(aliases)) != 6:
        raise RelationshipCanaryContractError("fixture aliases do not match the six-record allowlist")
    return fixture


def safety_report() -> dict[str, Any]:
    return {
        "synthetic_only": True,
        "private_data_read": False,
        "bulk_rewrite": False,
        "forced_malformed": False,
        "deletes": 0,
        "cleanup": "retain",
    }


def build_plan() -> dict[str, Any]:
    fixture = load_validated_fixture()
    return {
        "task_id": TASK_ID,
        "mode": "dry_run",
        "fixture_id": FIXTURE_ID,
        "fixture_sha256": FIXTURE_SHA256,
        "fixture_commit": FIXTURE_COMMIT,
        "target": {
            "cluster": TARGET_CLUSTER,
            "namespace": TARGET_NAMESPACE,
            "tenant_id": TENANT_ID,
            "scope": dict(SCOPE_PAYLOAD),
        },
        "aliases": list(EXPECTED_ALIASES),
        "case_ids": [case["id"] for case in fixture["cases"]],
        "record_count": 6,
        "pair_count": 3,
        "classification_sample_count": sum(CLASSIFICATION_SAMPLE_COUNTS.values()),
        "provider_call_ceiling": sum(CLASSIFICATION_SAMPLE_COUNTS.values()) * 2,
        "mutations_performed": 0,
        "would_mutate": True,
        "safety": safety_report(),
    }


def empirical_p95(values: list[float]) -> float:
    if not values or any(not math.isfinite(value) or value < 0 for value in values):
        raise RelationshipCanaryContractError("latency samples must be finite non-negative values")
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def idempotency_key(case_id: str, alias: str) -> str:
    return hashlib.sha256(f"{FIXTURE_ID}:{FIXTURE_SHA256}:{case_id}:{alias}".encode()).hexdigest()
