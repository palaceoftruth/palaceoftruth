import importlib.util
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "semantic_memory_v15_load_report.py"
SPEC = importlib.util.spec_from_file_location("semantic_memory_v15_load_report", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
load_report = importlib.util.module_from_spec(SPEC)
sys.modules["semantic_memory_v15_load_report"] = load_report
SPEC.loader.exec_module(load_report)


def test_build_report_uses_requested_scope_size_and_targets() -> None:
    report = load_report.build_report(entry_count=100, samples=3)

    assert report["fixture"]["entry_count"] == 100
    assert report["fixture"]["scope"] == "agent/iris"
    assert report["targets"] == {
        "recall_p95_seconds": 2.0,
        "retain_p95_seconds": 4.0,
    }
    assert report["results"]["recall"]["sample_count"] == 3
    assert report["results"]["retain_extraction"]["sample_count"] == 3
    assert report["passed"] is True


def test_render_markdown_includes_p95_and_regeneration_command() -> None:
    report = load_report.build_report(entry_count=10, samples=2)
    rendered = load_report.render_markdown(report)

    assert "Recall p95" in rendered
    assert "Retain extraction p95" in rendered
    assert "semantic_memory_v15_load_report.py" in rendered
