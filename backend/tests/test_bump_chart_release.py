import importlib.util
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "bump_chart_release.py"
SPEC = importlib.util.spec_from_file_location("bump_chart_release", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
bump_chart_release_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bump_chart_release_module)
bump_chart_release = bump_chart_release_module.bump_chart_release


def test_bump_chart_release_updates_chart(tmp_path: Path) -> None:
    chart = tmp_path / "Chart.yaml"
    chart.write_text(
        "\n".join(
            [
                "apiVersion: v2",
                "name: palaceoftruth",
                "version: 0.1.106",
                'appVersion: "8a8f0e5d"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    version = bump_chart_release(chart, "abc12345")

    assert version == "0.1.107"
    assert "version: 0.1.107\n" in chart.read_text(encoding="utf-8")
    assert 'appVersion: "abc12345"\n' in chart.read_text(encoding="utf-8")


def test_bump_chart_release_rejects_non_semantic_chart_version(tmp_path: Path) -> None:
    chart = tmp_path / "Chart.yaml"
    chart.write_text(
        "\n".join(
            [
                "apiVersion: v2",
                "name: palaceoftruth",
                "version: latest",
                'appVersion: "8a8f0e5d"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="semantic chart version"):
        bump_chart_release(chart, "abc12345")


def test_bump_chart_release_rejects_missing_app_version(tmp_path: Path) -> None:
    chart = tmp_path / "Chart.yaml"
    chart.write_text(
        "\n".join(
            [
                "apiVersion: v2",
                "name: palaceoftruth",
                "version: 0.1.106",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="appVersion"):
        bump_chart_release(chart, "abc12345")


def test_bump_chart_release_rejects_missing_chart_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Chart file does not exist"):
        bump_chart_release(tmp_path / "missing.yaml", "abc12345")
