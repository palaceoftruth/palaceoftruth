from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import check_memory_rollout_smoke as rollout_smoke


def test_script_path_execution_can_resolve_sibling_script_imports() -> None:
    backend_dir = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [sys.executable, "scripts/check_memory_rollout_smoke.py", "--help"],
        cwd=backend_dir,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def _http_client(**overrides: Any) -> rollout_smoke.HttpClient:
    options = {
        "base_url": "https://api.example/api/v1",
        "api_key": None,
        "bearer_token": None,
        "oauth_client_secret": None,
        "oauth_token_url": None,
        "oauth_resource": None,
        "client_key": "rollout-smoke",
        "client_scopes": ["read", "write", "write:workspace"],
        "timeout": 12,
    }
    options.update(overrides)
    return rollout_smoke.HttpClient(**options)


def test_http_client_sends_static_api_key_and_mcp_scope_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"ok": true}'

    def fake_urlopen(request, *, timeout: float):
        captured["headers"] = dict(request.header_items())
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(rollout_smoke.urllib.request, "urlopen", fake_urlopen)
    client = _http_client(api_key="static-key")

    result = client.request("POST", "/memory/entries", body={"scope": {"type": "workspace", "key": "rollout-smoke"}})

    assert result.status == 200
    assert captured["headers"]["X-api-key"] == "static-key"
    assert captured["headers"]["X-mcp-scope"] == "write"
    assert captured["headers"]["X-mcp-scopes"] == "write,write:workspace"
    assert "Authorization" not in captured["headers"]
    assert captured["timeout"] == 12


def test_http_client_sends_static_bearer_without_api_key_scope_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"ok": true}'

    def fake_urlopen(request, *, timeout: float):
        captured["headers"] = dict(request.header_items())
        return FakeResponse()

    monkeypatch.setattr(rollout_smoke.urllib.request, "urlopen", fake_urlopen)
    client = _http_client(api_key="legacy-api-key", bearer_token="bearer-token")

    result = client.request("GET", "/memory/whoami")

    assert result.status == 200
    assert captured["headers"]["Authorization"] == "Bearer bearer-token"
    assert "X-api-key" not in captured["headers"]
    assert "X-mcp-scopes" not in captured["headers"]


def test_http_client_mints_oauth_token_with_configured_resource(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, str, str | None]] = []

    class FakeResponse:
        status = 200

        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def read(self) -> bytes:
            return self.payload

    def fake_urlopen(request, *, timeout: float):
        body = request.data.decode("utf-8") if request.data else None
        seen.append((request.full_url, request.get_method(), body))
        if request.full_url.endswith("/oauth/token"):
            return FakeResponse(b'{"access_token": "minted-token", "expires_in": 3600}')
        return FakeResponse(b'{"ok": true}')

    monkeypatch.setattr(rollout_smoke.urllib.request, "urlopen", fake_urlopen)
    client = _http_client(
        api_key="legacy-api-key",
        oauth_client_secret="client-secret",
        oauth_token_url="https://api.example/api/v1/memory/mcp/oauth/token",
        oauth_resource="https://api.example/api/v1",
        client_key="helm-mcp",
    )

    result = client.request("GET", "/memory/whoami")

    assert result.status == 200
    assert seen[0][0] == "https://api.example/api/v1/memory/mcp/oauth/token"
    assert "client_id=helm-mcp" in (seen[0][2] or "")
    assert "resource=https%3A%2F%2Fapi.example%2Fapi%2Fv1" in (seen[0][2] or "")


def test_http_client_defaults_oauth_resource_to_backend_api(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, str, str | None]] = []

    class FakeResponse:
        status = 200

        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def read(self) -> bytes:
            return self.payload

    def fake_urlopen(request, *, timeout: float):
        body = request.data.decode("utf-8") if request.data else None
        seen.append((request.full_url, request.get_method(), body))
        if request.full_url.endswith("/oauth/token"):
            return FakeResponse(b'{"access_token": "minted-token", "expires_in": 3600}')
        return FakeResponse(b'{"ok": true}')

    monkeypatch.setattr(rollout_smoke.urllib.request, "urlopen", fake_urlopen)
    client = _http_client(
        oauth_client_secret="client-secret",
        oauth_token_url="https://api.example/api/v1/memory/mcp/oauth/token",
        client_key="helm-mcp",
    )

    result = client.request("GET", "/memory/whoami")

    assert result.status == 200
    assert "resource=https%3A%2F%2Fapi.example%2Fapi%2Fv1" in (seen[0][2] or "")


def test_http_client_ignores_legacy_mcp_oauth_resource_for_backend_api(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, str, str | None]] = []

    class FakeResponse:
        status = 200

        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def read(self) -> bytes:
            return self.payload

    def fake_urlopen(request, *, timeout: float):
        body = request.data.decode("utf-8") if request.data else None
        seen.append((request.full_url, request.get_method(), body))
        if request.full_url.endswith("/oauth/token"):
            return FakeResponse(b'{"access_token": "minted-token", "expires_in": 3600}')
        return FakeResponse(b'{"ok": true}')

    monkeypatch.setattr(rollout_smoke.urllib.request, "urlopen", fake_urlopen)
    client = _http_client(
        oauth_client_secret="client-secret",
        oauth_token_url="https://api.example/api/v1/memory/mcp/oauth/token",
        oauth_resource="https://mcp.example/mcp",
        client_key="helm-mcp",
    )

    result = client.request("GET", "/memory/whoami")

    assert result.status == 200
    assert "resource=https%3A%2F%2Fapi.example%2Fapi%2Fv1" in (seen[0][2] or "")
    assert "resource=https%3A%2F%2Fmcp.example%2Fmcp" not in (seen[0][2] or "")


class FakeHttpClient:
    def __init__(self, responses: dict[tuple[str, str], Any]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, str, dict[str, Any] | None, dict[str, Any] | None]] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> rollout_smoke.HttpResult:
        self.requests.append((method, path, body, query))
        response = self.responses[(method, path)]
        if isinstance(response, rollout_smoke.HttpResult):
            return response
        return rollout_smoke.HttpResult(200, response)


def test_memory_write_smoke_reports_completed_job() -> None:
    client = FakeHttpClient(
        {
            ("GET", "/memory/whoami"): {"tenant_id": "tenant-a"},
            ("POST", "/memory/entries"): {
                "job_id": "job-1",
                "status": "queued",
                "contract_status": "queued",
            },
            ("GET", "/memory/jobs/job-1"): {"job_id": "job-1", "status": "completed"},
            ("GET", "/memory/jobs"): {"jobs": [{"job_id": "job-1", "status": "completed"}]},
        }
    )
    report = {"target": "example-staging", "checks": [], "alerts": []}
    args = SimpleNamespace(
        target_name="example-staging",
        run_id="20260527T180000Z",
        scope_key="rollout-smoke",
        job_timeout_seconds=0.1,
        job_interval_seconds=0,
        job_list_limit=10,
    )

    rollout_smoke.check_memory_write(client, report, args)

    assert report["tenant_id"] == "tenant-a"
    assert report["alerts"] == []
    assert {check["name"]: check["status"] for check in report["checks"]} == {
        "tenant_identity": "passed",
        "memory_write": "passed",
        "memory_job_completion": "passed",
        "memory_jobs_listing": "passed",
    }
    write_request = next(request for request in client.requests if request[0:2] == ("POST", "/memory/entries"))
    assert write_request[2]["metadata"]["target"] == "example-staging"
    assert write_request[2]["idempotency_key"] == "palace-rollout-smoke:example-staging:20260527T180000Z"


def test_resolve_tenant_identity_checks_expected_oauth_identity() -> None:
    client = FakeHttpClient(
        {
            ("GET", "/memory/whoami"): {
                "tenant_id": "tenant-a",
                "auth_mode": "mcp_oauth",
                "mcp_client_key": "helm-mcp",
                "allowed_scopes": ["read", "write"],
            }
        }
    )
    report = {"target": "palaceoftruth", "checks": [], "alerts": []}
    args = SimpleNamespace(
        expected_tenant_id="tenant-a",
        expected_auth_mode="mcp_oauth",
        expected_client_key="helm-mcp",
        expected_scope=["read", "write"],
    )

    tenant_id = rollout_smoke.resolve_tenant_identity(client, report, args)

    assert tenant_id == "tenant-a"
    assert report["alerts"] == []
    assert {check["name"]: check["status"] for check in report["checks"]} == {
        "tenant_identity_expectations": "passed",
        "tenant_identity": "passed",
    }


def test_memory_write_smoke_alerts_when_accepted_job_never_completes() -> None:
    client = FakeHttpClient(
        {
            ("GET", "/memory/whoami"): {"tenant_id": "tenant-a"},
            ("POST", "/memory/entries"): {"job_id": "job-1", "status": "queued"},
            ("GET", "/memory/jobs/job-1"): {"job_id": "job-1", "status": "queued"},
            ("GET", "/memory/jobs"): {"jobs": [{"job_id": "job-1", "status": "queued"}]},
        }
    )
    report = {"target": "palaceoftruth", "checks": [], "alerts": []}
    args = SimpleNamespace(
        target_name="palaceoftruth",
        run_id="20260527T180000Z",
        scope_key="rollout-smoke",
        job_timeout_seconds=0,
        job_interval_seconds=0,
        job_list_limit=10,
    )

    rollout_smoke.check_memory_write(client, report, args)

    assert report["alerts"][0]["code"] == "accepted_but_not_completed"
    assert report["alerts"][0]["tenant_id"] == "tenant-a"
    assert report["alerts"][0]["target"] == "palaceoftruth"


def test_kubernetes_check_alerts_on_restart_and_master_not_found() -> None:
    class FakeKube:
        namespace = "example-staging"

        def get(self, path: str, *, query: dict[str, Any] | None = None) -> dict[str, Any]:
            assert path == "/api/v1/namespaces/example-staging/pods"
            assert query == {"labelSelector": "app.kubernetes.io/instance=palaceoftruth-hermes"}
            return {
                "items": [
                    {
                        "metadata": {
                            "name": "palace-worker-abc",
                            "labels": {"app": "palaceoftruth-palace-worker"},
                        },
                        "spec": {"containers": [{"name": "palace-worker"}]},
                        "status": {
                            "containerStatuses": [
                                {"name": "palace-worker", "restartCount": 4},
                            ]
                        },
                    }
                ]
            }

        def get_text(self, path: str, *, query: dict[str, Any] | None = None) -> str:
            assert path == "/api/v1/namespaces/example-staging/pods/palace-worker-abc/log"
            return "redis.exceptions.MasterNotFoundError: No master found for 'mymaster'"

    report = {"target": "example-staging", "tenant_id": "tenant-a", "checks": [], "alerts": []}
    args = SimpleNamespace(
        skip_kubernetes=False,
        namespace="example-staging",
        pod_label_selector="app.kubernetes.io/instance=palaceoftruth-hermes",
        worker_name_fragment="worker",
        restart_alert_threshold=3,
        skip_log_scan=False,
        log_since_seconds=3600,
        log_tail_lines=500,
        request_timeout=5,
    )

    rollout_smoke.check_kubernetes(report, args, kube=FakeKube())

    assert [alert["code"] for alert in report["alerts"]] == ["worker_restart_spike", "master_not_found"]
    assert report["checks"][0]["name"] == "kubernetes_alerts"
    assert report["checks"][0]["status"] == "failed"


def test_kubernetes_check_allows_resolved_sentinel_startup_retries() -> None:
    class FakeKube:
        namespace = "palaceoftruth"

        def get(self, path: str, *, query: dict[str, Any] | None = None) -> dict[str, Any]:
            return {
                "items": [
                    {
                        "metadata": {
                            "name": "worker-abc",
                            "labels": {"app": "palaceoftruth-worker"},
                        },
                        "spec": {"containers": [{"name": "worker"}]},
                        "status": {"containerStatuses": [{"name": "worker", "restartCount": 0}]},
                    }
                ]
            }

        def get_text(self, path: str, *, query: dict[str, Any] | None = None) -> str:
            return (
                "Waiting for Redis Sentinel master discovery before ARQ startup: "
                "error=redis.exceptions.MasterNotFoundError: No master found for 'mymaster'\n"
                "Redis Sentinel startup dependency ready: master=10.42.5.211:6379"
            )

    report = {"target": "palaceoftruth", "tenant_id": "tenant-a", "checks": [], "alerts": []}
    args = SimpleNamespace(
        skip_kubernetes=False,
        namespace="palaceoftruth",
        pod_label_selector="app.kubernetes.io/instance=palaceoftruth",
        worker_name_fragment="worker",
        restart_alert_threshold=3,
        skip_log_scan=False,
        log_since_seconds=3600,
        log_tail_lines=500,
        request_timeout=5,
    )

    rollout_smoke.check_kubernetes(report, args, kube=FakeKube())

    assert report["alerts"] == []
    assert report["checks"][0]["status"] == "passed"


def test_kubernetes_check_ignores_terminating_worker_startup_retries() -> None:
    class FakeKube:
        namespace = "palaceoftruth"

        def get(self, path: str, *, query: dict[str, Any] | None = None) -> dict[str, Any]:
            return {
                "items": [
                    {
                        "metadata": {
                            "name": "worker-old",
                            "labels": {"app": "palaceoftruth-worker"},
                            "deletionTimestamp": "2026-07-08T00:26:00Z",
                        },
                        "spec": {"containers": [{"name": "worker"}]},
                        "status": {
                            "phase": "Running",
                            "containerStatuses": [{"name": "worker", "restartCount": 0}],
                        },
                    },
                    {
                        "metadata": {
                            "name": "worker-new",
                            "labels": {"app": "palaceoftruth-worker"},
                        },
                        "spec": {"containers": [{"name": "worker"}]},
                        "status": {
                            "phase": "Running",
                            "containerStatuses": [{"name": "worker", "restartCount": 0}],
                        },
                    },
                ]
            }

        def get_text(self, path: str, *, query: dict[str, Any] | None = None) -> str:
            if "worker-old" in path:
                return (
                    "Waiting for Redis Sentinel master discovery before ARQ startup: "
                    "error=redis.exceptions.MasterNotFoundError: No master found for 'mymaster'"
                )
            return "Redis Sentinel startup dependency ready: master=10.42.5.211:6379"

    report = {"target": "palaceoftruth", "tenant_id": "tenant-a", "checks": [], "alerts": []}
    args = SimpleNamespace(
        skip_kubernetes=False,
        namespace="palaceoftruth",
        pod_label_selector="app.kubernetes.io/instance=palaceoftruth",
        worker_name_fragment="worker",
        restart_alert_threshold=3,
        skip_log_scan=False,
        log_since_seconds=3600,
        log_tail_lines=500,
        request_timeout=5,
    )

    rollout_smoke.check_kubernetes(report, args, kube=FakeKube())

    assert report["alerts"] == []
    assert report["checks"][0]["status"] == "passed"
    assert report["checks"][0]["selected_worker_pods"] == 1


def test_kubernetes_check_fails_on_active_non_running_worker() -> None:
    class FakeKube:
        namespace = "palaceoftruth"

        def get(self, path: str, *, query: dict[str, Any] | None = None) -> dict[str, Any]:
            return {
                "items": [
                    {
                        "metadata": {
                            "name": "worker-pending",
                            "labels": {"app": "palaceoftruth-worker"},
                        },
                        "spec": {"containers": [{"name": "worker"}]},
                        "status": {
                            "phase": "Pending",
                            "containerStatuses": [{"name": "worker", "restartCount": 0}],
                        },
                    },
                    {
                        "metadata": {
                            "name": "worker-running",
                            "labels": {"app": "palaceoftruth-worker"},
                        },
                        "spec": {"containers": [{"name": "worker"}]},
                        "status": {
                            "phase": "Running",
                            "containerStatuses": [{"name": "worker", "restartCount": 0}],
                        },
                    },
                ]
            }

        def get_text(self, path: str, *, query: dict[str, Any] | None = None) -> str:
            return "Redis Sentinel startup dependency ready: master=10.42.5.211:6379"

    report = {"target": "palaceoftruth", "tenant_id": "tenant-a", "checks": [], "alerts": []}
    args = SimpleNamespace(
        skip_kubernetes=False,
        namespace="palaceoftruth",
        pod_label_selector="app.kubernetes.io/instance=palaceoftruth",
        worker_name_fragment="worker",
        restart_alert_threshold=3,
        skip_log_scan=False,
        log_since_seconds=3600,
        log_tail_lines=500,
        request_timeout=5,
    )

    rollout_smoke.check_kubernetes(report, args, kube=FakeKube())

    assert [alert["code"] for alert in report["alerts"]] == ["worker_pod_not_running"]
    assert report["checks"][0]["status"] == "failed"
    assert report["checks"][0]["non_running_pods"] == [{"pod": "worker-pending", "phase": "Pending"}]


def test_kubernetes_check_alerts_when_master_not_found_after_startup_ready() -> None:
    class FakeKube:
        namespace = "palaceoftruth"

        def get(self, path: str, *, query: dict[str, Any] | None = None) -> dict[str, Any]:
            return {
                "items": [
                    {
                        "metadata": {
                            "name": "worker-abc",
                            "labels": {"app": "palaceoftruth-worker"},
                        },
                        "spec": {"containers": [{"name": "worker"}]},
                        "status": {"containerStatuses": [{"name": "worker", "restartCount": 0}]},
                    }
                ]
            }

        def get_text(self, path: str, *, query: dict[str, Any] | None = None) -> str:
            return (
                "Redis Sentinel startup dependency ready: master=10.42.5.211:6379\n"
                "redis.exceptions.MasterNotFoundError: No master found for 'mymaster'"
            )

    report = {"target": "palaceoftruth", "tenant_id": "tenant-a", "checks": [], "alerts": []}
    args = SimpleNamespace(
        skip_kubernetes=False,
        namespace="palaceoftruth",
        pod_label_selector="app.kubernetes.io/instance=palaceoftruth",
        worker_name_fragment="worker",
        restart_alert_threshold=3,
        skip_log_scan=False,
        log_since_seconds=3600,
        log_tail_lines=500,
        request_timeout=5,
    )

    rollout_smoke.check_kubernetes(report, args, kube=FakeKube())

    assert [alert["code"] for alert in report["alerts"]] == ["master_not_found"]
    assert report["checks"][0]["status"] == "failed"


def test_kubernetes_check_treats_log_marker_as_historical_after_post_scan_sentinel_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeKube:
        namespace = "palaceoftruth"

        def get(self, path: str, *, query: dict[str, Any] | None = None) -> dict[str, Any]:
            return {
                "items": [
                    {
                        "metadata": {
                            "name": "worker-abc",
                            "labels": {"app": "palaceoftruth-worker"},
                        },
                        "spec": {"containers": [{"name": "worker"}]},
                        "status": {
                            "phase": "Running",
                            "containerStatuses": [{"name": "worker", "restartCount": 0}],
                        },
                    }
                ]
            }

        def get_text(self, path: str, *, query: dict[str, Any] | None = None) -> str:
            return (
                "Redis Sentinel startup dependency ready: master=10.42.5.211:6379\n"
                "redis.exceptions.MasterNotFoundError: No master found for 'mymaster'"
            )

    report = {
        "target": "palaceoftruth",
        "tenant_id": "tenant-a",
        "checks": [
            {"name": "runtime_dependencies", "status": "passed"},
            {"name": "memory_job_completion", "status": "passed"},
            {"name": "sentinel_valkey", "status": "passed"},
        ],
        "alerts": [],
    }
    args = SimpleNamespace(
        skip_kubernetes=False,
        namespace="palaceoftruth",
        pod_label_selector="app.kubernetes.io/instance=palaceoftruth",
        worker_name_fragment="worker",
        restart_alert_threshold=3,
        skip_log_scan=False,
        log_since_seconds=3600,
        log_tail_lines=500,
        request_timeout=5,
    )
    async def healthy_sentinel():
        return SimpleNamespace(
            master_host="10.42.5.211",
            master_port=6379,
            connected_replicas=1,
        )

    monkeypatch.setattr(rollout_smoke, "check_rollout_gate", healthy_sentinel)

    rollout_smoke.check_kubernetes(report, args, kube=FakeKube())

    assert report["alerts"] == []
    kubernetes_check = report["checks"][-1]
    assert kubernetes_check["status"] == "passed"
    assert kubernetes_check["master_not_found_hits"] == []
    assert kubernetes_check["historical_master_not_found_hits"] == [
        {"pod": "worker-abc", "container": "worker"}
    ]
    assert kubernetes_check["post_scan_sentinel"]["healthy"] is True


def test_kubernetes_check_fails_closed_when_post_scan_sentinel_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeKube:
        namespace = "palaceoftruth"

        def get(self, path: str, *, query: dict[str, Any] | None = None) -> dict[str, Any]:
            return {
                "items": [
                    {
                        "metadata": {
                            "name": "worker-abc",
                            "labels": {"app": "palaceoftruth-worker"},
                        },
                        "spec": {"containers": [{"name": "worker"}]},
                        "status": {
                            "phase": "Running",
                            "containerStatuses": [{"name": "worker", "restartCount": 0}],
                        },
                    }
                ]
            }

        def get_text(self, path: str, *, query: dict[str, Any] | None = None) -> str:
            return (
                "Redis Sentinel startup dependency ready: master=10.42.5.211:6379\n"
                "redis.exceptions.MasterNotFoundError: No master found for 'mymaster'"
            )

    async def failed_sentinel():
        raise RuntimeError("No master found for 'mymaster'")

    monkeypatch.setattr(rollout_smoke, "check_rollout_gate", failed_sentinel)
    report = {
        "target": "palaceoftruth",
        "tenant_id": "tenant-a",
        "checks": [
            {"name": "runtime_dependencies", "status": "passed"},
            {"name": "memory_job_completion", "status": "passed"},
            {"name": "sentinel_valkey", "status": "passed"},
        ],
        "alerts": [],
    }
    args = SimpleNamespace(
        skip_kubernetes=False,
        namespace="palaceoftruth",
        pod_label_selector="app.kubernetes.io/instance=palaceoftruth",
        worker_name_fragment="worker",
        restart_alert_threshold=3,
        skip_log_scan=False,
        log_since_seconds=3600,
        log_tail_lines=500,
        request_timeout=5,
    )

    rollout_smoke.check_kubernetes(report, args, kube=FakeKube())

    assert [alert["code"] for alert in report["alerts"]] == ["master_not_found"]
    kubernetes_check = report["checks"][-1]
    assert kubernetes_check["status"] == "failed"
    assert kubernetes_check["post_scan_sentinel"] == {
        "healthy": False,
        "error_class": "RuntimeError",
        "error": "No master found for 'mymaster'",
    }


def test_kubernetes_check_fails_closed_when_worker_logs_cannot_be_read() -> None:
    class FakeKube:
        namespace = "palaceoftruth"

        def get(self, path: str, *, query: dict[str, Any] | None = None) -> dict[str, Any]:
            return {
                "items": [
                    {
                        "metadata": {
                            "name": "worker-abc",
                            "labels": {"app": "palaceoftruth-worker"},
                        },
                        "spec": {"containers": [{"name": "worker"}]},
                        "status": {"containerStatuses": [{"name": "worker", "restartCount": 0}]},
                    }
                ]
            }

        def get_text(self, path: str, *, query: dict[str, Any] | None = None) -> str:
            raise PermissionError("pods/log denied")

    report = {"target": "palaceoftruth", "tenant_id": "tenant-a", "checks": [], "alerts": []}
    args = SimpleNamespace(
        skip_kubernetes=False,
        namespace="palaceoftruth",
        pod_label_selector="app.kubernetes.io/instance=palaceoftruth",
        worker_name_fragment="worker",
        restart_alert_threshold=3,
        skip_log_scan=False,
        log_since_seconds=3600,
        log_tail_lines=500,
        request_timeout=5,
    )

    rollout_smoke.check_kubernetes(report, args, kube=FakeKube())

    assert report["alerts"][0]["code"] == "worker_log_scan_failed"
    assert report["alerts"][0]["tenant_id"] == "tenant-a"
    assert report["checks"][0]["status"] == "failed"
    assert report["checks"][0]["log_read_failures"] == [
        {"pod": "worker-abc", "container": "worker", "error_class": "PermissionError"}
    ]


def test_runtime_dependencies_require_api_readiness_and_started_workers() -> None:
    class FakeClient:
        def request(self, method: str, path: str, **_: Any) -> rollout_smoke.HttpResult:
            assert (method, path) == ("GET", "/ready")
            return rollout_smoke.HttpResult(
                200,
                {
                    "status": "ok",
                    "dependencies": {"database": {"status": "ok"}, "queue": {"status": "ok"}},
                },
            )

    class FakeKube:
        namespace = "palaceoftruth"

        def get(self, path: str, *, query: dict[str, Any] | None = None) -> dict[str, Any]:
            return {
                "items": [
                    {
                        "metadata": {"name": "worker-abc", "labels": {"app": "palaceoftruth-worker"}},
                        "spec": {"containers": [{"name": "worker"}]},
                        "status": {
                            "phase": "Running",
                            "conditions": [{"type": "Ready", "status": "True"}],
                            "containerStatuses": [{"name": "worker", "ready": True, "started": True}],
                        },
                    }
                ]
            }

        def get_text(self, path: str, *, query: dict[str, Any] | None = None) -> str:
            return "Redis Sentinel startup dependency ready: master=valkey:6379\nStarting worker for 1 functions"

    report = {"target": "palaceoftruth", "tenant_id": "tenant-a", "checks": [], "alerts": []}
    args = SimpleNamespace(
        namespace="palaceoftruth",
        pod_label_selector="app.kubernetes.io/instance=palaceoftruth",
        worker_name_fragment="worker",
        log_since_seconds=3600,
        log_tail_lines=500,
        request_timeout=5,
        dependency_timeout_seconds=1,
        dependency_interval_seconds=0.01,
    )

    rollout_smoke.check_runtime_dependencies(FakeClient(), report, args, kube=FakeKube())

    assert report["alerts"] == []
    assert report["checks"] == [
        {
            "name": "runtime_dependencies",
            "status": "passed",
            "api": {"ready": True, "http_status": 200, "status": "ok", "dependencies": {"database": "ok", "queue": "ok"}},
            "workers": {
                "ready": True,
                "selected_pods": ["worker-abc"],
                "non_running": [],
                "unready_pods": [],
                "missing_markers": [],
                "readiness_fallback": [],
                "unresolved_errors": [],
                "log_read_failures": [],
            },
        }
    ]


def _ready_sentinel_pods(count: int = 3) -> list[dict[str, Any]]:
    return [
        {
            "metadata": {
                "name": f"palaceoftruth-valkey-sentinel-{index}",
                "uid": f"sentinel-uid-{index}",
                "labels": {"app": "palaceoftruth-valkey-sentinel"},
            },
            "status": {
                "phase": "Running",
                "conditions": [{"type": "Ready", "status": "True"}],
                "containerStatuses": [{"name": "sentinel", "ready": True, "restartCount": 0}],
            },
        }
        for index in range(count)
    ]


def test_runtime_dependencies_reconcile_01529_rollout_transient_master_discovery_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def request(self, method: str, path: str, **_: Any) -> rollout_smoke.HttpResult:
            assert (method, path) == ("GET", "/ready")
            return rollout_smoke.HttpResult(
                200,
                {
                    "status": "ok",
                    "dependencies": {"database": {"status": "ok"}, "queue": {"status": "ok"}},
                },
            )

    class FakeKube:
        namespace = "palaceoftruth"

        def get(self, path: str, *, query: dict[str, Any] | None = None) -> dict[str, Any]:
            return {
                "items": [
                    {
                        "metadata": {
                            "name": "worker-abc",
                            "uid": "worker-uid-abc",
                            "labels": {"app": "palaceoftruth-worker"},
                        },
                        "spec": {"containers": [{"name": "worker"}]},
                        "status": {
                            "phase": "Running",
                            "conditions": [{"type": "Ready", "status": "True"}],
                            "containerStatuses": [
                                {"name": "worker", "ready": True, "started": True, "restartCount": 0}
                            ],
                        },
                    },
                    *_ready_sentinel_pods(),
                ],
            }

        def get_text(self, path: str, *, query: dict[str, Any] | None = None) -> str:
            return (
                "Redis Sentinel startup dependency ready: master=valkey:6379\n"
                "Starting worker for 1 functions\n"
                "redis.exceptions.MasterNotFoundError: No master found for 'mymaster'"
            )

    async def healthy_sentinel():
        return SimpleNamespace(
            master_host="valkey-primary",
            master_port=6379,
            connected_replicas=1,
        )

    monkeypatch.setattr(rollout_smoke, "check_rollout_gate", healthy_sentinel)
    report = {"target": "palaceoftruth", "tenant_id": "tenant-a", "checks": [], "alerts": []}
    args = SimpleNamespace(
        namespace="palaceoftruth",
        pod_label_selector="app.kubernetes.io/instance=palaceoftruth",
        worker_name_fragment="worker",
        log_since_seconds=3600,
        log_tail_lines=500,
        request_timeout=5,
        dependency_timeout_seconds=1,
        dependency_interval_seconds=0.01,
        expected_sentinel_pods=3,
        sentinel_name_fragment="valkey-sentinel",
    )

    rollout_smoke.check_runtime_dependencies(FakeClient(), report, args, kube=FakeKube())

    assert report["alerts"] == []
    worker_state = report["checks"][0]["workers"]
    assert worker_state["ready"] is True
    assert worker_state["unresolved_errors"] == []
    assert worker_state["historical_master_not_found_hits"] == [
        {"pod": "worker-abc", "container": "worker"}
    ]
    assert worker_state["sentinel_reconciliation"] == {
        "healthy": True,
        "master": "valkey-primary:6379",
        "connected_replicas": 1,
    }
    assert worker_state["worker_recheck_stable"] is True


def test_runtime_dependencies_fail_closed_when_master_discovery_error_is_still_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def request(self, method: str, path: str, **_: Any) -> rollout_smoke.HttpResult:
            assert (method, path) == ("GET", "/ready")
            return rollout_smoke.HttpResult(
                200,
                {
                    "status": "ok",
                    "dependencies": {"database": {"status": "ok"}, "queue": {"status": "ok"}},
                },
            )

    class FakeKube:
        namespace = "palaceoftruth"

        def get(self, path: str, *, query: dict[str, Any] | None = None) -> dict[str, Any]:
            return {
                "items": [
                    {
                        "metadata": {
                            "name": "worker-abc",
                            "uid": "worker-uid-abc",
                            "labels": {"app": "palaceoftruth-worker"},
                        },
                        "spec": {"containers": [{"name": "worker"}]},
                        "status": {
                            "phase": "Running",
                            "conditions": [{"type": "Ready", "status": "True"}],
                            "containerStatuses": [
                                {"name": "worker", "ready": True, "started": True, "restartCount": 0}
                            ],
                        },
                    },
                    *_ready_sentinel_pods(),
                ],
            }

        def get_text(self, path: str, *, query: dict[str, Any] | None = None) -> str:
            return (
                "Redis Sentinel startup dependency ready: master=valkey:6379\n"
                "Starting worker for 1 functions\n"
                "redis.exceptions.MasterNotFoundError: No master found for 'mymaster'"
            )

    async def failed_sentinel():
        raise RuntimeError("No master found for 'mymaster'")

    monkeypatch.setattr(rollout_smoke, "check_rollout_gate", failed_sentinel)
    report = {"target": "palaceoftruth", "tenant_id": "tenant-a", "checks": [], "alerts": []}
    args = SimpleNamespace(
        namespace="palaceoftruth",
        pod_label_selector="app.kubernetes.io/instance=palaceoftruth",
        worker_name_fragment="worker",
        log_since_seconds=3600,
        log_tail_lines=500,
        request_timeout=5,
        dependency_timeout_seconds=0.01,
        dependency_interval_seconds=0.01,
        expected_sentinel_pods=3,
        sentinel_name_fragment="valkey-sentinel",
    )

    rollout_smoke.check_runtime_dependencies(FakeClient(), report, args, kube=FakeKube())

    assert [alert["code"] for alert in report["alerts"]] == ["runtime_dependencies_not_ready"]
    worker_state = report["checks"][0]["workers"]
    assert worker_state["ready"] is False
    assert worker_state["unresolved_errors"] == [{"pod": "worker-abc", "container": "worker"}]
    assert worker_state["sentinel_reconciliation"] == {
        "healthy": False,
        "error_class": "RuntimeError",
        "error": "No master found for 'mymaster'",
    }


@pytest.mark.parametrize(("topology", "selected_count"), [("missing", 0), ("one_of_three_ready", 3)])
def test_runtime_dependencies_fail_closed_for_missing_or_split_sentinel_pods(
    monkeypatch: pytest.MonkeyPatch,
    topology: str,
    selected_count: int,
) -> None:
    sentinel_pods = _ready_sentinel_pods()
    if topology == "missing":
        sentinel_pods = []
    else:
        for pod in sentinel_pods[1:]:
            pod["status"]["conditions"][0]["status"] = "False"
            pod["status"]["containerStatuses"][0]["ready"] = False

    class FakeClient:
        def request(self, method: str, path: str, **_: Any) -> rollout_smoke.HttpResult:
            assert (method, path) == ("GET", "/ready")
            return rollout_smoke.HttpResult(
                200,
                {
                    "status": "ok",
                    "dependencies": {"database": {"status": "ok"}, "queue": {"status": "ok"}},
                },
            )

    class FakeKube:
        namespace = "palaceoftruth"

        def get(self, path: str, *, query: dict[str, Any] | None = None) -> dict[str, Any]:
            return {
                "items": [
                    {
                        "metadata": {
                            "name": "worker-abc",
                            "uid": "worker-uid-abc",
                            "labels": {"app": "palaceoftruth-worker"},
                        },
                        "spec": {"containers": [{"name": "worker"}]},
                        "status": {
                            "phase": "Running",
                            "conditions": [{"type": "Ready", "status": "True"}],
                            "containerStatuses": [
                                {"name": "worker", "ready": True, "started": True, "restartCount": 0}
                            ],
                        },
                    },
                    *sentinel_pods,
                ]
            }

        def get_text(self, path: str, *, query: dict[str, Any] | None = None) -> str:
            return (
                "Redis Sentinel startup dependency ready: master=valkey:6379\n"
                "Starting worker for 1 functions\n"
                "redis.exceptions.MasterNotFoundError: No master found for 'mymaster'"
            )

    async def sentinel_probe_must_not_run():
        raise AssertionError("service-level probe must not mask incomplete Sentinel pods")

    monkeypatch.setattr(rollout_smoke, "check_rollout_gate", sentinel_probe_must_not_run)
    report = {"target": "palaceoftruth", "tenant_id": "tenant-a", "checks": [], "alerts": []}
    args = SimpleNamespace(
        namespace="palaceoftruth",
        pod_label_selector="app.kubernetes.io/instance=palaceoftruth",
        worker_name_fragment="worker",
        log_since_seconds=3600,
        log_tail_lines=500,
        request_timeout=5,
        dependency_timeout_seconds=0.01,
        dependency_interval_seconds=0.01,
        expected_sentinel_pods=3,
        sentinel_name_fragment="valkey-sentinel",
    )

    rollout_smoke.check_runtime_dependencies(FakeClient(), report, args, kube=FakeKube())

    assert [alert["code"] for alert in report["alerts"]] == ["runtime_dependencies_not_ready"]
    sentinel_state = report["checks"][0]["workers"]["sentinel_pods"]
    assert sentinel_state["ready"] is False
    assert sentinel_state["expected"] == 3
    assert len(sentinel_state["selected"]) == selected_count


def test_master_discovery_reconciliation_fails_closed_across_worker_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeKube:
        namespace = "palaceoftruth"
        reads = 0

        def get(self, path: str, *, query: dict[str, Any] | None = None) -> dict[str, Any]:
            self.reads += 1
            return {
                "items": [
                    {
                        "metadata": {
                            "name": "worker-abc",
                            "uid": f"worker-uid-{self.reads}",
                            "labels": {"app": "palaceoftruth-worker"},
                        },
                        "spec": {"containers": [{"name": "worker"}]},
                        "status": {
                            "phase": "Running",
                            "conditions": [{"type": "Ready", "status": "True"}],
                            "containerStatuses": [
                                {"name": "worker", "ready": True, "started": True, "restartCount": 0}
                            ],
                        },
                    },
                    *_ready_sentinel_pods(),
                ]
            }

        def get_text(self, path: str, *, query: dict[str, Any] | None = None) -> str:
            return (
                "Redis Sentinel startup dependency ready: master=valkey:6379\n"
                "Starting worker for 1 functions\n"
                "redis.exceptions.MasterNotFoundError: No master found for 'mymaster'"
            )

    async def healthy_sentinel():
        return SimpleNamespace(master_host="valkey-primary", master_port=6379, connected_replicas=1)

    monkeypatch.setattr(rollout_smoke, "check_rollout_gate", healthy_sentinel)
    args = SimpleNamespace(
        pod_label_selector="app.kubernetes.io/instance=palaceoftruth",
        worker_name_fragment="worker",
        log_since_seconds=3600,
        log_tail_lines=500,
        expected_sentinel_pods=3,
        sentinel_name_fragment="valkey-sentinel",
    )
    kube = FakeKube()
    initial_state = rollout_smoke._worker_startup_state(kube, args)

    reconciled = rollout_smoke._reconcile_worker_master_discovery_errors(initial_state, kube, args)

    assert reconciled["ready"] is False
    assert reconciled["worker_recheck_stable"] is False
    assert reconciled["unresolved_errors"] == [{"pod": "worker-abc", "container": "worker"}]


@pytest.mark.parametrize(
    ("second_suffix", "expected_stable"),
    [
        ("\nWorker health heartbeat completed", True),
        ("\nredis.exceptions.MasterNotFoundError: No master found for 'mymaster'", False),
    ],
)
def test_master_discovery_reconciliation_ignores_unrelated_logs_but_rejects_new_errors(
    monkeypatch: pytest.MonkeyPatch,
    second_suffix: str,
    expected_stable: bool,
) -> None:
    class FakeKube:
        namespace = "palaceoftruth"
        reads = 0

        def get(self, path: str, *, query: dict[str, Any] | None = None) -> dict[str, Any]:
            self.reads += 1
            return {
                "items": [
                    {
                        "metadata": {
                            "name": "worker-abc",
                            "uid": "worker-uid-abc",
                            "labels": {"app": "palaceoftruth-worker"},
                        },
                        "spec": {"containers": [{"name": "worker"}]},
                        "status": {
                            "phase": "Running",
                            "conditions": [{"type": "Ready", "status": "True"}],
                            "containerStatuses": [
                                {"name": "worker", "ready": True, "started": True, "restartCount": 0}
                            ],
                        },
                    },
                    *_ready_sentinel_pods(),
                ]
            }

        def get_text(self, path: str, *, query: dict[str, Any] | None = None) -> str:
            base = (
                "Redis Sentinel startup dependency ready: master=valkey:6379\n"
                "Starting worker for 1 functions\n"
                "redis.exceptions.MasterNotFoundError: No master found for 'mymaster'"
            )
            return base if self.reads == 1 else base + second_suffix

    async def healthy_sentinel():
        return SimpleNamespace(master_host="valkey-primary", master_port=6379, connected_replicas=1)

    monkeypatch.setattr(rollout_smoke, "check_rollout_gate", healthy_sentinel)
    args = SimpleNamespace(
        pod_label_selector="app.kubernetes.io/instance=palaceoftruth",
        worker_name_fragment="worker",
        log_since_seconds=3600,
        log_tail_lines=500,
        expected_sentinel_pods=3,
        sentinel_name_fragment="valkey-sentinel",
    )
    kube = FakeKube()
    initial_state = rollout_smoke._worker_startup_state(kube, args)

    reconciled = rollout_smoke._reconcile_worker_master_discovery_errors(initial_state, kube, args)

    assert reconciled["worker_recheck_stable"] is expected_stable
    assert reconciled["ready"] is expected_stable
    if expected_stable:
        assert reconciled["unresolved_errors"] == []
    else:
        assert reconciled["unresolved_errors"] == [{"pod": "worker-abc", "container": "worker"}]


def test_runtime_dependencies_accept_ready_long_lived_worker_without_recent_startup_logs() -> None:
    class FakeClient:
        def request(self, method: str, path: str, **_: Any) -> rollout_smoke.HttpResult:
            assert (method, path) == ("GET", "/ready")
            return rollout_smoke.HttpResult(
                200,
                {
                    "status": "ok",
                    "dependencies": {"database": {"status": "ok"}, "queue": {"status": "ok"}},
                },
            )

    class FakeKube:
        namespace = "palaceoftruth"

        def get(self, path: str, *, query: dict[str, Any] | None = None) -> dict[str, Any]:
            return {
                "items": [
                    {
                        "metadata": {"name": "worker-long-lived", "labels": {"app": "palaceoftruth-worker"}},
                        "spec": {"containers": [{"name": "worker"}]},
                        "status": {
                            "phase": "Running",
                            "conditions": [{"type": "Ready", "status": "True"}],
                            "containerStatuses": [{"name": "worker", "ready": True, "started": True}],
                        },
                    }
                ]
            }

        def get_text(self, path: str, *, query: dict[str, Any] | None = None) -> str:
            assert query == {"container": "worker", "sinceSeconds": 3600, "tailLines": 500}
            return ""

    report = {"target": "palaceoftruth", "tenant_id": "tenant-a", "checks": [], "alerts": []}
    args = SimpleNamespace(
        namespace="palaceoftruth",
        pod_label_selector="app.kubernetes.io/instance=palaceoftruth",
        worker_name_fragment="worker",
        log_since_seconds=3600,
        log_tail_lines=500,
        request_timeout=5,
        dependency_timeout_seconds=1,
        dependency_interval_seconds=0.01,
    )

    rollout_smoke.check_runtime_dependencies(FakeClient(), report, args, kube=FakeKube())

    assert report["alerts"] == []
    worker_state = report["checks"][0]["workers"]
    assert worker_state["ready"] is True
    assert worker_state["readiness_fallback"] == [
        {
            "pod": "worker-long-lived",
            "container": "worker",
            "missing": [
                "Redis Sentinel startup dependency ready",
                "Starting worker",
            ],
        }
    ]


@pytest.mark.parametrize(
    ("ready_condition", "container_ready", "log_text", "expected_field"),
    [
        (
            "False",
            False,
            "Redis Sentinel startup dependency ready: master=valkey:6379\nStarting worker",
            "unready_pods",
        ),
        (
            "True",
            True,
            "redis.exceptions.MasterNotFoundError: No master found for 'mymaster'",
            "unresolved_errors",
        ),
    ],
)
def test_worker_startup_state_fails_closed_for_unready_or_unresolved_worker(
    ready_condition: str,
    container_ready: bool,
    log_text: str,
    expected_field: str,
) -> None:
    class FakeKube:
        namespace = "palaceoftruth"

        def get(self, path: str, *, query: dict[str, Any] | None = None) -> dict[str, Any]:
            return {
                "items": [
                    {
                        "metadata": {"name": "worker-abc", "labels": {"app": "palaceoftruth-worker"}},
                        "spec": {"containers": [{"name": "worker"}]},
                        "status": {
                            "phase": "Running",
                            "conditions": [{"type": "Ready", "status": ready_condition}],
                            "containerStatuses": [{"name": "worker", "ready": container_ready}],
                        },
                    }
                ]
            }

        def get_text(self, path: str, *, query: dict[str, Any] | None = None) -> str:
            return log_text

    state = rollout_smoke._worker_startup_state(
        FakeKube(),
        SimpleNamespace(
            pod_label_selector="app.kubernetes.io/instance=palaceoftruth",
            worker_name_fragment="worker",
            log_since_seconds=3600,
            log_tail_lines=500,
        ),
    )

    assert state["ready"] is False
    assert state[expected_field]


def test_worker_startup_state_fails_closed_when_recent_logs_cannot_be_read() -> None:
    class FakeKube:
        namespace = "palaceoftruth"

        def get(self, path: str, *, query: dict[str, Any] | None = None) -> dict[str, Any]:
            return {
                "items": [
                    {
                        "metadata": {"name": "worker-abc", "labels": {"app": "palaceoftruth-worker"}},
                        "spec": {"containers": [{"name": "worker"}]},
                        "status": {
                            "phase": "Running",
                            "conditions": [{"type": "Ready", "status": "True"}],
                            "containerStatuses": [{"name": "worker", "ready": True}],
                        },
                    }
                ]
            }

        def get_text(self, path: str, *, query: dict[str, Any] | None = None) -> str:
            raise PermissionError("pods/log denied")

    state = rollout_smoke._worker_startup_state(
        FakeKube(),
        SimpleNamespace(
            pod_label_selector="app.kubernetes.io/instance=palaceoftruth",
            worker_name_fragment="worker",
            log_since_seconds=3600,
            log_tail_lines=500,
        ),
    )

    assert state["ready"] is False
    assert state["log_read_failures"] == [
        {"pod": "worker-abc", "container": "worker", "error_class": "PermissionError"}
    ]


def test_runtime_dependencies_skip_kubernetes_requires_only_api_readiness() -> None:
    class FakeClient:
        def request(self, method: str, path: str, **_: Any) -> rollout_smoke.HttpResult:
            assert (method, path) == ("GET", "/ready")
            return rollout_smoke.HttpResult(
                200,
                {
                    "status": "ok",
                    "dependencies": {"database": {"status": "ok"}, "queue": {"status": "ok"}},
                },
            )

    report = {"target": "local", "tenant_id": "tenant-a", "checks": [], "alerts": []}
    args = SimpleNamespace(
        namespace=None,
        request_timeout=5,
        dependency_timeout_seconds=1,
        dependency_interval_seconds=0.01,
        skip_kubernetes=True,
    )

    rollout_smoke.check_runtime_dependencies(FakeClient(), report, args)

    assert report["alerts"] == []
    assert report["checks"] == [
        {
            "name": "runtime_dependencies",
            "status": "passed",
            "api": {
                "ready": True,
                "http_status": 200,
                "status": "ok",
                "dependencies": {"database": "ok", "queue": "ok"},
            },
            "workers": {
                "ready": True,
                "skipped": True,
                "reason": "--skip-kubernetes",
            },
        }
    ]


def test_build_report_combines_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sentinel(report: dict[str, Any]) -> None:
        rollout_smoke._record_check(report, "sentinel_valkey", "passed", master="valkey-primary:6379")

    class FakeClient:
        def __init__(self, *, base_url: str, api_key: str | None, timeout: float, **_: Any) -> None:
            assert base_url == "http://api/api/v1"

        def request(self, method: str, path: str, **_: Any) -> rollout_smoke.HttpResult:
            if (method, path) == ("GET", "/health"):
                return rollout_smoke.HttpResult(200, {"status": "ok"})
            if (method, path) == ("GET", "/memory/whoami"):
                return rollout_smoke.HttpResult(200, {"tenant_id": "tenant-a"})
            if (method, path) == ("POST", "/memory/entries"):
                return rollout_smoke.HttpResult(200, {"job_id": "job-1", "contract_status": "queued"})
            if (method, path) == ("GET", "/memory/jobs/job-1"):
                return rollout_smoke.HttpResult(200, {"status": "completed"})
            if (method, path) == ("GET", "/memory/jobs"):
                return rollout_smoke.HttpResult(200, {"jobs": [{"status": "completed"}]})
            raise AssertionError((method, path))

    monkeypatch.setattr(rollout_smoke, "check_sentinel", fake_sentinel)
    monkeypatch.setattr(rollout_smoke, "HttpClient", FakeClient)
    monkeypatch.setattr(rollout_smoke, "check_runtime_dependencies", lambda client, report, args, kube=None: rollout_smoke._record_check(report, "runtime_dependencies", "passed"))
    monkeypatch.setattr(rollout_smoke, "check_mcp_reachable", lambda url, report, *, timeout: rollout_smoke._record_check(report, "mcp_health", "passed"))
    monkeypatch.setattr(rollout_smoke, "check_kubernetes", lambda report, args, kube=None: rollout_smoke._record_check(report, "kubernetes_alerts", "passed"))
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "secret")
    args = rollout_smoke.build_parser().parse_args(
        [
            "--target-name",
            "palaceoftruth",
            "--api-base-url",
            "http://api/api/v1",
            "--run-id",
            "20260527T180000Z",
        ]
    )

    report = rollout_smoke.build_report(args)

    assert report["status"] == "passed"
    assert report["alert_count"] == 0
    assert report["tenant_id"] == "tenant-a"
