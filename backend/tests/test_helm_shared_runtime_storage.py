from __future__ import annotations

import json
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


def _manifest_by_kind_name(manifests: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    for manifest in manifests:
        if manifest.get("kind") == kind and manifest.get("metadata", {}).get("name") == name:
            return manifest
    raise AssertionError(f"{kind} {name} was not rendered")


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


def test_firecrawl_config_renders_for_self_hosted_scraping() -> None:
    manifests = _render_chart(
        "config.webpageScraperProvider=firecrawl-self-hosted",
        "config.firecrawlBaseUrl=https://firecrawl.example.internal/v2",
        "config.firecrawlTimeoutSeconds=45",
        "config.firecrawlOnlyMainContent=false",
    )

    config_map = _manifest_by_kind_name(manifests, "ConfigMap", "palaceoftruth-config")

    assert config_map["data"]["WEBPAGE_SCRAPER_PROVIDER"] == "firecrawl-self-hosted"
    assert config_map["data"]["FIRECRAWL_BASE_URL"] == "https://firecrawl.example.internal/v2"
    assert config_map["data"]["FIRECRAWL_TIMEOUT_SECONDS"] == "45"
    assert config_map["data"]["FIRECRAWL_ONLY_MAIN_CONTENT"] == "false"


def test_default_delegated_agent_policy_allows_hermes_orchestrator_specialists() -> None:
    manifests = _render_chart()
    config_map = _manifest_by_kind_name(manifests, "ConfigMap", "palaceoftruth-config")

    policies = json.loads(config_map["data"]["PALACEOFTRUTH_DELEGATED_AGENT_MEMORY_READ_POLICIES"])

    assert policies == [
        {
            "tenant_id": "default",
            "subject_agent_scope_key": "orchestrator",
            "read_agent_scope_keys": ["security", "macos"],
            "policy_id": "hermes-orchestrator-security-macos",
            "policy_source": "chart/values.yaml",
            "require_access_reason": True,
            "max_cross_agent_scopes": 2,
        }
    ]
    assert "allow_all_agent_scopes" not in policies[0]


def test_firecrawl_api_key_can_be_sourced_from_external_secret() -> None:
    manifests = _render_chart(
        "externalSecrets.enabled=true",
        "externalSecrets.secretStoreName=bitwarden-fields",
        "externalSecrets.appSecretItemId=app-secret-item",
        "externalSecrets.registrySecretItemId=registry-secret-item",
        "externalSecrets.firecrawlApiKeyProperty=firecrawl-api-key",
    )

    external_secret = _manifest_by_kind_name(manifests, "ExternalSecret", "palaceoftruth-app-secrets")
    assert {
        "secretKey": "FIRECRAWL_API_KEY",
        "remoteRef": {"key": "app-secret-item", "property": "firecrawl-api-key"},
    } in external_secret["spec"]["data"]


def test_backend_service_exposes_prometheus_scrape_metadata_without_servicemonitor_by_default() -> None:
    manifests = _render_chart()

    backend_service = _manifest_by_kind_name(manifests, "Service", "palaceoftruth-backend")

    assert backend_service["metadata"]["labels"]["app"] == "palaceoftruth-backend"
    assert backend_service["metadata"]["annotations"] == {
        "prometheus.io/scrape": "true",
        "prometheus.io/path": "/api/v1/metrics",
        "prometheus.io/port": "8000",
    }
    assert backend_service["spec"]["ports"][0]["name"] == "http"
    assert not any(manifest.get("kind") == "ServiceMonitor" for manifest in manifests)


def test_backend_servicemonitor_renders_when_enabled() -> None:
    manifests = _render_chart(
        "metrics.serviceMonitor.enabled=true",
        "metrics.serviceMonitor.labels.release=kube-prometheus",
        "metrics.serviceMonitor.interval=15s",
        "metrics.serviceMonitor.scrapeTimeout=5s",
    )

    service_monitor = _manifest_by_kind_name(manifests, "ServiceMonitor", "palaceoftruth-backend")

    assert service_monitor["metadata"]["labels"]["release"] == "kube-prometheus"
    assert service_monitor["spec"]["selector"]["matchLabels"] == {"app": "palaceoftruth-backend"}
    assert service_monitor["spec"]["endpoints"] == [
        {
            "port": "http",
            "path": "/api/v1/metrics",
            "interval": "15s",
            "scrapeTimeout": "5s",
        }
    ]
