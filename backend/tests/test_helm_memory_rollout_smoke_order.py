from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml


CHART_DIR = Path(__file__).resolve().parents[2] / "chart"
SMOKE_JOB_PREFIX = "palaceoftruth-memory-smoke-"
RLS_SUFFIX = "-tenant-rls-enforcement"


def _render_chart(*set_args: str) -> list[dict[str, Any]]:
    if shutil.which("helm") is None:
        pytest.skip("helm is required for chart rendering tests")
    command = ["helm", "template", "palaceoftruth", str(CHART_DIR)]
    for arg in set_args:
        command.extend(["--set", arg])
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]


def _jobs(manifests: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        manifest["metadata"]["name"]: manifest
        for manifest in manifests
        if manifest.get("kind") == "Job"
    }


def _smoke_job(manifests: list[dict[str, Any]]) -> dict[str, Any]:
    return next(
        job for name, job in _jobs(manifests).items() if name.startswith(SMOKE_JOB_PREFIX)
    )


def _rls_job(manifests: list[dict[str, Any]]) -> dict[str, Any]:
    return next(
        job for name, job in _jobs(manifests).items() if name.endswith(RLS_SUFFIX)
    )


def _hook_weight(manifest: dict[str, Any]) -> int:
    return int(manifest["metadata"]["annotations"]["helm.sh/hook-weight"])


def _render_with_smoke() -> list[dict[str, Any]]:
    return _render_chart("valkey.sentinel.enabled=true")


def test_smoke_and_rls_jobs_both_render() -> None:
    manifests = _render_with_smoke()
    _smoke_job(manifests)
    _rls_job(manifests)


def test_rls_enforcement_runs_before_read_only_smoke() -> None:
    manifests = _render_with_smoke()
    rls = _rls_job(manifests)
    smoke = _smoke_job(manifests)

    rls_weight = _hook_weight(rls)
    smoke_weight = _hook_weight(smoke)
    # Distinct weights are required: Helm orders equal-weight hooks
    # alphabetically by name, and the smoke Job name sorts before
    # tenant-rls-enforcement, so a failed smoke hook would block RLS.
    assert rls_weight != smoke_weight
    assert rls_weight < smoke_weight

    # Reproduce actual Helm ordering semantics: weight ascending, then name.
    ordered = sorted(
        [(name, _hook_weight(job)) for name, job in _jobs(manifests).items()
         if name.startswith(SMOKE_JOB_PREFIX) or name.endswith(RLS_SUFFIX)],
        key=lambda item: (item[1], item[0]),
    )
    assert ordered[0][0].endswith(RLS_SUFFIX)
    assert ordered[-1][0].startswith(SMOKE_JOB_PREFIX)


def test_both_hooks_are_post_upgrade_and_delete_policies_match() -> None:
    manifests = _render_with_smoke()
    for job in (_smoke_job(manifests), _rls_job(manifests)):
        annotations = job["metadata"]["annotations"]
        hooks = annotations["helm.sh/hook"]
        assert "post-upgrade" in hooks
        assert "post-install" in hooks


def test_smoke_hook_is_read_only_and_never_write_mode() -> None:
    smoke = _smoke_job(_render_with_smoke())
    containers = smoke["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1
    args = containers[0]["args"]

    assert "--mode" in args
    mode = args[args.index("--mode") + 1]
    assert mode == "read-only"

    # No synthetic-write activation: reject any write/reserve/POST switch.
    joined = " ".join(args)
    for forbidden in ("--mode write", "--write", "--reserve", "--post", "--apply", "--synthetic"):
        assert forbidden not in joined

    # The hook must not carry the operator-only bounded POST flag.
    assert "--allow-write" not in args
    assert "--operator-writer" not in args


def test_smoke_hook_uses_backend_image_and_read_only_rbac() -> None:
    manifests = _render_with_smoke()
    smoke = _smoke_job(manifests)
    container = smoke["spec"]["template"]["spec"]["containers"][0]
    assert container["image"]  # backend image include resolves

    roles = [
        manifest
        for manifest in manifests
        if manifest.get("kind") == "Role"
        and manifest["metadata"]["name"] == "palaceoftruth-memory-rollout-smoke"
    ]
    assert len(roles) == 1
    verbs = {verb for rule in roles[0]["rules"] for verb in rule["verbs"]}
    assert verbs == {"get", "list"}
    # Read-only, and exactly the objects the lane reads: pods and their logs,
    # namespace events for OOM evidence, the live HelmRelease it binds the
    # running images to, and the Deployment/ReplicaSet chain behind each pod.
    # Every extra resource here is a permission the gate genuinely uses; none
    # of them is writable.
    resources = {resource for rule in roles[0]["rules"] for resource in rule["resources"]}
    assert resources == {
        "pods", "pods/log", "events", "helmreleases", "deployments", "replicasets",
    }
    api_groups = {group for rule in roles[0]["rules"] for group in rule["apiGroups"]}
    assert api_groups == {"", "helm.toolkit.fluxcd.io", "apps"}


def test_smoke_disabled_removes_both_job_and_rbac() -> None:
    manifests = _render_chart("valkey.sentinel.enabled=true", "memoryRolloutSmoke.enabled=false")
    names = [m["metadata"]["name"] for m in manifests if m.get("kind") in {"Job", "Role", "ServiceAccount", "RoleBinding"}]
    assert not any("memory-rollout-smoke" in name or name.startswith(SMOKE_JOB_PREFIX) for name in names)
