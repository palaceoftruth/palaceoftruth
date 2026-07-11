from __future__ import annotations

import argparse
import asyncio
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.check_redis_sentinel_rollout_gate import check_rollout_gate


TERMINAL_SUCCESS = {"complete", "completed", "duplicate"}
TERMINAL_FAILURE = {"failed", "cancelled", "canceled", "dependency_unavailable"}
KUBE_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
KUBE_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
KUBE_NAMESPACE_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"


@dataclass(frozen=True)
class HttpResult:
    status: int
    payload: Any


class HttpClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        bearer_token: str | None,
        oauth_client_secret: str | None,
        oauth_token_url: str | None,
        oauth_resource: str | None,
        client_key: str,
        client_scopes: list[str],
        timeout: float,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.bearer_token = bearer_token
        self.oauth_client_secret = oauth_client_secret
        self.oauth_token_url = oauth_token_url
        self.oauth_resource = oauth_resource
        self.client_key = client_key
        self.client_scopes = client_scopes
        self.timeout = timeout

    def _mcp_scope_headers(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, str]:
        normalized = method.upper(), path
        if normalized in {
            ("GET", "/memory/whoami"),
            ("GET", "/memory/jobs"),
        } or (normalized[0] == "GET" and path.startswith("/memory/jobs/")):
            return {"X-MCP-Scope": "read", "X-MCP-Scopes": "read"}
        if normalized == ("POST", "/memory/entries"):
            scopes = ["write"]
            write_grant = _write_scope_grant(body)
            if write_grant:
                scopes.append(write_grant)
            return {"X-MCP-Scope": "write", "X-MCP-Scopes": ",".join(scopes)}
        return {}

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> HttpResult:
        encoded_query = urllib.parse.urlencode(query or {})
        url = f"{self.base_url}{path}"
        if encoded_query:
            url = f"{url}?{encoded_query}"
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.bearer_token or self.oauth_client_secret:
            try:
                token = self._active_bearer_token()
            except Exception as exc:
                return HttpResult(599, {"error": "oauth_token_unavailable", "detail": str(exc)})
            if token:
                headers["Authorization"] = f"Bearer {token}"
        elif self.api_key:
            headers["X-API-Key"] = self.api_key
            headers.update(self._mcp_scope_headers(method, path, body))
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
                return HttpResult(response.status, _decode_payload(payload))
        except urllib.error.HTTPError as exc:
            return HttpResult(exc.code, _decode_payload(exc.read()))

    def _active_bearer_token(self) -> str | None:
        if self.bearer_token:
            return self.bearer_token
        if not self.oauth_client_secret:
            return None
        token_url = self.oauth_token_url or f"{self.base_url}/memory/mcp/oauth/token"
        oauth_resource = _oauth_backend_resource(self.oauth_resource, token_url)
        data = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": self.client_key,
                "client_secret": self.oauth_client_secret,
                "scope": " ".join(self.client_scopes),
                "resource": oauth_resource,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            token_url,
            data=data,
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = _decode_payload(response.read())
        except urllib.error.HTTPError as exc:
            payload = _decode_payload(exc.read())
            raise RuntimeError(f"Palace OAuth token endpoint returned HTTP {exc.code}: {payload}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Palace OAuth token endpoint was unreachable: {exc.reason}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("access_token"), str):
            raise RuntimeError("Palace OAuth token endpoint did not return access_token")
        self.bearer_token = payload["access_token"].strip()
        return self.bearer_token


class KubernetesClient:
    def __init__(self, *, namespace: str | None = None, timeout: float = 10.0) -> None:
        host = os.getenv("KUBERNETES_SERVICE_HOST")
        port = os.getenv("KUBERNETES_SERVICE_PORT", "443")
        if not host or not os.path.exists(KUBE_TOKEN_PATH):
            raise RuntimeError("in-cluster Kubernetes service account is unavailable")
        self.base_url = f"https://{host}:{port}"
        self.namespace = namespace or _read_text(KUBE_NAMESPACE_PATH).strip()
        self.token = _read_text(KUBE_TOKEN_PATH).strip()
        self.timeout = timeout
        self.context = ssl.create_default_context(cafile=KUBE_CA_PATH)

    def get(self, path: str, *, query: dict[str, Any] | None = None) -> dict[str, Any]:
        encoded_query = urllib.parse.urlencode(query or {})
        url = f"{self.base_url}{path}"
        if encoded_query:
            url = f"{url}?{encoded_query}"
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout, context=self.context) as response:
            payload = response.read()
            decoded = _decode_payload(payload)
            if not isinstance(decoded, dict):
                raise RuntimeError(f"Kubernetes API response was not an object: {decoded!r}")
            return decoded

    def get_text(self, path: str, *, query: dict[str, Any] | None = None) -> str:
        encoded_query = urllib.parse.urlencode(query or {})
        url = f"{self.base_url}{path}"
        if encoded_query:
            url = f"{url}?{encoded_query}"
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(request, timeout=self.timeout, context=self.context) as response:
            return response.read().decode("utf-8", errors="replace")


def _decode_payload(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return raw.decode("utf-8", errors="replace")


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _write_scope_grant(entry: dict[str, Any] | None) -> str | None:
    if not isinstance(entry, dict):
        return None
    scope = entry.get("scope")
    if not isinstance(scope, dict):
        return None
    scope_type = scope.get("type")
    if scope_type in {"agent", "workspace", "session"}:
        return f"write:{scope_type}"
    return None


def _oauth_resource_from_token_url(token_url: str) -> str:
    parsed = urllib.parse.urlsplit(token_url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/api/v1", "", ""))


def _oauth_backend_resource(configured_resource: str | None, token_url: str) -> str:
    if configured_resource:
        configured = urllib.parse.urlsplit(configured_resource)
        if configured.path.rstrip("/") != "/mcp":
            return configured_resource
    return _oauth_resource_from_token_url(token_url)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _api_key() -> str:
    return (
        os.getenv("PALACEOFTRUTH_API_KEY")
        or os.getenv("SECONDBRAIN_API_KEY")
        or os.getenv("API_KEY")
        or ""
    ).strip()


def _bearer_token() -> str:
    return (os.getenv("PALACEOFTRUTH_MCP_BEARER_TOKEN") or os.getenv("SECONDBRAIN_MCP_BEARER_TOKEN") or "").strip()


def _oauth_client_secret() -> str:
    return (
        os.getenv("PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET")
        or os.getenv("SECONDBRAIN_MCP_OAUTH_CLIENT_SECRET")
        or ""
    ).strip()


def _oauth_token_url() -> str:
    return (
        os.getenv("PALACEOFTRUTH_MCP_OAUTH_TOKEN_URL")
        or os.getenv("SECONDBRAIN_MCP_OAUTH_TOKEN_URL")
        or ""
    ).strip()


def _oauth_resource() -> str:
    return (
        os.getenv("PALACEOFTRUTH_MCP_OAUTH_RESOURCE")
        or os.getenv("SECONDBRAIN_MCP_OAUTH_RESOURCE")
        or os.getenv("PALACEOFTRUTH_MCP_OAUTH_AUDIENCE")
        or os.getenv("SECONDBRAIN_MCP_OAUTH_AUDIENCE")
        or ""
    ).strip()


def _client_key() -> str:
    return (os.getenv("PALACEOFTRUTH_MCP_CLIENT_KEY") or os.getenv("SECONDBRAIN_MCP_CLIENT_KEY") or "rollout-smoke").strip()


def _client_scopes() -> list[str]:
    raw = os.getenv("PALACEOFTRUTH_MCP_CLIENT_SCOPES") or os.getenv("SECONDBRAIN_MCP_CLIENT_SCOPES") or "read,write,write:workspace"
    return [part.strip() for part in raw.replace(",", " ").split() if part.strip()]


def _memory_entry(*, tenant_id: str, target_name: str, run_id: str, scope_key: str) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "title": f"Rollout memory smoke {target_name} {run_id}",
        "summary": "Post-sync Palace memory dependency smoke.",
        "body": (
            f"PALACE-ROLLOUT-SMOKE-{run_id}\n\n"
            "This non-sensitive memory verifies post-sync write, queue, worker, and retrieval dependencies."
        ),
        "source": "palace-rollout-smoke",
        "created_at": utc_now().isoformat().replace("+00:00", "Z"),
        "tags": ["palace-rollout-smoke", f"palace-rollout-smoke-{target_name}", f"palace-rollout-smoke-{run_id}"],
        "scope": {"type": "workspace", "key": scope_key},
        "created_by_role": "automation",
        "metadata": {"target": target_name, "run_id": run_id, "smoke": "palace-memory-rollout"},
        "idempotency_key": f"palace-rollout-smoke:{target_name}:{run_id}",
        "enable_ai_enrichment": False,
        "relationship_policy": "deferred",
    }


def _record_check(report: dict[str, Any], name: str, status: str, **details: Any) -> None:
    report["checks"].append(
        {"name": name, "status": status, **{key: value for key, value in details.items() if value is not None}}
    )


def _alert(report: dict[str, Any], code: str, message: str, **details: Any) -> None:
    report["alerts"].append(
        {
            "target": report["target"],
            "tenant_id": report.get("tenant_id"),
            "code": code,
            "message": message,
            **{key: value for key, value in details.items() if value is not None},
        }
    )


def check_api_health(client: HttpClient, report: dict[str, Any]) -> None:
    result = client.request("GET", "/health")
    if result.status >= 400:
        _alert(report, "api_health_failed", f"API health returned HTTP {result.status}", response=result.payload)
        _record_check(report, "api_health", "failed", http_status=result.status)
        return
    _record_check(report, "api_health", "passed", http_status=result.status)


def _api_readiness_state(client: HttpClient) -> dict[str, Any]:
    result = client.request("GET", "/ready")
    payload = result.payload if isinstance(result.payload, dict) else {}
    dependencies = payload.get("dependencies") if isinstance(payload.get("dependencies"), dict) else {}
    dependency_statuses = {
        name: value.get("status")
        for name, value in dependencies.items()
        if isinstance(value, dict)
    }
    ready = (
        result.status < 400
        and payload.get("status") == "ok"
        and {"database", "queue"}.issubset(dependency_statuses)
        and all(status == "ok" for status in dependency_statuses.values())
    )
    return {
        "ready": ready,
        "http_status": result.status,
        "status": payload.get("status"),
        "dependencies": dependency_statuses,
    }


def _worker_startup_state(kube: KubernetesClient, args: argparse.Namespace) -> dict[str, Any]:
    pods_payload = kube.get(
        f"/api/v1/namespaces/{kube.namespace}/pods",
        query={"labelSelector": args.pod_label_selector},
    )
    pods = pods_payload.get("items") if isinstance(pods_payload.get("items"), list) else []
    selected_pods: list[str] = []
    missing_markers: list[dict[str, Any]] = []
    non_running: list[dict[str, str]] = []

    for pod in pods:
        if not isinstance(pod, dict):
            continue
        metadata = pod.get("metadata") if isinstance(pod.get("metadata"), dict) else {}
        status = pod.get("status") if isinstance(pod.get("status"), dict) else {}
        pod_name = str(metadata.get("name") or "")
        app_label = str((metadata.get("labels") or {}).get("app") or "")
        if args.worker_name_fragment and args.worker_name_fragment not in app_label and args.worker_name_fragment not in pod_name:
            continue
        if metadata.get("deletionTimestamp"):
            continue
        selected_pods.append(pod_name)
        phase = str(status.get("phase") or "")
        if phase != "Running":
            non_running.append({"pod": pod_name, "phase": phase})
            continue
        for container in pod.get("spec", {}).get("containers") or []:
            container_name = container.get("name") if isinstance(container, dict) else None
            if not container_name:
                continue
            log_text = kube.get_text(
                f"/api/v1/namespaces/{kube.namespace}/pods/{pod_name}/log",
                query={
                    "container": container_name,
                    "sinceSeconds": args.log_since_seconds,
                    "tailLines": args.log_tail_lines,
                },
            )
            required_markers = ("Redis Sentinel startup dependency ready", "Starting worker")
            absent = [marker for marker in required_markers if marker not in log_text]
            if absent:
                missing_markers.append({"pod": pod_name, "container": container_name, "missing": absent})

    return {
        "ready": bool(selected_pods) and not non_running and not missing_markers,
        "selected_pods": selected_pods,
        "non_running": non_running,
        "missing_markers": missing_markers,
    }


def check_runtime_dependencies(
    client: HttpClient,
    report: dict[str, Any],
    args: argparse.Namespace,
    *,
    kube: KubernetesClient | None = None,
) -> None:
    """Wait until API, database/queue, and every ARQ worker are ready to consume."""
    deadline = time.monotonic() + args.dependency_timeout_seconds
    last_state: dict[str, Any] = {}
    try:
        kube = kube or KubernetesClient(namespace=args.namespace, timeout=args.request_timeout)
    except Exception as exc:
        _alert(report, "runtime_dependencies_unqueryable", str(exc), error_class=exc.__class__.__name__)
        _record_check(report, "runtime_dependencies", "failed", error_class=exc.__class__.__name__)
        return

    while time.monotonic() <= deadline:
        api_state = _api_readiness_state(client)
        try:
            worker_state = _worker_startup_state(kube, args)
        except Exception as exc:
            worker_state = {"ready": False, "error_class": exc.__class__.__name__, "error": str(exc)}
        last_state = {"api": api_state, "workers": worker_state}
        if api_state["ready"] and worker_state["ready"]:
            _record_check(report, "runtime_dependencies", "passed", **last_state)
            return
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(args.dependency_interval_seconds, remaining))

    _alert(
        report,
        "runtime_dependencies_not_ready",
        "backend readiness or ARQ worker startup did not complete before the memory write",
        last_state=last_state,
    )
    _record_check(report, "runtime_dependencies", "failed", **last_state)


def check_mcp_reachable(url: str | None, report: dict[str, Any], *, timeout: float) -> None:
    if not url:
        _record_check(report, "mcp_health", "skipped", reason="no MCP URL configured")
        return
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except Exception as exc:
        _alert(report, "mcp_unreachable", f"MCP endpoint was unreachable: {exc.__class__.__name__}")
        _record_check(report, "mcp_health", "failed", error_class=exc.__class__.__name__)
        return
    if status >= 500:
        _alert(report, "mcp_unhealthy", f"MCP endpoint returned HTTP {status}")
        _record_check(report, "mcp_health", "failed", http_status=status)
        return
    _record_check(report, "mcp_health", "passed", http_status=status)


async def check_sentinel(report: dict[str, Any]) -> None:
    try:
        result = await check_rollout_gate()
    except Exception as exc:
        _alert(report, "sentinel_or_valkey_failed", str(exc), error_class=exc.__class__.__name__)
        _record_check(report, "sentinel_valkey", "failed", error_class=exc.__class__.__name__)
        return
    _record_check(
        report,
        "sentinel_valkey",
        "passed",
        master=f"{result.master_host}:{result.master_port}",
        connected_replicas=result.connected_replicas,
    )


def resolve_tenant_identity(client: HttpClient, report: dict[str, Any], args: argparse.Namespace | None = None) -> str | None:
    whoami = client.request("GET", "/memory/whoami")
    if whoami.status >= 400 or not isinstance(whoami.payload, dict) or not whoami.payload.get("tenant_id"):
        _alert(report, "tenant_identity_failed", f"whoami returned HTTP {whoami.status}", response=whoami.payload)
        _record_check(report, "tenant_identity", "failed", http_status=whoami.status)
        return None
    tenant_id = str(whoami.payload["tenant_id"])
    report["tenant_id"] = tenant_id
    if args is not None:
        _check_expected_whoami(whoami.payload, report, args)
    _record_check(report, "tenant_identity", "passed", tenant_id=tenant_id)
    return tenant_id


def _check_expected_whoami(payload: dict[str, Any], report: dict[str, Any], args: argparse.Namespace) -> None:
    mismatches: dict[str, Any] = {}
    expected = {
        "tenant_id": getattr(args, "expected_tenant_id", ""),
        "auth_mode": getattr(args, "expected_auth_mode", ""),
        "mcp_client_key": getattr(args, "expected_client_key", ""),
    }
    for field, expected_value in expected.items():
        if expected_value and payload.get(field) != expected_value:
            mismatches[field] = {"expected": expected_value, "actual": payload.get(field)}
    granted_scopes = payload.get("allowed_scopes") or payload.get("scopes") or []
    expected_scopes = getattr(args, "expected_scope", [])
    if expected_scopes:
        missing = sorted(set(expected_scopes) - {str(scope) for scope in granted_scopes})
        if missing:
            mismatches["scopes"] = {"missing": missing, "actual": granted_scopes}
    if mismatches:
        _alert(report, "tenant_identity_mismatch", "whoami did not match expected OAuth identity", mismatches=mismatches)
        _record_check(report, "tenant_identity_expectations", "failed", mismatches=mismatches)
        return
    if any(expected.values()) or expected_scopes:
        _record_check(report, "tenant_identity_expectations", "passed")


def check_memory_write(client: HttpClient, report: dict[str, Any], args: argparse.Namespace) -> None:
    tenant_id = report.get("tenant_id")
    if not isinstance(tenant_id, str) or not tenant_id:
        tenant_id = resolve_tenant_identity(client, report, args)
    if tenant_id is None:
        _record_check(report, "memory_write", "skipped", reason="tenant identity unavailable")
        return
    run_id = args.run_id or utc_now().strftime("%Y%m%dT%H%M%SZ")
    entry = _memory_entry(tenant_id=tenant_id, target_name=args.target_name, run_id=run_id, scope_key=args.scope_key)
    accepted = client.request("POST", "/memory/entries", body=entry)
    accepted_payload = accepted.payload if isinstance(accepted.payload, dict) else {}
    job_id = str(accepted_payload.get("job_id") or "")
    contract_status = accepted_payload.get("contract_status") or accepted_payload.get("status")
    if accepted.status >= 400 or not job_id:
        _alert(
            report,
            "memory_write_not_accepted",
            f"memory write returned HTTP {accepted.status}",
            contract_status=contract_status,
            response=accepted.payload,
        )
        _record_check(report, "memory_write", "failed", http_status=accepted.status, contract_status=contract_status)
        return
    _record_check(report, "memory_write", "passed", job_id=job_id, contract_status=contract_status)

    deadline = time.monotonic() + args.job_timeout_seconds
    last_payload: dict[str, Any] | None = None
    while time.monotonic() <= deadline:
        job = client.request("GET", f"/memory/jobs/{job_id}")
        payload = job.payload if isinstance(job.payload, dict) else {}
        last_payload = payload
        status = str(payload.get("status") or "")
        if status in TERMINAL_SUCCESS:
            _record_check(report, "memory_job_completion", "passed", job_id=job_id, job_status=status)
            break
        if status in TERMINAL_FAILURE:
            _alert(report, "memory_job_failed", f"memory job {job_id} ended with {status}", job_id=job_id)
            _record_check(report, "memory_job_completion", "failed", job_id=job_id, job_status=status)
            break
        time.sleep(args.job_interval_seconds)
    else:
        _alert(
            report,
            "accepted_but_not_completed",
            f"memory job {job_id} did not complete before timeout",
            job_id=job_id,
            last_status=(last_payload or {}).get("status"),
        )
        _record_check(report, "memory_job_completion", "failed", job_id=job_id, last_payload=last_payload)

    jobs = client.request("GET", "/memory/jobs", query={"page": 1, "per_page": args.job_list_limit})
    if isinstance(jobs.payload, dict) and isinstance(jobs.payload.get("jobs"), list):
        counts: dict[str, int] = {}
        for row in jobs.payload["jobs"]:
            if isinstance(row, dict):
                status = str(row.get("status") or "unknown")
                counts[status] = counts.get(status, 0) + 1
        _record_check(report, "memory_jobs_listing", "passed", returned=len(jobs.payload["jobs"]), status_counts=counts)
    else:
        _alert(report, "memory_jobs_unqueryable", "memory jobs listing did not return jobs", response=jobs.payload)
        _record_check(report, "memory_jobs_listing", "failed")


def check_kubernetes(report: dict[str, Any], args: argparse.Namespace, kube: KubernetesClient | None = None) -> None:
    if args.skip_kubernetes:
        _record_check(report, "kubernetes_alerts", "skipped", reason="--skip-kubernetes")
        return
    try:
        kube = kube or KubernetesClient(namespace=args.namespace, timeout=args.request_timeout)
        pods_payload = kube.get(
            f"/api/v1/namespaces/{kube.namespace}/pods",
            query={"labelSelector": args.pod_label_selector},
        )
    except Exception as exc:
        _alert(report, "kubernetes_query_failed", str(exc), error_class=exc.__class__.__name__)
        _record_check(report, "kubernetes_alerts", "failed", error_class=exc.__class__.__name__)
        return

    pods = pods_payload.get("items") if isinstance(pods_payload.get("items"), list) else []
    restart_rows: list[dict[str, Any]] = []
    non_running_rows: list[dict[str, str]] = []
    master_not_found_hits: list[dict[str, str]] = []
    log_read_failures: list[dict[str, str]] = []
    selected_worker_pods = 0
    selected_worker_containers = 0
    for pod in pods:
        if not isinstance(pod, dict):
            continue
        metadata = pod.get("metadata") if isinstance(pod.get("metadata"), dict) else {}
        status = pod.get("status") if isinstance(pod.get("status"), dict) else {}
        pod_name = str(metadata.get("name") or "")
        app_label = str((metadata.get("labels") or {}).get("app") or "")
        if args.worker_name_fragment and args.worker_name_fragment not in app_label and args.worker_name_fragment not in pod_name:
            continue
        if metadata.get("deletionTimestamp"):
            continue
        phase = str(status.get("phase") or "")
        selected_worker_pods += 1
        if phase and phase != "Running":
            non_running_rows.append({"pod": pod_name, "phase": phase})
            continue
        for container_status in status.get("containerStatuses") or []:
            if not isinstance(container_status, dict):
                continue
            restart_count = int(container_status.get("restartCount") or 0)
            if restart_count >= args.restart_alert_threshold:
                restart_rows.append(
                    {
                        "pod": pod_name,
                        "container": container_status.get("name"),
                        "restart_count": restart_count,
                    }
                )
        if not args.skip_log_scan:
            for container in pod.get("spec", {}).get("containers") or []:
                container_name = container.get("name") if isinstance(container, dict) else None
                if not container_name:
                    continue
                selected_worker_containers += 1
                try:
                    log_text = kube.get_text(
                        f"/api/v1/namespaces/{kube.namespace}/pods/{pod_name}/log",
                        query={
                            "container": container_name,
                            "sinceSeconds": args.log_since_seconds,
                            "tailLines": args.log_tail_lines,
                        },
                    )
                except Exception as exc:
                    log_read_failures.append(
                        {"pod": pod_name, "container": container_name, "error_class": exc.__class__.__name__}
                    )
                    continue
                if _has_unresolved_master_discovery_error(log_text):
                    master_not_found_hits.append({"pod": pod_name, "container": container_name})

    if selected_worker_pods == 0:
        _alert(report, "worker_pods_not_found", "no worker pods matched rollout smoke selector")
    if non_running_rows:
        _alert(report, "worker_pod_not_running", "one or more worker pods are not running", pods=non_running_rows)
    if restart_rows:
        _alert(report, "worker_restart_spike", "worker restart threshold exceeded", pods=restart_rows)
    if master_not_found_hits:
        _alert(report, "master_not_found", "MasterNotFoundError or No master found appeared in worker logs", pods=master_not_found_hits)
    if log_read_failures:
        _alert(report, "worker_log_scan_failed", "one or more worker container logs could not be scanned", pods=log_read_failures)
    failed = bool(non_running_rows or restart_rows or master_not_found_hits or log_read_failures or selected_worker_pods == 0)
    _record_check(
        report,
        "kubernetes_alerts",
        "failed" if failed else "passed",
        pod_count=len(pods),
        selected_worker_pods=selected_worker_pods,
        selected_worker_containers=selected_worker_containers,
        non_running_pods=non_running_rows,
        restart_alerts=restart_rows,
        master_not_found_hits=master_not_found_hits,
        log_read_failures=log_read_failures,
    )


def _has_unresolved_master_discovery_error(log_text: str) -> bool:
    last_error_offset = max(log_text.rfind("MasterNotFoundError"), log_text.rfind("No master found"))
    if last_error_offset == -1:
        return False

    # Startup can legitimately log transient discovery failures before the
    # Sentinel dependency gate reports readiness. Anything after readiness is
    # treated as unresolved so real regressions still fail closed.
    last_ready_offset = log_text.rfind("Redis Sentinel startup dependency ready")
    return last_ready_offset < last_error_offset


def build_report(args: argparse.Namespace, *, kube: KubernetesClient | None = None) -> dict[str, Any]:
    report: dict[str, Any] = {
        "report": "palace-memory-rollout-smoke",
        "generated_at": utc_now().isoformat().replace("+00:00", "Z"),
        "target": args.target_name,
        "checks": [],
        "alerts": [],
    }
    client = HttpClient(
        base_url=args.api_base_url.rstrip("/"),
        api_key=_api_key(),
        bearer_token=_bearer_token(),
        oauth_client_secret=_oauth_client_secret(),
        oauth_token_url=_oauth_token_url(),
        oauth_resource=_oauth_resource(),
        client_key=_client_key(),
        client_scopes=_client_scopes(),
        timeout=args.request_timeout,
    )
    check_api_health(client, report)
    check_runtime_dependencies(client, report, args, kube=kube)
    resolve_tenant_identity(client, report, args)
    if not any(alert["code"].startswith("runtime_dependencies_") for alert in report["alerts"]):
        check_memory_write(client, report, args)
    else:
        _record_check(report, "memory_write", "skipped", reason="runtime dependencies unavailable")
    asyncio.run(check_sentinel(report))
    check_mcp_reachable(args.mcp_url, report, timeout=args.request_timeout)
    check_kubernetes(report, args, kube=kube)
    report["status"] = "passed" if not report["alerts"] else "failed"
    report["alert_count"] = len(report["alerts"])
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run post-sync Palace memory dependency smoke checks.")
    parser.add_argument("--target-name", default=os.getenv("PALACE_ROLLOUT_SMOKE_TARGET", "palaceoftruth"))
    parser.add_argument("--namespace", default=os.getenv("POD_NAMESPACE"))
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--api-base-url", default=os.getenv("PALACEOFTRUTH_API_BASE_URL", "http://localhost:8000/api/v1"))
    parser.add_argument("--mcp-url", default=os.getenv("PALACEOFTRUTH_MCP_URL"))
    parser.add_argument("--scope-key", default=os.getenv("PALACE_ROLLOUT_SMOKE_SCOPE_KEY", "rollout-smoke"))
    parser.add_argument("--request-timeout", type=float, default=10.0)
    parser.add_argument("--dependency-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--dependency-interval-seconds", type=float, default=5.0)
    parser.add_argument("--job-interval-seconds", type=float, default=5.0)
    parser.add_argument("--job-timeout-seconds", type=float, default=180.0)
    parser.add_argument("--job-list-limit", type=int, default=20)
    parser.add_argument("--expected-auth-mode", default="")
    parser.add_argument("--expected-tenant-id", default="")
    parser.add_argument("--expected-client-key", default="")
    parser.add_argument("--expected-scope", action="append", default=[])
    parser.add_argument("--pod-label-selector", default="app.kubernetes.io/name=palaceoftruth")
    parser.add_argument("--worker-name-fragment", default="worker")
    parser.add_argument("--restart-alert-threshold", type=int, default=3)
    parser.add_argument("--log-since-seconds", type=int, default=3600)
    parser.add_argument("--log-tail-lines", type=int, default=500)
    parser.add_argument("--skip-log-scan", action="store_true")
    parser.add_argument("--skip-kubernetes", action="store_true")
    parser.add_argument("--output", default=None)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    report = build_report(args)
    output = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(output)
    else:
        print(output, end="")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
