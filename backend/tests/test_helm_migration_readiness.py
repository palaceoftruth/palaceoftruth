from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml


CHART_DIR = Path(__file__).resolve().parents[2] / "chart"


def _render_chart(*set_args: str, is_upgrade: bool = False) -> list[dict[str, Any]]:
    if shutil.which("helm") is None:
        pytest.skip("helm is required for chart rendering tests")
    command = ["helm", "template", "palaceoftruth", str(CHART_DIR)]
    if is_upgrade:
        command.append("--is-upgrade")
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


def _backend_deployment(manifests: list[dict[str, Any]]) -> dict[str, Any]:
    for manifest in manifests:
        if manifest.get("kind") == "Deployment" and manifest.get("metadata", {}).get("name") == "palaceoftruth-backend":
            return manifest
    raise AssertionError("backend Deployment was not rendered")


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
        "value": "postgresql+asyncpg://$(DB_USER):$(DB_PASSWORD)@$(DB_HOST):$(DB_PORT)/$(DB_NAME)?sslmode=verify-full",
    }
    assert env["DATABASE_SSL_ROOT_CERT"]["value"] == "/etc/palaceoftruth/database-tls/ca.crt"
    assert {mount["name"] for mount in readiness["volumeMounts"]} >= {"database-tls"}
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


def test_database_tls_can_be_disabled_without_rendering_verify_full() -> None:
    manifests = _render_chart("databaseTls.enabled=false")
    job = _migration_job(manifests)
    pod_spec = job["spec"]["template"]["spec"]
    containers = [*pod_spec.get("initContainers", []), *pod_spec["containers"]]

    for container in containers:
        env = {entry["name"]: entry for entry in container["env"]}
        assert "sslmode=verify-full" not in env["DATABASE_URL"]["value"]
        assert "DATABASE_SSL_ROOT_CERT" not in env
        assert "database-tls" not in {mount["name"] for mount in container.get("volumeMounts", [])}


def test_rollout_defers_rls_until_post_rollout_hook() -> None:
    manifests = _render_chart()
    migration = _migration_job(manifests)
    migration_env = {
        entry["name"]: entry
        for entry in migration["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    enforcement = next(
        manifest
        for manifest in manifests
        if manifest.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component")
        == "tenant-rls-enforcement"
    )

    assert migration_env["DEFER_TENANT_RLS_ENFORCEMENT"]["value"] == "true"
    assert enforcement["metadata"]["annotations"]["helm.sh/hook"] == "post-install,post-upgrade"
    assert enforcement["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == "3"
    enforcement_spec = enforcement["spec"]["template"]["spec"]
    assert enforcement_spec["serviceAccountName"] == "palaceoftruth-tenant-rls-enforcement"
    rollout_gate = enforcement_spec["initContainers"][0]
    assert rollout_gate["name"] == "wait-for-tenant-aware-rollout"
    assert "updatedReplicas" in rollout_gate["command"][-1]
    assert "readyReplicas" in rollout_gate["command"][-1]
    database_gate = enforcement_spec["initContainers"][1]
    assert database_gate["name"] == "wait-for-writable-database"
    assert database_gate["command"] == ["python", "-m", "app.wait_for_database"]
    database_env = {entry["name"]: entry for entry in database_gate["env"]}
    assert database_env["DATABASE_URL"]["value"].startswith("postgresql+asyncpg://")
    assert database_env["MIGRATION_DB_WAIT_TIMEOUT_SECONDS"]["value"] == "240"
    assert {mount["name"] for mount in database_gate["volumeMounts"]} >= {
        "database-tls"
    }
    role = next(
        manifest
        for manifest in manifests
        if manifest.get("kind") == "Role"
        and manifest.get("metadata", {}).get("name") == "palaceoftruth-tenant-rls-enforcement"
    )
    assert role["rules"] == [
        {"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["get", "list"]}
    ]
    assert enforcement["spec"]["template"]["spec"]["containers"][0]["command"] == [
        "python",
        "-m",
        "app.enforce_tenant_rls",
    ]


def test_disabled_migration_job_still_defers_and_enforces_rls_after_rollout() -> None:
    manifests = _render_chart("migrations.enabled=false", is_upgrade=True)
    backend = _backend_deployment(manifests)
    backend_env = {
        entry["name"]: entry
        for entry in backend["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    enforcement = next(
        manifest
        for manifest in manifests
        if manifest.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component")
        == "tenant-rls-enforcement"
    )

    assert backend_env["DEFER_TENANT_RLS_ENFORCEMENT"]["value"] == "true"
    assert enforcement["metadata"]["annotations"]["helm.sh/hook"] == "post-install,post-upgrade"


def test_backend_startup_probe_allows_dependency_gate_budget() -> None:
    """The probe must outlast both startup gates in app.main's lifespan.

    The API waits up to 300s for a writable database primary and then up to 180s
    for a Sentinel-elected Redis primary before it opens the ARQ pool. If the
    probe budget is shorter than their sum, a pod that is legitimately waiting
    gets killed mid-startup.
    """
    database_gate_seconds = 300
    sentinel_gate_seconds = 180
    helm_release_timeout_seconds = 900

    backend = _backend_deployment(_render_chart())
    container = backend["spec"]["template"]["spec"]["containers"][0]

    startup_probe = container["startupProbe"]
    assert startup_probe["httpGet"] == {
        "path": "/api/v1/health",
        "port": 8000,
        "httpHeaders": [
            {"name": "Host", "value": "api.palaceoftruth.example.com"},
        ],
    }
    assert startup_probe["periodSeconds"] == 5
    assert startup_probe["timeoutSeconds"] == 5
    assert startup_probe["failureThreshold"] == 150

    budget_seconds = startup_probe["periodSeconds"] * startup_probe["failureThreshold"]
    # Headroom on top of the gates covers Alembic and the idempotent seed work.
    assert budget_seconds > database_gate_seconds + sentinel_gate_seconds
    # Overrunning Helm's own timeout would fail the release instead of the pod.
    assert budget_seconds < helm_release_timeout_seconds


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
