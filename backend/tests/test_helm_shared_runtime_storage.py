from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
CHART_DIR = REPO_ROOT / "chart"
RUNTIME_DEPLOYMENTS = {
    "palaceoftruth-backend",
    "palaceoftruth-worker",
    "palaceoftruth-media-worker",
    "palaceoftruth-palace-worker",
}


def _render_chart(*set_args: str) -> list[dict[str, Any]]:
    if shutil.which("helm") is None:
        pytest.skip("helm is required for chart rendering tests")
    command = ["helm", "template", "palaceoftruth", str(CHART_DIR)]
    for arg in set_args:
        command.extend(["--set", arg])
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]


def _deployment_by_name(manifests: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for manifest in manifests:
        if manifest.get("kind") == "Deployment" and manifest.get("metadata", {}).get("name") == name:
            return manifest
    raise AssertionError(f"deployment {name} was not rendered")


def _temp_volume(deployment: dict[str, Any]) -> dict[str, Any]:
    volumes = deployment["spec"]["template"]["spec"].get("volumes", [])
    for volume in volumes:
        if volume.get("name") == "temp-files":
            return volume
    raise AssertionError(f"{deployment['metadata']['name']} did not render temp-files volume")


def _temp_mount(deployment: dict[str, Any]) -> dict[str, Any]:
    containers = deployment["spec"]["template"]["spec"]["containers"]
    mounts = containers[0].get("volumeMounts", [])
    for mount in mounts:
        if mount.get("name") == "temp-files":
            return mount
    raise AssertionError(f"{deployment['metadata']['name']} did not render temp-files mount")


def _upload_artifacts_mounts(deployment: dict[str, Any]) -> list[dict[str, Any]]:
    containers = deployment["spec"]["template"]["spec"]["containers"]
    return [
        mount
        for mount in containers[0].get("volumeMounts", [])
        if mount.get("name") == "upload-artifacts"
    ]


def test_runtime_storage_defaults_to_shared_upload_artifacts() -> None:
    manifests = _render_chart()

    runtime_pvcs = [
        manifest
        for manifest in manifests
        if manifest.get("kind") == "PersistentVolumeClaim"
        and manifest.get("metadata", {}).get("name") == "palaceoftruth-runtime"
    ]
    assert len(runtime_pvcs) == 1
    assert runtime_pvcs[0]["spec"]["accessModes"] == ["ReadWriteMany"]
    assert "storageClassName" not in runtime_pvcs[0]["spec"]

    for name in RUNTIME_DEPLOYMENTS:
        deployment = _deployment_by_name(manifests, name)
        assert _temp_mount(deployment)["mountPath"] == "/tmp/palaceoftruth"
        assert _upload_artifacts_mounts(deployment) == [
            {"name": "upload-artifacts", "mountPath": "/tmp/palaceoftruth/upload-artifacts"}
        ]
        assert _temp_volume(deployment) == {"name": "temp-files", "emptyDir": {}}
        assert {"name": "upload-artifacts", "persistentVolumeClaim": {"claimName": "palaceoftruth-runtime"}} in deployment[
            "spec"
        ]["template"]["spec"]["volumes"]


def test_shared_runtime_storage_can_be_disabled_for_local_single_pod_installs() -> None:
    manifests = _render_chart("sharedRuntimeStorage.enabled=false")

    assert not any(
        manifest.get("kind") == "PersistentVolumeClaim"
        and manifest.get("metadata", {}).get("name") == "palaceoftruth-runtime"
        for manifest in manifests
    )
    for name in RUNTIME_DEPLOYMENTS:
        deployment = _deployment_by_name(manifests, name)
        assert _temp_mount(deployment)["mountPath"] == "/tmp/palaceoftruth"
        assert _upload_artifacts_mounts(deployment) == []
        assert _temp_volume(deployment) == {"name": "temp-files", "emptyDir": {}}


def test_shared_runtime_storage_mounts_one_pvc_for_api_and_workers() -> None:
    manifests = _render_chart(
        "sharedRuntimeStorage.enabled=true",
        "sharedRuntimeStorage.storageClassName=custom-rwx",
    )

    runtime_pvcs = [
        manifest
        for manifest in manifests
        if manifest.get("kind") == "PersistentVolumeClaim"
        and manifest.get("metadata", {}).get("name") == "palaceoftruth-runtime"
    ]
    assert len(runtime_pvcs) == 1
    assert runtime_pvcs[0]["spec"]["accessModes"] == ["ReadWriteMany"]
    assert runtime_pvcs[0]["spec"]["storageClassName"] == "custom-rwx"

    for name in RUNTIME_DEPLOYMENTS:
        deployment = _deployment_by_name(manifests, name)
        assert _temp_mount(deployment)["mountPath"] == "/tmp/palaceoftruth"
        assert _upload_artifacts_mounts(deployment) == [
            {"name": "upload-artifacts", "mountPath": "/tmp/palaceoftruth/upload-artifacts"}
        ]
        assert _temp_volume(deployment) == {"name": "temp-files", "emptyDir": {}}
        assert {"name": "upload-artifacts", "persistentVolumeClaim": {"claimName": "palaceoftruth-runtime"}} in deployment[
            "spec"
        ]["template"]["spec"]["volumes"]


def test_high_availability_automatically_shares_runtime_storage() -> None:
    manifests = _render_chart("highAvailability.enabled=true")

    runtime_pvcs = [
        manifest
        for manifest in manifests
        if manifest.get("kind") == "PersistentVolumeClaim"
        and manifest.get("metadata", {}).get("name") == "palaceoftruth-runtime"
    ]
    assert len(runtime_pvcs) == 1
    assert runtime_pvcs[0]["spec"]["accessModes"] == ["ReadWriteMany"]
    assert "storageClassName" not in runtime_pvcs[0]["spec"]

    for name in RUNTIME_DEPLOYMENTS:
        deployment = _deployment_by_name(manifests, name)
        assert _temp_mount(deployment)["mountPath"] == "/tmp/palaceoftruth"
        assert _upload_artifacts_mounts(deployment) == [
            {"name": "upload-artifacts", "mountPath": "/tmp/palaceoftruth/upload-artifacts"}
        ]
        assert _temp_volume(deployment) == {"name": "temp-files", "emptyDir": {}}
        assert {"name": "upload-artifacts", "persistentVolumeClaim": {"claimName": "palaceoftruth-runtime"}} in deployment[
            "spec"
        ]["template"]["spec"]["volumes"]


def test_shared_runtime_storage_can_use_existing_claim_without_rendering_pvc() -> None:
    manifests = _render_chart(
        "sharedRuntimeStorage.enabled=true",
        "sharedRuntimeStorage.existingClaim=palace-upload-artifacts",
    )

    assert not any(
        manifest.get("kind") == "PersistentVolumeClaim"
        and manifest.get("metadata", {}).get("name") == "palace-upload-artifacts"
        for manifest in manifests
    )
    for name in RUNTIME_DEPLOYMENTS:
        deployment = _deployment_by_name(manifests, name)
        assert _upload_artifacts_mounts(deployment) == [
            {"name": "upload-artifacts", "mountPath": "/tmp/palaceoftruth/upload-artifacts"}
        ]
        assert {"name": "upload-artifacts", "persistentVolumeClaim": {"claimName": "palace-upload-artifacts"}} in deployment[
            "spec"
        ]["template"]["spec"]["volumes"]
