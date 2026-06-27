#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = Path("third_party_plugins/hermes/memory/palaceoftruth")
PACKAGING_SCRIPT = Path("scripts/package_hermes_memory_plugin.py")
RELEASE_TRIGGER_FILES = {
    PLUGIN_DIR / "__init__.py",
    PLUGIN_DIR / "Dockerfile",
    PLUGIN_DIR / "README.md",
    PLUGIN_DIR / "plugin.yaml",
    PACKAGING_SCRIPT,
}
ZERO_SHA = "0" * 40


def should_release_for_changed_paths(paths: list[str]) -> bool:
    changed_paths = {Path(path) for path in paths}
    return any(path in RELEASE_TRIGGER_FILES for path in changed_paths)


def _changed_paths(before: str, after: str, repo_root: Path) -> list[str]:
    if not before or before == ZERO_SHA:
        return [str(path) for path in sorted(RELEASE_TRIGGER_FILES)]
    if not after:
        raise SystemExit("--after is required when --before is set")

    result = subprocess.run(
        [
            "git",
            "diff",
            "--name-only",
            before,
            after,
            "--",
            str(PLUGIN_DIR),
            str(PACKAGING_SCRIPT),
        ],
        cwd=repo_root,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return [line for line in result.stdout.splitlines() if line]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Detect whether a Palace Hermes plugin change needs a versioned release."
    )
    parser.add_argument("--before", default="", help="Base commit SHA from the GitHub push event.")
    parser.add_argument("--after", default="", help="Head commit SHA to compare.")
    args = parser.parse_args()

    print("true" if should_release_for_changed_paths(_changed_paths(args.before, args.after, REPO_ROOT)) else "false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
