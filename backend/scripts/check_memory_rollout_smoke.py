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
    def __init__(self, *, base_url: str, api_key: str | None, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

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
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
                return HttpResult(response.status, _decode_payload(payload))
        except urllib.error.HTTPError as exc:
            return HttpResult(exc.code, _decode_payload(exc.read()))


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


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _api_key() -> str:
    return (
        os.getenv("PALACEOFTRUTH_API_KEY")
        or os.getenv("SECONDBRAIN_API_KEY")
        or os.getenv("API_KEY")
        or ""
    ).strip()


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


def resolve_tenant_identity(client: HttpClient, report: dict[str, Any]) -> str | None:
    whoami = client.request("GET", "/memory/whoami")
    if whoami.status >= 400 or not isinstance(whoami.payload, dict) or not whoami.payload.get("tenant_id"):
        _alert(report, "tenant_identity_failed", f"whoami returned HTTP {whoami.status}", response=whoami.payload)
        _record_check(report, "tenant_identity", "failed", http_status=whoami.status)
        return None
    tenant_id = str(whoami.payload["tenant_id"])
    report["tenant_id"] = tenant_id
    _record_check(report, "tenant_identity", "passed", tenant_id=tenant_id)
    return tenant_id


def check_memory_write(client: HttpClient, report: dict[str, Any], args: argparse.Namespace) -> None:
    tenant_id = report.get("tenant_id")
    if not isinstance(tenant_id, str) or not tenant_id:
        tenant_id = resolve_tenant_identity(client, report)
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
        selected_worker_pods += 1
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
    if restart_rows:
        _alert(report, "worker_restart_spike", "worker restart threshold exceeded", pods=restart_rows)
    if master_not_found_hits:
        _alert(report, "master_not_found", "MasterNotFoundError or No master found appeared in worker logs", pods=master_not_found_hits)
    if log_read_failures:
        _alert(report, "worker_log_scan_failed", "one or more worker container logs could not be scanned", pods=log_read_failures)
    failed = bool(restart_rows or master_not_found_hits or log_read_failures or selected_worker_pods == 0)
    _record_check(
        report,
        "kubernetes_alerts",
        "failed" if failed else "passed",
        pod_count=len(pods),
        selected_worker_pods=selected_worker_pods,
        selected_worker_containers=selected_worker_containers,
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
    client = HttpClient(base_url=args.api_base_url.rstrip("/"), api_key=_api_key(), timeout=args.request_timeout)
    check_api_health(client, report)
    resolve_tenant_identity(client, report)
    check_memory_write(client, report, args)
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
    parser.add_argument("--job-interval-seconds", type=float, default=5.0)
    parser.add_argument("--job-timeout-seconds", type=float, default=180.0)
    parser.add_argument("--job-list-limit", type=int, default=20)
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
