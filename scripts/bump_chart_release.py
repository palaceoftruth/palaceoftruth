#!/usr/bin/env python3
"""Bump the Helm chart patch release."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


VERSION_RE = re.compile(r"^(version:\s*)(\"?)(\d+)\.(\d+)\.(\d+)(\"?)(\s*)$")
APP_VERSION_RE = re.compile(r"^(appVersion:\s*).*$")
IMAGE_DIGEST_RE = re.compile(r"^(\s{2})(backendDigest|workerDigest|frontendDigest):\s*.*$")
SEMVER_RE = re.compile(
    r"^(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.-]+)?(?:[+_][0-9A-Za-z.-]+)?$"
)


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


def _parse_semver(version: str) -> tuple[int, int, int]:
    match = SEMVER_RE.match(version.strip().strip('"'))
    if not match:
        raise ValueError(f"Reserved chart version is not semantic: {version}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def bump_chart_release(
    chart_path: Path,
    app_version: str,
    *,
    reserved_versions: set[str] | None = None,
    values_path: Path | None = None,
    backend_digest: str | None = None,
    worker_digest: str | None = None,
    frontend_digest: str | None = None,
) -> str:
    if not chart_path.is_file():
        raise ValueError(f"Chart file does not exist: {chart_path}")

    chart_lines = chart_path.read_text(encoding="utf-8").splitlines()
    reserved = {_parse_semver(version) for version in reserved_versions or set()}
    new_version: str | None = None

    for index, line in enumerate(chart_lines):
        match = VERSION_RE.match(line)
        if not match:
            continue

        current = (int(match.group(3)), int(match.group(4)), int(match.group(5)))
        # Published charts and concurrent release PRs are durable coordinates.
        # Advance from the greatest one so a stale main branch cannot regress or
        # collide with a release that was published before its PR was recorded.
        major, minor, patch = max({current, *reserved})
        next_patch = patch + 1
        new_version = f"{major}.{minor}.{next_patch}"
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
    digests = {
        "backendDigest": backend_digest,
        "workerDigest": worker_digest,
        "frontendDigest": frontend_digest,
    }
    if any(digests.values()):
        if not values_path or not values_path.is_file() or not all(digests.values()):
            raise ValueError("values path and all three image digests are required together")
        for value in digests.values():
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(value)):
                raise ValueError(f"Invalid OCI image digest: {value}")
        values_lines = values_path.read_text(encoding="utf-8").splitlines()
        updated_keys: set[str] = set()
        for index, line in enumerate(values_lines):
            match = IMAGE_DIGEST_RE.match(line)
            if match and match.group(2) in digests:
                key = match.group(2)
                values_lines[index] = f'{match.group(1)}{key}: "{digests[key]}"'
                updated_keys.add(key)
        if updated_keys != set(digests):
            raise ValueError("Failed to find all image digest keys in chart values")
        values_path.write_text("\n".join(values_lines) + "\n", encoding="utf-8")
    return new_version


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chart", default="chart/Chart.yaml", type=Path)
    parser.add_argument("--app-version", required=True)
    parser.add_argument("--values", type=Path)
    parser.add_argument("--backend-digest")
    parser.add_argument("--worker-digest")
    parser.add_argument("--frontend-digest")
    parser.add_argument(
        "--reserved-version",
        action="append",
        default=[],
        help="Existing chart version that the next patch release must exceed.",
    )
    args = parser.parse_args()

    try:
        print(
            bump_chart_release(
                args.chart,
                args.app_version,
                reserved_versions=set(args.reserved_version),
                values_path=args.values,
                backend_digest=args.backend_digest,
                worker_digest=args.worker_digest,
                frontend_digest=args.frontend_digest,
            )
        )
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
