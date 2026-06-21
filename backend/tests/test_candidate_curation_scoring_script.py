import importlib.util
import json
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "report_candidate_curation_scores.py"
PROMOTION_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "render_candidate_promotion_handoff.py"
SPEC = importlib.util.spec_from_file_location("report_candidate_curation_scores", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
report_module = importlib.util.module_from_spec(SPEC)
sys.modules["report_candidate_curation_scores"] = report_module
SPEC.loader.exec_module(report_module)

PROMOTION_SPEC = importlib.util.spec_from_file_location(
    "render_candidate_promotion_handoff",
    PROMOTION_SCRIPT_PATH,
)
assert PROMOTION_SPEC is not None
assert PROMOTION_SPEC.loader is not None
promotion_module = importlib.util.module_from_spec(PROMOTION_SPEC)
sys.modules["render_candidate_promotion_handoff"] = promotion_module
PROMOTION_SPEC.loader.exec_module(promotion_module)


def test_report_candidate_curation_scores_writes_ci_safe_artifact(
    monkeypatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "candidate-report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "report_candidate_curation_scores.py",
            "--output",
            str(output),
        ],
    )

    assert report_module.main() == 0
    report = json.loads(output.read_text(encoding="utf-8"))

    assert report["passed"] is True
    assert report["candidate_count"] == 3
    assert report["blocked_promotion_count"] == 2
    assert "candidate_body" not in output.read_text(encoding="utf-8")


def test_report_candidate_curation_scores_require_all_ready_returns_nonzero(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "report_candidate_curation_scores.py",
            "--require-all-ready",
        ],
    )

    assert report_module.main() == 1


def test_render_candidate_promotion_handoff_writes_dry_run_artifact(
    monkeypatch,
    tmp_path: Path,
) -> None:
    pack = tmp_path / "approved-pack.json"
    output = tmp_path / "promotion-handoff.md"
    pack.write_text(
        json.dumps(
            {
                "artifacts": [
                    {
                        "artifact_id": "approved-codex-candidate",
                        "artifact_kind": "candidate_skill",
                        "target_runtime": "codex",
                        "target_surface": "skills/codex-pm-tasks",
                        "status": "approved",
                        "source_item_ids": ["SAR-514", "PR-340"],
                        "source_digests": {
                            "SAR-514": "sha256:sar-514",
                            "PR-340": "sha256:pr-340",
                        },
                        "candidate_body": "Use the project-manager helper for task writes.",
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
                            "interference": {
                                "overrides_newer_guidance": False,
                                "overrides_more_specific_guidance": False,
                            },
                            "regression_cases": [{"case_id": "no-runtime-mutation", "passed": True}],
                        },
                        "approval": {
                            "approved_by": "codex-review",
                            "approved_at": "2026-05-21T19:30:00Z",
                            "decision": "approved",
                            "promotion_target": "codex skill PR",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "render_candidate_promotion_handoff.py",
            "--pack",
            str(pack),
            "--artifact-id",
            "approved-codex-candidate",
            "--output",
            str(output),
        ],
    )

    assert promotion_module.main() == 0
    rendered = output.read_text(encoding="utf-8")
    assert "Candidate Promotion Handoff" in rendered
    assert "Promotion target: codex skill PR" in rendered
    assert "Approval decision: approved" in rendered
    assert "Do not apply this candidate automatically from Palace." in rendered


def test_render_candidate_promotion_handoff_blocks_unapproved_candidate(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "render_candidate_promotion_handoff.py",
            "--artifact-id",
            "curation-ready-codex-routing",
        ],
    )

    assert promotion_module.main() == 2
