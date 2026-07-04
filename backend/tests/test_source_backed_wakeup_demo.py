from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts" / "demo_source_backed_wakeup.py"
FIXTURE_PATH = ROOT / "fixtures" / "source_backed_wakeup_demo.json"
DOC_PATH = ROOT / "docs" / "source-backed-wakeup-demo.md"
POST_WAKEUP_DESIGN_PATH = ROOT / "docs" / "post-wakeup-claims-promotion-invalidation-design.md"

spec = importlib.util.spec_from_file_location("demo_source_backed_wakeup", SCRIPT_PATH)
assert spec is not None
demo = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(demo)


def test_source_backed_wakeup_fixture_passes_privacy_and_state_scan() -> None:
    payload = demo.load_fixture(FIXTURE_PATH)

    validation = demo.validate_demo_fixture(payload)

    assert validation["entry_count"] == 4
    assert "source_backed" in validation["states"]
    assert "generated_unpromoted" in validation["states"]
    assert "stale_source" in validation["warning_states"]
    assert "policy_limited" in validation["optional_states"]
    assert all(url.startswith("https://example.test/") for url in validation["checked_source_urls"])


def test_source_backed_wakeup_demo_renders_required_blocks() -> None:
    payload = demo.load_fixture(FIXTURE_PATH)

    output = demo.render_demo(payload)

    assert "Context Palace selected" in output
    assert "Trust warnings Palace found" in output
    assert "Safe next action" in output
    assert "source_backed" in output
    assert "generated_unpromoted" in output
    assert "stale_source" in output
    assert "Privacy check: passed" in output


def test_source_backed_wakeup_privacy_scan_rejects_secret_markers() -> None:
    payload = demo.load_fixture(FIXTURE_PATH)
    payload["memory_entries"][0]["summary"] = "api_key should never appear in a fixture"

    try:
        demo.validate_demo_fixture(payload)
    except demo.DemoValidationError as exc:
        assert "forbidden privacy marker" in str(exc)
    else:
        raise AssertionError("expected privacy scan to reject secret-like fixture content")


def test_source_backed_wakeup_docs_page_preserves_public_contract() -> None:
    page = DOC_PATH.read_text(encoding="utf-8")

    for heading in (
        "# Source-Backed Wakeup for Agent Teams",
        "## Quickstart",
        "## Expected Output",
        "## What The Trust States Mean",
        "## Privacy Boundary",
        "## What Palace Does Not Trust Yet",
        "## Operator Next Steps",
        "## Roadmap After The MVP Boundary",
    ):
        assert heading in page

    for state in (
        "source_backed",
        "generated_unpromoted",
        "stale_source",
        "source_missing",
        "policy_limited",
    ):
        assert state in page

    assert "python3 scripts/demo_source_backed_wakeup.py" in page
    assert "get_wakeup_context" in page
    assert "raw chunks" in page
    assert "source previews" in page
    assert "does not claim full source-backed answers" in page
    assert "post-wakeup-claims-promotion-invalidation-design.md" in page


def test_post_wakeup_claims_design_preserves_research_decision_contract() -> None:
    page = POST_WAKEUP_DESIGN_PATH.read_text(encoding="utf-8")

    for heading in (
        "# Post-Wakeup Claims, Promotion, And Invalidation Design",
        "## Recommendation",
        "## Rejected First Claim Types",
        "## Claim Model Boundary",
        "## Promotion States",
        "## Minimum Dependency Graph",
        "## Stale Invalidation Triggers",
        "## Operator UX Surface",
        "## Migration And Backfill Risk",
        "## Test Strategy",
        "## Out Of Scope",
        "## Follow-Up Task Updates",
    ):
        assert heading in page

    assert "Task: SAR-935" in page
    assert "Build the first claim slice around `claim_type='decision'`" in page
    assert "Do not add a parallel" in page
    assert "`task_state`" in page
    assert "`preference`" in page
    assert "`policy`" in page
    assert "`artifact_summary`" in page
    assert "No production data mutation" in page
    assert "No implementation in SAR-935" in page
