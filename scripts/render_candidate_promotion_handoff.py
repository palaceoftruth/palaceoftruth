#!/usr/bin/env python3
"""Render a non-mutating promotion handoff for a sanitized curation artifact."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://curation-promotion:unused@localhost/palace")
os.environ.setdefault("API_KEY", "curation-promotion-unused")
os.environ.setdefault("OPENAI_API_KEY", "curation-promotion-unused")
os.environ.setdefault("OPENROUTER_API_KEY", "curation-promotion-unused")

from app.services.candidate_curation_promotion import render_candidate_promotion_handoff  # noqa: E402
from app.services.curation_artifacts import CandidateCurationArtifactError  # noqa: E402


DEFAULT_PACK = REPO_ROOT / "backend" / "tests" / "fixtures" / "candidate_curation_scoring_pack.json"


@dataclass
class DryRunArtifact:
    id: uuid.UUID
    artifact_kind: str
    target_runtime: str
    target_surface: str
    status: str
    source_item_ids: list[str]
    source_digests: dict[str, str]
    candidate_body: str
    privacy_review: dict[str, Any]
    eval_summary: dict[str, Any]
    approval: dict[str, Any]
    metadata_: dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render an approved candidate curation artifact into a PR/handoff template."
    )
    parser.add_argument("--pack", default=str(DEFAULT_PACK), help="Sanitized fixture pack or artifact JSON file.")
    parser.add_argument("--artifact-id", required=True, help="Artifact id from the pack to render.")
    parser.add_argument("--output", help="Write rendered markdown to this path instead of stdout.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        payload = json.loads(Path(args.pack).read_text(encoding="utf-8"))
        row = _select_artifact(payload, args.artifact_id)
        handoff = render_candidate_promotion_handoff(_artifact_from_row(row))
    except (json.JSONDecodeError, OSError, CandidateCurationArtifactError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    rendered = handoff["rendered_handoff"]
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


def _select_artifact(payload: dict[str, Any], artifact_id: str) -> dict[str, Any]:
    if "artifacts" in payload:
        artifacts = payload.get("artifacts")
        if not isinstance(artifacts, list):
            raise ValueError("artifact pack must contain an artifacts list")
        for row in artifacts:
            if isinstance(row, dict) and row.get("artifact_id") == artifact_id:
                return row
        raise ValueError(f"artifact not found in pack: {artifact_id}")
    if payload.get("artifact_id") == artifact_id:
        return payload
    raise ValueError(f"artifact not found: {artifact_id}")


def _artifact_from_row(row: dict[str, Any]) -> DryRunArtifact:
    artifact_id = row.get("id") or row.get("artifact_id")
    return DryRunArtifact(
        id=uuid.uuid5(uuid.NAMESPACE_URL, str(artifact_id)),
        artifact_kind=str(row.get("artifact_kind") or "candidate_skill"),
        target_runtime=str(row.get("target_runtime") or ""),
        target_surface=str(row.get("target_surface") or ""),
        status=str(row.get("status") or "draft"),
        source_item_ids=[str(value) for value in row.get("source_item_ids") or []],
        source_digests={str(key): str(value) for key, value in (row.get("source_digests") or {}).items()},
        candidate_body=str(row.get("candidate_body") or ""),
        privacy_review=dict(row.get("privacy_review") or {}),
        eval_summary=dict(row.get("eval_summary") or {}),
        approval=dict(row.get("approval") or {}),
        metadata_=dict(row.get("metadata") or {}),
    )


if __name__ == "__main__":
    raise SystemExit(main())
