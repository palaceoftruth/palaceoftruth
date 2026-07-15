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


def _render_chart(
    *set_args: str,
    release_name: str = "palaceoftruth",
    namespace: str | None = None,
) -> list[dict[str, Any]]:
    if shutil.which("helm") is None:
        pytest.skip("helm is required for chart rendering tests")
    command = ["helm", "template", release_name, str(CHART_DIR)]
    if namespace is not None:
        command.extend(["--namespace", namespace])
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


def _manifest_by_kind_name_prefix(manifests: list[dict[str, Any]], kind: str, prefix: str) -> dict[str, Any]:
    for manifest in manifests:
        name = manifest.get("metadata", {}).get("name")
        if manifest.get("kind") == kind and isinstance(name, str) and name.startswith(prefix):
            return manifest
    raise AssertionError(f"{kind} with name prefix {prefix} was not rendered")


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


def _container_env(container: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["name"]: entry for entry in container.get("env", [])}


def _arg_value(args: list[str], name: str) -> str:
    try:
        index = args.index(name)
    except ValueError as exc:
        raise AssertionError(f"{name} was not rendered in args") from exc
    try:
        return args[index + 1]
    except IndexError as exc:
        raise AssertionError(f"{name} did not render a value") from exc


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


def test_frontend_proxy_does_not_inject_backend_api_key() -> None:
    manifests = _render_chart()
    nginx_config = _manifest_by_kind_name(manifests, "ConfigMap", "palaceoftruth-frontend-nginx")
    frontend = _deployment_by_name(manifests, "palaceoftruth-frontend")
    container = frontend["spec"]["template"]["spec"]["containers"][0]

    assert "proxy_set_header X-API-Key" not in nginx_config["data"]["default.conf"]
    assert container.get("env", []) == []


def test_backend_cors_uses_explicit_allowlist() -> None:
    manifests = _render_chart("config.corsAllowedOrigins=https://palace.example.com\\,https://api.palace.example.com")
    config_map = _manifest_by_kind_name(manifests, "ConfigMap", "palaceoftruth-config")

    assert config_map["data"]["CORS_ALLOWED_ORIGINS"] == "https://palace.example.com,https://api.palace.example.com"
    assert config_map["data"]["CORS_ALLOWED_ORIGINS"] != "*"


def test_mcp_deployment_defaults_to_explicit_legacy_api_key_fallback() -> None:
    manifests = _render_chart()
    deployment = _deployment_by_name(manifests, "palaceoftruth-mcp")
    env = _container_env(deployment["spec"]["template"]["spec"]["containers"][0])

    assert env["PALACEOFTRUTH_API_KEY"]["valueFrom"]["secretKeyRef"]["key"] == "API_KEY"
    assert env["PALACEOFTRUTH_MCP_CLIENT_KEY"]["value"] == "helm-mcp"


def test_mcp_oauth_only_mode_omits_broad_api_key_and_mounts_oauth_secret() -> None:
    manifests = _render_chart(
        "mcp.legacyApiKeyAuthEnabled=false",
        "mcp.oauthClientSecretKey=MCP_CLIENT_SECRET",
        "mcp.oauthTokenUrl=https://api.palace.example/api/v1/memory/mcp/oauth/token",
        "mcp.oauthResource=https://api.palace.example/api/v1",
        "mcp.oauthAudience=https://api.palace.example/api/v1",
    )
    deployment = _deployment_by_name(manifests, "palaceoftruth-mcp")
    env = _container_env(deployment["spec"]["template"]["spec"]["containers"][0])

    assert "PALACEOFTRUTH_API_KEY" not in env
    assert env["PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET"]["valueFrom"]["secretKeyRef"]["key"] == "MCP_CLIENT_SECRET"
    assert env["PALACEOFTRUTH_MCP_OAUTH_TOKEN_URL"]["value"] == "https://api.palace.example/api/v1/memory/mcp/oauth/token"
    assert env["PALACEOFTRUTH_MCP_OAUTH_RESOURCE"]["value"] == "https://api.palace.example/api/v1"
    assert env["PALACEOFTRUTH_MCP_OAUTH_AUDIENCE"]["value"] == "https://api.palace.example/api/v1"


def test_mcp_deployment_renders_default_memory_scope_env() -> None:
    manifests = _render_chart(
        "mcp.defaultScopeType=agent",
        "mcp.defaultScopeKey=karen",
    )
    deployment = _deployment_by_name(manifests, "palaceoftruth-mcp")
    env = _container_env(deployment["spec"]["template"]["spec"]["containers"][0])

    assert env["PALACEOFTRUTH_DEFAULT_SCOPE_TYPE"]["value"] == "agent"
    assert env["PALACEOFTRUTH_DEFAULT_SCOPE_KEY"]["value"] == "karen"


def test_rollout_smoke_oauth_only_mode_verifies_oauth_identity_without_api_key() -> None:
    manifests = _render_chart(
        "valkey.sentinel.enabled=true",
        "mcp.legacyApiKeyAuthEnabled=false",
        "mcp.oauthClientSecretKey=MCP_CLIENT_SECRET",
        "mcp.oauthTokenUrl=https://api.palace.example/api/v1/memory/mcp/oauth/token",
        "mcp.oauthResource=https://api.palace.example/api/v1",
        "memoryRolloutSmoke.expectedAuthMode=mcp_oauth",
        "memoryRolloutSmoke.expectedTenantId=tenant-a",
        "memoryRolloutSmoke.expectedClientKey=helm-mcp",
        "memoryRolloutSmoke.expectedScopes[0]=read",
        "memoryRolloutSmoke.expectedScopes[1]=write",
        "memoryRolloutSmoke.requestTimeoutSeconds=60",
    )
    job = _manifest_by_kind_name_prefix(manifests, "Job", "palaceoftruth-memory-smoke-")
    container = job["spec"]["template"]["spec"]["containers"][0]
    env = _container_env(container)

    assert "PALACEOFTRUTH_API_KEY" not in env
    assert env["PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET"]["valueFrom"]["secretKeyRef"]["key"] == "MCP_CLIENT_SECRET"
    assert "--expected-auth-mode" in container["args"]
    assert "mcp_oauth" in container["args"]
    assert container["args"].count("--expected-scope") == 2
    assert _arg_value(container["args"], "--request-timeout") == "60"


def test_runtime_workers_and_smoke_use_ordered_dependency_gates() -> None:
    manifests = _render_chart("valkey.sentinel.enabled=true")
    worker_settings = {
        "palaceoftruth-worker": "app.workers.worker.WorkerSettings",
        "palaceoftruth-media-worker": "app.workers.worker.MediaWorkerSettings",
        "palaceoftruth-palace-worker": "app.workers.worker.PalaceWorkerSettings",
    }
    for name, settings_path in worker_settings.items():
        container = _deployment_by_name(manifests, name)["spec"]["template"]["spec"]["containers"][0]
        command = container["command"]
        assert command[:3] == ["python", "scripts/wait_for_worker_dependencies.py", "--"]
        assert container["readinessProbe"] == {
            "exec": {"command": ["arq", settings_path, "--check"]},
            "initialDelaySeconds": 5,
            "periodSeconds": 10,
            "timeoutSeconds": 5,
            "failureThreshold": 3,
        }
        assert "livenessProbe" not in container

    job = _manifest_by_kind_name_prefix(manifests, "Job", "palaceoftruth-memory-smoke-")
    annotations = job["metadata"]["annotations"]
    assert annotations["helm.sh/hook"] == "post-install,post-upgrade"
    assert annotations["helm.sh/hook-weight"] == "10"
    assert annotations["helm.sh/hook-delete-policy"] == "before-hook-creation,hook-succeeded"
    assert job["spec"]["activeDeadlineSeconds"] == 600
    args = job["spec"]["template"]["spec"]["containers"][0]["args"]
    assert _arg_value(args, "--dependency-timeout-seconds") == "300"
    assert _arg_value(args, "--dependency-interval-seconds") == "5"
    assert _arg_value(args, "--log-tail-lines") == "3000"


def test_palace_sarvent_oauth_staging_values_keep_fallback_and_verify_oauth_identity() -> None:
    manifests = _render_chart(
        "valkey.sentinel.enabled=true",
        "externalSecrets.enabled=true",
        "externalSecrets.secretStoreName=bitwarden-fields",
        "externalSecrets.appSecretItemId=app-secret-item",
        "externalSecrets.registrySecretItemId=registry-secret-item",
        "externalSecrets.mcpOauthClientSecretProperty=mcp-oauth-client-secret",
        "mcp.apiBaseUrl=https://api.palace.sarvent.cloud",
        "mcp.legacyApiKeyAuthEnabled=true",
        "mcp.oauthClientSecretKey=MCP_CLIENT_SECRET",
        "mcp.oauthTokenUrl=https://api.palace.sarvent.cloud/api/v1/memory/mcp/oauth/token",
        "mcp.oauthResource=https://api.palace.sarvent.cloud/api/v1",
        "mcp.oauthAudience=https://api.palace.sarvent.cloud/api/v1",
        "memoryRolloutSmoke.expectedAuthMode=mcp_oauth",
        "memoryRolloutSmoke.expectedTenantId=default",
        "memoryRolloutSmoke.expectedClientKey=helm-mcp",
        "memoryRolloutSmoke.expectedScopes[0]=read",
        "memoryRolloutSmoke.expectedScopes[1]=write",
        "memoryRolloutSmoke.requestTimeoutSeconds=60",
    )
    deployment = _deployment_by_name(manifests, "palaceoftruth-mcp")
    deployment_env = _container_env(deployment["spec"]["template"]["spec"]["containers"][0])
    job = _manifest_by_kind_name_prefix(manifests, "Job", "palaceoftruth-memory-smoke-")
    job_container = job["spec"]["template"]["spec"]["containers"][0]
    job_env = _container_env(job_container)
    external_secret = _manifest_by_kind_name(manifests, "ExternalSecret", "palaceoftruth-app-secrets")

    assert deployment_env["PALACEOFTRUTH_API_KEY"]["valueFrom"]["secretKeyRef"]["key"] == "API_KEY"
    assert deployment_env["PALACEOFTRUTH_API_BASE_URL"]["value"] == "https://api.palace.sarvent.cloud"
    assert deployment_env["PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET"]["valueFrom"]["secretKeyRef"]["key"] == "MCP_CLIENT_SECRET"
    assert deployment_env["PALACEOFTRUTH_MCP_OAUTH_TOKEN_URL"]["value"].endswith("/api/v1/memory/mcp/oauth/token")
    assert deployment_env["PALACEOFTRUTH_MCP_OAUTH_RESOURCE"]["value"] == "https://api.palace.sarvent.cloud/api/v1"
    assert deployment_env["PALACEOFTRUTH_MCP_OAUTH_AUDIENCE"]["value"] == "https://api.palace.sarvent.cloud/api/v1"

    assert job_env["PALACEOFTRUTH_API_KEY"]["valueFrom"]["secretKeyRef"]["key"] == "API_KEY"
    assert job_env["PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET"]["valueFrom"]["secretKeyRef"]["key"] == "MCP_CLIENT_SECRET"
    assert "--expected-auth-mode" in job_container["args"]
    assert "mcp_oauth" in job_container["args"]
    assert "--expected-tenant-id" in job_container["args"]
    assert "default" in job_container["args"]
    assert "--expected-client-key" in job_container["args"]
    assert "helm-mcp" in job_container["args"]
    assert job_container["args"].count("--expected-scope") == 2
    assert _arg_value(job_container["args"], "--request-timeout") == "60"

    assert {
        "secretKey": "MCP_CLIENT_SECRET",
        "remoteRef": {"key": "app-secret-item", "property": "mcp-oauth-client-secret"},
    } in external_secret["spec"]["data"]


def test_mcp_oauth_external_secret_uses_configured_secret_key() -> None:
    manifests = _render_chart(
        "externalSecrets.enabled=true",
        "externalSecrets.secretStoreName=bitwarden-fields",
        "externalSecrets.appSecretItemId=app-secret-item",
        "externalSecrets.registrySecretItemId=registry-secret-item",
        "externalSecrets.mcpOauthClientSecretProperty=mcp-oauth-client-secret",
        "mcp.oauthClientSecretKey=CUSTOM_MCP_OAUTH_SECRET",
    )
    deployment = _deployment_by_name(manifests, "palaceoftruth-mcp")
    deployment_env = _container_env(deployment["spec"]["template"]["spec"]["containers"][0])
    external_secret = _manifest_by_kind_name(manifests, "ExternalSecret", "palaceoftruth-app-secrets")

    assert (
        deployment_env["PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET"]["valueFrom"]["secretKeyRef"]["key"]
        == "CUSTOM_MCP_OAUTH_SECRET"
    )
    assert {
        "secretKey": "CUSTOM_MCP_OAUTH_SECRET",
        "remoteRef": {"key": "app-secret-item", "property": "mcp-oauth-client-secret"},
    } in external_secret["spec"]["data"]


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

    postgres_cluster = _manifest_by_kind_name(manifests, "Cluster", "palaceoftruth-postgres")
    assert postgres_cluster["spec"]["postgresql"]["parameters"] == {"shared_buffers": "128MB"}
    assert "resources" not in postgres_cluster["spec"]
    assert not any(manifest.get("kind") == "PodMonitor" for manifest in manifests)


def test_postgres_resources_render_only_when_configured() -> None:
    manifests = _render_chart(
        "postgres.resources.requests.cpu=250m",
        "postgres.resources.requests.memory=2Gi",
        "postgres.resources.limits.cpu=2",
        "postgres.resources.limits.memory=4Gi",
    )

    postgres_cluster = _manifest_by_kind_name(manifests, "Cluster", "palaceoftruth-postgres")
    assert postgres_cluster["spec"]["resources"] == {
        "requests": {"cpu": "250m", "memory": "2Gi"},
        "limits": {"cpu": 2, "memory": "4Gi"},
    }


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
            "honorLabels": True,
            "interval": "15s",
            "scrapeTimeout": "5s",
        }
    ]


def test_valkey_exporter_is_disabled_by_default() -> None:
    manifests = _render_chart()
    deployment = _deployment_by_name(manifests, "palaceoftruth-valkey")
    service = _manifest_by_kind_name(manifests, "Service", "palaceoftruth-valkey")

    assert [container["name"] for container in deployment["spec"]["template"]["spec"]["containers"]] == [
        "valkey"
    ]
    assert [port["name"] for port in service["spec"]["ports"]] == ["redis"]
    assert "palaceoftruth.io/valkey-metrics" not in service["metadata"]["labels"]
    assert not any(
        manifest.get("kind") == "ServiceMonitor"
        and manifest.get("metadata", {}).get("name") == "palaceoftruth-valkey"
        for manifest in manifests
    )
    assert not any(
        manifest.get("kind") == "PrometheusRule"
        and manifest.get("metadata", {}).get("name") == "palaceoftruth-valkey"
        for manifest in manifests
    )


def test_standalone_valkey_exporter_renders_hardened_sidecar_and_metrics_service() -> None:
    manifests = _render_chart("valkey.metrics.enabled=true")
    deployment = _deployment_by_name(manifests, "palaceoftruth-valkey")
    service = _manifest_by_kind_name(manifests, "Service", "palaceoftruth-valkey")
    containers = deployment["spec"]["template"]["spec"]["containers"]
    exporter = next(container for container in containers if container["name"] == "valkey-exporter")
    env = _container_env(exporter)

    assert exporter["image"] == "oliver006/redis_exporter:v1.77.0"
    assert env == {"REDIS_ADDR": {"name": "REDIS_ADDR", "value": "redis://127.0.0.1:6379"}}
    assert exporter["ports"] == [{"name": "metrics", "containerPort": 9121, "protocol": "TCP"}]
    assert exporter["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "readOnlyRootFilesystem": True,
        "runAsNonRoot": True,
    }
    assert exporter["resources"]["requests"] == {"memory": "16Mi", "cpu": "10m"}
    assert exporter["resources"]["limits"] == {"memory": "64Mi", "cpu": "100m"}
    assert "volumeMounts" not in exporter
    assert service["metadata"]["labels"]["palaceoftruth.io/valkey-metrics"] == "true"
    assert service["spec"]["ports"][-1] == {
        "name": "metrics",
        "port": 9121,
        "targetPort": "metrics",
        "protocol": "TCP",
    }


def test_sentinel_valkey_exporters_and_bounded_servicemonitor_render_when_enabled() -> None:
    manifests = _render_chart(
        "valkey.sentinel.enabled=true",
        "valkey.metrics.enabled=true",
        "valkey.metrics.serviceMonitor.enabled=true",
        "valkey.metrics.serviceMonitor.labels.release=kube-prometheus",
        "valkey.metrics.serviceMonitor.interval=15s",
        "valkey.metrics.serviceMonitor.scrapeTimeout=5s",
    )
    workload_targets = {
        ("StatefulSet", "palaceoftruth-valkey-primary"): "redis://127.0.0.1:6379",
        ("StatefulSet", "palaceoftruth-valkey-replica"): "redis://127.0.0.1:6379",
        ("Deployment", "palaceoftruth-valkey-sentinel"): "redis://127.0.0.1:26379",
    }
    for (kind, name), expected_target in workload_targets.items():
        workload = _manifest_by_kind_name(manifests, kind, name)
        exporter = next(
            container
            for container in workload["spec"]["template"]["spec"]["containers"]
            if container["name"] == "valkey-exporter"
        )
        assert _container_env(exporter)["REDIS_ADDR"]["value"] == expected_target
        assert "args" not in exporter

    for service_name in (
        "palaceoftruth-valkey-primary",
        "palaceoftruth-valkey-replica",
        "palaceoftruth-valkey-sentinel",
    ):
        service = _manifest_by_kind_name(manifests, "Service", service_name)
        assert service["metadata"]["labels"]["palaceoftruth.io/valkey-metrics"] == "true"
        assert service["spec"]["ports"][-1]["name"] == "metrics"

    service_monitor = _manifest_by_kind_name(manifests, "ServiceMonitor", "palaceoftruth-valkey")
    assert service_monitor["metadata"]["labels"]["release"] == "kube-prometheus"
    assert service_monitor["spec"]["selector"]["matchLabels"] == {
        "app.kubernetes.io/instance": "palaceoftruth",
        "palaceoftruth.io/valkey-metrics": "true"
    }
    assert service_monitor["spec"]["endpoints"] == [
        {"port": "metrics", "path": "/metrics", "interval": "15s", "scrapeTimeout": "5s"}
    ]


def test_valkey_servicemonitor_selector_isolates_releases_in_the_same_namespace() -> None:
    set_args = ("valkey.metrics.enabled=true", "valkey.metrics.serviceMonitor.enabled=true")
    alpha = _render_chart(*set_args, release_name="palace-alpha", namespace="shared-monitoring")
    beta = _render_chart(*set_args, release_name="palace-beta", namespace="shared-monitoring")

    alpha_monitor = _manifest_by_kind_name(alpha, "ServiceMonitor", "palace-alpha-palaceoftruth-valkey")
    beta_monitor = _manifest_by_kind_name(beta, "ServiceMonitor", "palace-beta-palaceoftruth-valkey")
    assert alpha_monitor["spec"]["selector"]["matchLabels"] == {
        "app.kubernetes.io/instance": "palace-alpha",
        "palaceoftruth.io/valkey-metrics": "true",
    }
    assert beta_monitor["spec"]["selector"]["matchLabels"] == {
        "app.kubernetes.io/instance": "palace-beta",
        "palaceoftruth.io/valkey-metrics": "true",
    }
    alpha_service = _manifest_by_kind_name(alpha, "Service", "palace-alpha-palaceoftruth-valkey")
    beta_service = _manifest_by_kind_name(beta, "Service", "palace-beta-palaceoftruth-valkey")
    assert alpha_service["metadata"]["labels"]["app.kubernetes.io/instance"] == "palace-alpha"
    assert beta_service["metadata"]["labels"]["app.kubernetes.io/instance"] == "palace-beta"


def test_valkey_prometheus_rules_are_observational_and_release_bounded() -> None:
    manifests = _render_chart(
        "valkey.sentinel.enabled=true",
        "valkey.metrics.enabled=true",
        "valkey.metrics.prometheusRule.enabled=true",
        "valkey.metrics.prometheusRule.labels.release=kube-prometheus",
        release_name="palace-alpha",
        namespace="palace-a",
    )
    prometheus_rule = _manifest_by_kind_name(manifests, "PrometheusRule", "palace-alpha-palaceoftruth-valkey")
    rules = prometheus_rule["spec"]["groups"][0]["rules"]
    rules_by_name = {rule["alert"]: rule for rule in rules}

    assert prometheus_rule["metadata"]["labels"]["release"] == "kube-prometheus"
    assert set(rules_by_name) == {
        "PalaceValkeyExporterTargetsAbsent",
        "PalaceValkeyExporterScrapeDown",
        "PalaceValkeyInstanceDown",
        "PalaceValkeySentinelCkquorumFailed",
    }
    for rule in rules:
        expression = rule["expr"]
        assert 'namespace="palace-a"' in expression
        assert "palace-alpha-palaceoftruth-valkey" in expression
        assert "SENTINEL RESET" not in expression
        assert "failover" not in expression.lower()
    assert "absent(up{" in rules_by_name["PalaceValkeyExporterTargetsAbsent"]["expr"]
    assert "max_over_time(up{" in rules_by_name["PalaceValkeyExporterScrapeDown"]["expr"]
    assert "redis_up{" in rules_by_name["PalaceValkeyInstanceDown"]["expr"]
    assert (
        "redis_sentinel_master_ckquorum_status{"
        in rules_by_name["PalaceValkeySentinelCkquorumFailed"]["expr"]
    )


def test_documented_password_map_keys_match_rendered_exporter_targets() -> None:
    manifests = _render_chart("valkey.sentinel.enabled=true", "valkey.metrics.enabled=true")
    rendered_targets = {
        _container_env(container)["REDIS_ADDR"]["value"]
        for manifest in manifests
        if manifest.get("kind") in {"Deployment", "StatefulSet"}
        for container in manifest.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        if container.get("name") == "valkey-exporter"
    }
    monitoring_docs = (REPO_ROOT / "docs" / "monitoring" / "grafana" / "README.md").read_text()
    password_map_block = monitoring_docs.split("```json", 1)[1].split("```", 1)[0]
    documented_password_map = json.loads(password_map_block)

    assert rendered_targets == {"redis://127.0.0.1:6379", "redis://127.0.0.1:26379"}
    assert set(documented_password_map) == rendered_targets
    assert all(value.startswith("<") and value.endswith(">") for value in documented_password_map.values())


def test_valkey_exporter_reads_optional_password_from_existing_secret_file() -> None:
    manifests = _render_chart(
        "valkey.metrics.enabled=true",
        "valkey.metrics.existingSecret=valkey-auth",
        "valkey.metrics.passwordFileKey=exporter-password-map",
    )
    deployment = _deployment_by_name(manifests, "palaceoftruth-valkey")
    pod_spec = deployment["spec"]["template"]["spec"]
    exporter = next(container for container in pod_spec["containers"] if container["name"] == "valkey-exporter")
    env = _container_env(exporter)

    assert env["REDIS_PASSWORD_FILE"]["value"] == "/run/secrets/valkey-exporter/password.json"
    assert "REDIS_PASSWORD" not in env
    assert exporter["volumeMounts"] == [
        {
            "name": "valkey-exporter-password",
            "mountPath": "/run/secrets/valkey-exporter",
            "readOnly": True,
        }
    ]
    password_volume = next(volume for volume in pod_spec["volumes"] if volume["name"] == "valkey-exporter-password")
    assert password_volume["secret"] == {
        "secretName": "valkey-auth",
        "items": [{"key": "exporter-password-map", "path": "password.json"}],
    }


@pytest.mark.parametrize(
    "partial_config",
    ["valkey.metrics.existingSecret=valkey-auth", "valkey.metrics.passwordFileKey=exporter-password-map"],
)
def test_valkey_exporter_rejects_partial_password_file_configuration(partial_config: str) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        _render_chart("valkey.metrics.enabled=true", partial_config)


def test_postgres_podmonitor_and_query_statistics_parameter_render_when_enabled() -> None:
    manifests = _render_chart(
        "postgres.parameters.pg_stat_statements\\.track=top",
        "postgres.monitoring.podMonitor.enabled=true",
        "postgres.monitoring.podMonitor.labels.release=kube-prometheus",
        "postgres.monitoring.podMonitor.interval=15s",
        "postgres.monitoring.podMonitor.scrapeTimeout=5s",
    )

    cluster = _manifest_by_kind_name(manifests, "Cluster", "palaceoftruth-postgres")
    pod_monitor = _manifest_by_kind_name(manifests, "PodMonitor", "palaceoftruth-postgres")

    assert cluster["spec"]["postgresql"]["parameters"] == {
        "pg_stat_statements.track": "top",
        "shared_buffers": "128MB",
    }
    assert pod_monitor["metadata"]["labels"]["release"] == "kube-prometheus"
    assert pod_monitor["spec"] == {
        "selector": {"matchLabels": {"cnpg.io/cluster": "palaceoftruth-postgres"}},
        "podMetricsEndpoints": [
            {
                "port": "metrics",
                "path": "/metrics",
                "interval": "15s",
                "scrapeTimeout": "5s",
            }
        ],
    }


def test_postgres_query_statistics_configmap_is_opt_in_and_safe() -> None:
    manifests = _render_chart(
        "postgres.monitoring.customQueries.enabled=true",
        "postgres.parameters.pg_stat_statements\\.track=top",
    )

    cluster = _manifest_by_kind_name(manifests, "Cluster", "palaceoftruth-postgres")
    config_map = _manifest_by_kind_name(manifests, "ConfigMap", "palaceoftruth-postgres-monitoring")

    assert cluster["spec"]["monitoring"] == {
        "customQueriesConfigMap": [
            {"name": "palaceoftruth-postgres-monitoring", "key": "custom-queries"},
        ],
    }
    assert config_map["metadata"]["labels"]["cnpg.io/reload"] == ""
    queries = config_map["data"]["custom-queries"]
    assert "public.pg_stat_statements" in queries
    assert "queryid" not in queries
    assert "query_text" not in queries
    assert "query_family" in queries
    assert "bounded_hybrid" in queries
    assert "legacy_hybrid" in queries
    assert "pg_catalog.pg_locks" in queries


def test_postgres_query_statistics_requires_pg_stat_statements() -> None:
    with pytest.raises(subprocess.CalledProcessError):
        _render_chart("postgres.monitoring.customQueries.enabled=true")


def test_postgres_prometheus_rules_are_observational_and_namespace_bounded() -> None:
    manifests = _render_chart(
        "postgres.monitoring.prometheusRule.enabled=true",
        "postgres.monitoring.prometheusRule.labels.release=monitoring-kube-prometheus",
        release_name="palace-alpha",
        namespace="palace-a",
    )

    prometheus_rule = _manifest_by_kind_name(manifests, "PrometheusRule", "palace-alpha-palaceoftruth-postgres")
    rules = prometheus_rule["spec"]["groups"][0]["rules"]
    rules_by_name = {rule["alert"]: rule for rule in rules}

    assert prometheus_rule["metadata"]["labels"]["release"] == "monitoring-kube-prometheus"
    assert set(rules_by_name) == {
        "PalaceCNPGMetricsAbsent",
        "PalaceCNPGMetricsScrapeDown",
        "PalaceCNPGRetrievalTempIoHigh",
        "PalaceCNPGLockWaits",
        "PalaceCNPGReplicationLag",
    }
    for rule in rules:
        assert 'namespace="palace-a"' in rule["expr"]
        assert "reload" not in rule["expr"].lower()
        assert "failover" not in rule["expr"].lower()
    assert "cnpg_pg_replication_lag" in rules_by_name["PalaceCNPGReplicationLag"]["expr"]


def test_valkey_primary_replica_required_anti_affinity_is_an_explicit_opt_in() -> None:
    default_manifests = _render_chart("valkey.sentinel.enabled=true")
    default_primary = _manifest_by_kind_name(default_manifests, "StatefulSet", "palaceoftruth-valkey-primary")
    default_replica = _manifest_by_kind_name(default_manifests, "StatefulSet", "palaceoftruth-valkey-replica")

    assert "affinity" not in default_primary["spec"]["template"]["spec"]
    assert "affinity" not in default_replica["spec"]["template"]["spec"]

    manifests = _render_chart(
        "valkey.sentinel.enabled=true",
        "valkey.sentinel.primaryReplicaAntiAffinity.enabled=true",
    )
    primary = _manifest_by_kind_name(manifests, "StatefulSet", "palaceoftruth-valkey-primary")
    replica = _manifest_by_kind_name(manifests, "StatefulSet", "palaceoftruth-valkey-replica")

    assert primary["spec"]["template"]["spec"]["affinity"] == {
        "podAntiAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": [
                {
                    "topologyKey": "kubernetes.io/hostname",
                    "labelSelector": {"matchLabels": {"app": "palaceoftruth-valkey-replica"}},
                }
            ]
        }
    }
    assert replica["spec"]["template"]["spec"]["affinity"] == {
        "podAntiAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": [
                {
                    "topologyKey": "kubernetes.io/hostname",
                    "labelSelector": {"matchLabels": {"app": "palaceoftruth-valkey-primary"}},
                }
            ]
        }
    }
