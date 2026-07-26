"""Plan or run the compiled SAR-1083 relationship telemetry canary.

Dry-run is the default and performs no network or database calls. ``--write``
is accepted only inside the exact Palace staging namespace and calls the
loopback admin endpoint so relationship metrics remain visible in the scraped
backend process.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.relationship_canary_contract import (  # noqa: E402
    FIXTURE_SHA256,
    TARGET_NAMESPACE,
    build_plan,
)

NAMESPACE_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
LOOPBACK_URL = "http://127.0.0.1:8000/api/v1/admin/canaries/sar-1083"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="Run the authorized fixture through the fixed loopback admin endpoint.",
    )
    parser.add_argument(
        "--authorization-id",
        help="Identifier for the fresh explicit operator approval; required with --write.",
    )
    parser.add_argument(
        "--expected-app-version",
        help="Exact APP_VERSION expected in the live backend; required with --write.",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.write and not args.authorization_id:
        parser.error("--authorization-id is required with --write")
    if args.write and not args.expected_app_version:
        parser.error("--expected-app-version is required with --write")
    if not args.write and (args.authorization_id or args.expected_app_version):
        parser.error("authorization and revision arguments are valid only with --write")
    return args


def _live_namespace() -> str:
    try:
        return NAMESPACE_PATH.read_text().strip()
    except OSError as exc:
        raise RuntimeError("Kubernetes namespace identity is unavailable") from exc


async def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.write:
        print(json.dumps(build_plan(), sort_keys=True))
        return 0

    if _live_namespace() != TARGET_NAMESPACE:
        raise RuntimeError("live canary is allowed only in the compiled staging namespace")
    admin_secret = os.environ.get("PALACEOFTRUTH_ADMIN_SECRET", "")
    if not admin_secret:
        raise RuntimeError("PALACEOFTRUTH_ADMIN_SECRET is unavailable")

    # Keep the default dry-run independent of application/network packages.
    import httpx

    payload = {
        "authorization_id": args.authorization_id,
        "expected_app_version": args.expected_app_version,
        "fixture_sha256": FIXTURE_SHA256,
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
        response = await client.post(
            LOOPBACK_URL,
            headers={"X-Admin-Secret": admin_secret},
            json=payload,
        )
    try:
        report = response.json()
    except ValueError as exc:
        raise RuntimeError(f"canary endpoint returned HTTP {response.status_code} without JSON") from exc
    print(json.dumps(report, sort_keys=True))
    return 0 if response.is_success and report.get("passed") is True else 1


def main() -> int:
    try:
        return asyncio.run(run())
    except Exception as exc:
        # Only bounded error classes/messages are emitted. Never include
        # request headers, environment values, or response bodies.
        print(
            json.dumps(
                {
                    "task_id": "SAR-1083",
                    "status": "error",
                    "error_class": exc.__class__.__name__,
                    "message": str(exc),
                },
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
