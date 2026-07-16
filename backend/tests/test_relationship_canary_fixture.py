import json
from pathlib import Path


FIXTURE = Path(__file__).parent / "fixtures" / "sar_1083_relationship_canary_fixture.json"


def test_relationship_canary_fixture_is_synthetic_and_bounded() -> None:
    fixture = json.loads(FIXTURE.read_text())

    assert fixture["schema_version"] == 1
    assert fixture["fixture_id"] == "sar-1083-relationship-telemetry-canary-v1"
    assert fixture["artifact_metadata"] == {
        "source": "SAR-1083",
        "tenant_scope": "canary",
        "synthetic": True,
        "operator_approved": True,
        "network_calls": False,
        "raw_content_reported": False,
        "cleanup": "retain",
    }
    assert [case["id"] for case in fixture["cases"]] == [
        "valid_related",
        "empty_unrelated",
        "malformed_fallback_observation",
    ]

    for case in fixture["cases"]:
        assert len(case["records"]) == 2
        assert all(record["alias"].startswith(("telemetry-", "metrics-", "seasonal-", "archival-", "fallback-")) for record in case["records"])
        assert all("http" not in record["summary"].lower() for record in case["records"])


def test_fallback_case_never_attempts_to_prompt_a_malformed_response() -> None:
    fixture = json.loads(FIXTURE.read_text())
    fallback_case = fixture["cases"][2]

    assert fallback_case["expected_observations"] == {
        "validation_outcomes": ["malformed", "provider_error", "timeout"],
        "require_retry_or_fallback_attribution_if_observed": True,
        "execution_mode": "observation_only",
    }
