#!/usr/bin/env python3
"""Detect whether a change requires an agent-client plugin release."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = Path("third_party_plugins/agent_clients/palaceoftruth-memory")
PACKAGER = Path("scripts/package_agent_memory_plugin.py")
ZERO_SHA = "0" * 40


def should_release_for_changed_paths(paths: list[str]) -> bool:
    return any(Path(path) == PACKAGER or PLUGIN_DIR in Path(path).parents for path in paths)


def changed_paths(before: str, after: str) -> list[str]:
    if not before or before == ZERO_SHA:
        return [str(PLUGIN_DIR / ".codex-plugin/plugin.json")]
    result = subprocess.run(
        ["git", "diff", "--name-only", before, after, "--", str(PLUGIN_DIR), str(PACKAGER)],
        cwd=REPO_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return result.stdout.splitlines()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", default="")
    parser.add_argument("--after", default="")
    args = parser.parse_args()
    print("true" if should_release_for_changed_paths(changed_paths(args.before, args.after)) else "false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
