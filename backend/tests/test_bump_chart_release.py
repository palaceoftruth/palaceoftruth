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


def test_bump_chart_release_exceeds_open_and_published_versions(tmp_path: Path) -> None:
    chart = tmp_path / "Chart.yaml"
    chart.write_text(
        "\n".join(
            [
                "apiVersion: v2",
                "name: palaceoftruth",
                "version: 0.1.476",
                'appVersion: "8a8f0e5d"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    version = bump_chart_release(
        chart,
        "abc12345",
        reserved_versions={"0.1.477", "0.1.478"},
    )

    assert version == "0.1.479"
    assert "version: 0.1.479\n" in chart.read_text(encoding="utf-8")
    assert 'appVersion: "abc12345"\n' in chart.read_text(encoding="utf-8")


def test_bump_chart_release_never_regresses_below_higher_reserved_series(tmp_path: Path) -> None:
    chart = tmp_path / "Chart.yaml"
    chart.write_text(
        "\n".join(
            [
                "apiVersion: v2",
                "name: palaceoftruth",
                "version: 0.1.476",
                'appVersion: "8a8f0e5d"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    version = bump_chart_release(chart, "abc12345", reserved_versions={"0.2.3"})

    assert version == "0.2.4"
    assert "version: 0.2.4\n" in chart.read_text(encoding="utf-8")


def test_bump_chart_release_compares_helm_build_metadata_tag_by_core_version(
    tmp_path: Path,
) -> None:
    chart = tmp_path / "Chart.yaml"
    chart.write_text(
        "\n".join(
            [
                "apiVersion: v2",
                "name: palaceoftruth",
                "version: 0.1.476",
                'appVersion: "8a8f0e5d"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    version = bump_chart_release(
        chart,
        "abc12345",
        reserved_versions={"0.1.478_build.1"},
    )

    assert version == "0.1.479"


def test_bump_chart_release_rejects_non_semantic_reserved_version(tmp_path: Path) -> None:
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
    with pytest.raises(ValueError, match="Reserved chart version is not semantic"):
        bump_chart_release(chart, "abc12345", reserved_versions={"latest"})


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
