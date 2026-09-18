"""Exercise rendered scrape trust with the same middleware used by the API."""
from __future__ import annotations

import re

import pytest
from starlette.applications import Starlette
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from tests.test_helm_scope_security import _render


def _backend(manifests):
    return next(
        item for item in manifests
        if item.get("kind") == "Deployment"
        and item["metadata"]["name"] == "palaceoftruth-backend"
    )["spec"]["template"]["spec"]["containers"][0]


def _runtime_hosts(manifests, pod_ip, secret_hosts=None):
    # Kubelet resolves envFrom first, then expands each env.value in order.
    config = next(item for item in manifests if item.get("kind") == "ConfigMap")
    env = dict(config["data"])
    if secret_hosts is not None:
        env["TRUSTED_HOSTS"] = secret_hosts
    for entry in _backend(manifests)["env"]:
        if entry.get("valueFrom", {}).get("fieldRef", {}).get("fieldPath") == "status.podIP":
            env[entry["name"]] = pod_ip
        elif "value" in entry:
            env[entry["name"]] = re.sub(
                r"\$\(([^)]+)\)", lambda match: env.get(match[1], match[0]), entry["value"]
            )
    return [host.strip() for host in env["TRUSTED_HOSTS"].split(",") if host.strip()]


@pytest.mark.parametrize("pod_ip,other_ip", [("10.42.1.97", "10.42.4.150"), ("10.42.4.150", "10.42.1.97")])
def test_authenticated_monitor_trusts_only_its_own_pod_address(pod_ip, other_ip):
    manifests = _render("metrics.serviceMonitor.enabled=true", "backend.replicas=2")
    hosts = _runtime_hosts(manifests, pod_ip)
    app = Starlette(routes=[Route("/api/v1/metrics", lambda request: PlainTextResponse("reached"))])
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    client = TestClient(app)
    assert client.get("/api/v1/metrics", headers={"Host": f"{pod_ip}:8000"}).status_code == 200
    for host in (other_ip, "attacker.example", "0.0.0.0"):
        assert client.get("/api/v1/metrics", headers={"Host": f"{host}:8000"}).status_code == 400
    assert "*" not in hosts
    assert "api.palaceoftruth.example.com" in hosts
    monitor = next(item for item in manifests if item.get("kind") == "ServiceMonitor")
    endpoint = monitor["spec"]["endpoints"][0]
    assert endpoint["authorization"] == {
        "type": "Bearer", "credentials": {"name": "palaceoftruth-app-secrets", "key": "API_KEY"}
    }
    # Keep endpoint discovery per pod; do not rewrite both targets to a Service VIP.
    assert "relabelings" not in endpoint
    assert "httpHeaders" not in endpoint


def test_disabled_monitor_does_not_extend_trusted_hosts():
    manifests = _render()
    assert "10.42.1.97" not in _runtime_hosts(manifests, "10.42.1.97")
    assert not any(entry["name"] == "TRUSTED_HOSTS" for entry in _backend(manifests)["env"])


def test_monitor_preserves_secret_host_override():
    manifests = _render("metrics.serviceMonitor.enabled=true")
    assert _runtime_hosts(manifests, "10.42.1.97", "custom.example") == ["custom.example", "10.42.1.97"]


@pytest.mark.parametrize("pod_ip", ["10.42.1.97", "10.42.4.150"])
def test_pod_scrapes_still_require_real_metrics_authorization(pod_ip):
    from fastapi import FastAPI
    from tests.test_system_api import MetricsSession, _metrics_client

    manifests = _render("metrics.serviceMonitor.enabled=true")
    client = _metrics_client(MetricsSession())
    assert isinstance(client.app, FastAPI)
    client.app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=_runtime_hosts(manifests, pod_ip)
    )
    # Real system router, credential dependency and metrics renderer; only the
    # database uses the existing metrics test session. No production key needed.
    response = client.get("/api/v1/metrics", headers={"Host": f"{pod_ip}:8000"})
    assert response.status_code == 200
    assert "palace_metrics_scrape" in response.text
    client.headers.pop("Authorization")
    assert client.get("/api/v1/metrics", headers={"Host": f"{pod_ip}:8000"}).status_code == 401
    for authorization in ("", "Bearer wrong-key", "Basic test-api-key"):
        response = client.get("/api/v1/metrics", headers={
            "Host": f"{pod_ip}:8000", "Authorization": authorization,
        })
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
    assert client.get("/api/v1/metrics", headers={
        "Host": "attacker.example", "Authorization": "Bearer test-api-key",
    }).status_code == 400
