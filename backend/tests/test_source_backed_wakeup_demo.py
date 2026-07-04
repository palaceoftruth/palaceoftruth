from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts" / "demo_source_backed_wakeup.py"
FIXTURE_PATH = ROOT / "fixtures" / "source_backed_wakeup_demo.json"

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
