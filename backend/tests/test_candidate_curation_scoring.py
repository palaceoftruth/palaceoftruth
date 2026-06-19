import json
from pathlib import Path

from app.services.candidate_curation_scoring import (
    candidate_curation_report_to_json,
    read_candidate_fixture_pack,
    score_candidate_fixture_pack,
)


FIXTURE = Path(__file__).parent / "fixtures" / "candidate_curation_scoring_pack.json"


def test_candidate_curation_scoring_reports_only_ids_scores_and_gate_failures() -> None:
    report = score_candidate_fixture_pack(read_candidate_fixture_pack(FIXTURE))

    assert report["mutating"] is False
    assert report["prints_raw_memory_bodies"] is False
    assert report["candidate_count"] == 3
    assert report["promotion_ready_count"] == 1
    assert report["blocked_promotion_count"] == 2
    assert report["passed"] is True
    assert report["expectation_mismatch_artifact_ids"] == []
    assert report["failure_case_ids"] == [
        "block-sensitive-candidate",
        "mcp-rendering-stays-compatible",
        "stale-guidance-blocks-promotion",
    ]

    rendered = candidate_curation_report_to_json(report)
    assert "candidate_body" not in rendered
    assert "redacted support evidence" not in rendered
    assert "Use the central project-manager helper" not in rendered


def test_candidate_curation_scoring_blocks_secretish_or_private_candidates() -> None:
    payload = {
        "artifacts": [
            {
                "artifact_id": "unsafe",
                "status": "proposed",
                "source_item_ids": ["SAR-513"],
                "source_digests": {"candidate_body": "sha256:unsafe"},
                "candidate_body": "api_key=secret-value",
                "privacy_review": {
                    "safe_for_review": True,
                    "raw_sensitive_content_excluded": True,
                    "contains_sensitive_content": False,
                },
                "eval_summary": {
                    "compatibility": {
                        "passed": True,
                        "transport_results": {
                            "codex": "pass",
                            "hermes": "pass",
                            "rest": "pass",
                            "mcp": "pass",
                        },
                    },
                    "regression_cases": [{"case_id": "secret-blocked", "passed": True}],
                },
            }
        ]
    }

    report = score_candidate_fixture_pack(payload)

    assert report["blocked_promotion_count"] == 1
    assert report["artifacts"][0]["promotion_ready"] is False
    assert report["artifacts"][0]["scores"]["privacy_safety"] == 0.0
    assert "unsafe:privacy_safety" in report["artifacts"][0]["failure_case_ids"]


def test_candidate_curation_scoring_redacts_unsafe_report_ids_and_blocks_private_keys() -> None:
    payload = {
        "artifacts": [
            {
                "artifact_id": "api_key=secret-value",
                "status": "proposed",
                "source_item_ids": ["SAR-513"],
                "source_digests": {"candidate_body": "sha256:unsafe"},
                "candidate_body": "-----BEGIN PRIVATE KEY-----",
                "privacy_review": {
                    "safe_for_review": True,
                    "raw_sensitive_content_excluded": True,
                    "contains_sensitive_content": False,
                },
                "eval_summary": {
                    "compatibility": {
                        "passed": True,
                        "transport_results": {
                            "codex": "pass",
                            "hermes": "pass",
                            "rest": "pass",
                            "mcp": "pass",
                        },
                    },
                    "regression_cases": [{"case_id": "password=leaked-case", "passed": False}],
                },
                "failure_case_ids": ["authorization: Bearer leaked-token"],
            }
        ]
    }

    report = score_candidate_fixture_pack(payload)
    rendered = candidate_curation_report_to_json(report)

    assert report["artifacts"][0]["artifact_id"].startswith("redacted:")
    assert report["artifacts"][0]["promotion_ready"] is False
    assert report["artifacts"][0]["scores"]["privacy_safety"] == 0.0
    assert all(case_id.startswith("redacted:") for case_id in report["failure_case_ids"])
    assert "secret-value" not in rendered
    assert "leaked-token" not in rendered
    assert "PRIVATE KEY" not in rendered


def test_candidate_curation_scoring_redacts_fallback_failure_ids() -> None:
    payload = {
        "artifacts": [
            {
                "artifact_id": "api_key=secret-value",
                "status": "proposed",
                "source_item_ids": ["SAR-513"],
                "source_digests": {"candidate_body": "sha256:unsafe"},
                "candidate_body": "api_key=secret-value",
                "privacy_review": {
                    "safe_for_review": True,
                    "raw_sensitive_content_excluded": True,
                    "contains_sensitive_content": False,
                },
                "eval_summary": {
                    "compatibility": {
                        "passed": True,
                        "transport_results": {
                            "codex": "pass",
                            "hermes": "pass",
                            "rest": "pass",
                            "mcp": "pass",
                        },
                    },
                    "regression_cases": [{"case_id": "secret-blocked", "passed": True}],
                },
            }
        ]
    }

    report = score_candidate_fixture_pack(payload)
    rendered = candidate_curation_report_to_json(report)

    assert report["artifacts"][0]["artifact_id"].startswith("redacted:")
    assert report["artifacts"][0]["failure_case_ids"][0].startswith("redacted:")
    assert "secret-value" not in rendered


def test_candidate_curation_scoring_blocks_non_pem_private_key_candidates() -> None:
    payload = {
        "artifacts": [
            {
                "artifact_id": "unsafe-private-key",
                "status": "proposed",
                "source_item_ids": ["SAR-513"],
                "source_digests": {"candidate_body": "sha256:unsafe"},
                "candidate_body": "private-key material should never be promoted",
                "privacy_review": {
                    "safe_for_review": True,
                    "raw_sensitive_content_excluded": True,
                    "contains_sensitive_content": False,
                },
                "eval_summary": {
                    "compatibility": {
                        "passed": True,
                        "transport_results": {
                            "codex": "pass",
                            "hermes": "pass",
                            "rest": "pass",
                            "mcp": "pass",
                        },
                    },
                    "regression_cases": [{"case_id": "private-key-blocked", "passed": True}],
                },
            }
        ]
    }

    report = score_candidate_fixture_pack(payload)

    assert report["artifacts"][0]["promotion_ready"] is False
    assert report["artifacts"][0]["scores"]["privacy_safety"] == 0.0


def test_candidate_curation_scoring_marks_expectation_mismatches() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["artifacts"][1]["expected_promotion_ready"] = True

    report = score_candidate_fixture_pack(payload)

    assert report["passed"] is False
    assert report["expectation_mismatch_artifact_ids"] == ["curation-privacy-blocked"]
