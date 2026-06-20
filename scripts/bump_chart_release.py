#!/usr/bin/env python3
"""Bump the Helm chart patch release."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


VERSION_RE = re.compile(r"^(version:\s*)(\"?)(\d+)\.(\d+)\.(\d+)(\"?)(\s*)$")
APP_VERSION_RE = re.compile(r"^(appVersion:\s*).*$")


def _replace_one_line(
    lines: list[str],
    pattern: re.Pattern[str],
    replacement: str,
    *,
    missing_message: str,
) -> list[str]:
    for index, line in enumerate(lines):
        if pattern.match(line):
            updated = lines.copy()
            updated[index] = replacement
            return updated
    raise ValueError(missing_message)


def bump_chart_release(chart_path: Path, app_version: str) -> str:
    if not chart_path.is_file():
        raise ValueError(f"Chart file does not exist: {chart_path}")

    chart_lines = chart_path.read_text(encoding="utf-8").splitlines()
    new_version: str | None = None

    for index, line in enumerate(chart_lines):
        match = VERSION_RE.match(line)
        if not match:
            continue

        major, minor, patch = (int(match.group(3)), int(match.group(4)), int(match.group(5)))
        new_version = f"{major}.{minor}.{patch + 1}"
        chart_lines[index] = f"{match.group(1)}{new_version}{match.group(7)}"
        break

    if new_version is None:
        raise ValueError(f"Failed to find semantic chart version in {chart_path}")

    chart_lines = _replace_one_line(
        chart_lines,
        APP_VERSION_RE,
        f'appVersion: "{app_version}"',
        missing_message=f"Failed to find appVersion in {chart_path}",
    )

    chart_path.write_text("\n".join(chart_lines) + "\n", encoding="utf-8")
    return new_version


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chart", default="chart/Chart.yaml", type=Path)
    parser.add_argument("--app-version", required=True)
    args = parser.parse_args()

    try:
        print(bump_chart_release(args.chart, args.app_version))
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
