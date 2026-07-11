from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml


CHART_DIR = Path(__file__).resolve().parents[2] / "chart"


def _render_chart(*set_args: str) -> list[dict[str, Any]]:
    if shutil.which("helm") is None:
        pytest.skip("helm is required for chart rendering tests")
    command = ["helm", "template", "palaceoftruth", str(CHART_DIR)]
    for arg in set_args:
        command.extend(["--set", arg])
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]


def _migration_job(manifests: list[dict[str, Any]]) -> dict[str, Any]:
    jobs = [manifest for manifest in manifests if manifest.get("kind") == "Job"]
    for job in jobs:
        if job.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component") == "migration":
            return job
    raise AssertionError("migration Job was not rendered")


def test_migration_job_waits_for_writable_database_before_alembic() -> None:
    job = _migration_job(
        _render_chart(
            "migrations.readiness.timeoutSeconds=30",
            "migrations.readiness.intervalSeconds=2",
            "migrations.readiness.connectTimeoutSeconds=3",
        )
    )
    pod_spec = job["spec"]["template"]["spec"]
    readiness = pod_spec["initContainers"][0]

    assert readiness["name"] == "wait-for-writable-database"
    assert readiness["image"] == pod_spec["containers"][0]["image"]
    assert readiness["command"] == ["python", "-m", "app.wait_for_database"]
    env = {entry["name"]: entry for entry in readiness["env"]}
    assert env["DATABASE_URL"] == {
        "name": "DATABASE_URL",
        "value": "postgresql+asyncpg://$(DB_USER):$(DB_PASSWORD)@$(DB_HOST):$(DB_PORT)/$(DB_NAME)",
    }
    assert env["MIGRATION_DB_WAIT_TIMEOUT_SECONDS"]["value"] == "30"
    assert env["MIGRATION_DB_WAIT_INTERVAL_SECONDS"]["value"] == "2"
    assert env["MIGRATION_DB_CONNECT_TIMEOUT_SECONDS"]["value"] == "3"
    assert pod_spec["containers"][0]["command"] == ["alembic", "upgrade", "head"]
    assert job["metadata"]["annotations"] == {
        "argocd.argoproj.io/sync-wave": "1",
        "helm.sh/hook": "post-install,pre-upgrade",
        "helm.sh/hook-weight": "-10",
        "helm.sh/hook-delete-policy": "before-hook-creation,hook-succeeded",
    }


def test_migration_readiness_gate_can_be_disabled() -> None:
    job = _migration_job(_render_chart("migrations.readiness.enabled=false"))
    assert "initContainers" not in job["spec"]["template"]["spec"]


def test_readiness_timeout_must_leave_time_for_alembic() -> None:
    if shutil.which("helm") is None:
        pytest.skip("helm is required for chart rendering tests")
    result = subprocess.run(
        [
            "helm",
            "template",
            "palaceoftruth",
            str(CHART_DIR),
            "--set",
            "migrations.readiness.timeoutSeconds=300",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "timeoutSeconds must be less than migrations.activeDeadlineSeconds" in result.stderr
