#!/usr/bin/env python3
"""Host-side entry point for the rollout fence.

This is packaged/delivered SEPARATELY from the backend image. The backend image
does not need a Helm binary; the operator runs this script on a host that has
`git`, `helm`, `kubectl` and `skopeo`.

Usage:
    rollout_fence.py help
    rollout_fence.py dry-run --manifest FILE --manifest-sha256 SHA256
    rollout_fence.py run --manifest FILE --manifest-sha256 SHA256 \
        --store DIR --workdir DIR --repo-dir DIR

The `run` subcommand performs live read-only binding, verifies the OCI chart by
digest, binds approved GitOps values, consumes the frozen slot at-most-once,
then performs one bounded Helm action WHILE the Flux hold stays suspended.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend" / "scripts"))

from rollout_action_control import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
