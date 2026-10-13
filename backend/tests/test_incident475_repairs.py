"""incident475 repairs: (A) read-only health lane needs no approval/receipt;
(B) the runner must apply the canonical HelmRelease postRenderers.

Offline only: fake readers/adapters, a fixture chart, and the generated
post-renderer executed as a real subprocess. No cluster, no Helm install, no
secrets.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


smoke = _load("check_memory_rollout_smoke", SCRIPTS / "check_memory_rollout_smoke.py")
runner = _load("rollout_action_control", SCRIPTS / "rollout_action_control.py")


# --- A: read-only health lane ------------------------------------------------

class _FakeKube:
    def __init__(self, *, namespace=None, timeout=None, pods=None):
        self.budget = None
        self._pods = pods or [{"metadata": {"name": "palace-sarvent-worker-abc",
                                            "uid": "u1", "labels": {"app": "palace-sarvent-worker"}},
                               "status": {"phase": "Running",
                                          "containerStatuses": [{"name": "worker", "ready": True,
                                                                 "containerID": "c1", "restartCount": 0,
                                                                 "state": {"running": {"startedAt": smoke.utc_now().isoformat()}},
                                                                 "lastState": {}}]}}]
    def get(self, path, query=None):
        if path.endswith("/pods"):
            return {"items": self._pods}
        if "namespaces/kube-system" in path:
            return {"metadata": {"uid": "cluster-uid"}}
        if path.endswith("/pods") or "namespaces/" in path and "helmreleases" not in path:
            return {"metadata": {"uid": "ns-uid"}}
        if "helmreleases" in path:
            return {"metadata": {"uid": "rel-uid"},
                    "spec": {"suspend": True, "values": {"image": {"backendDigest": "sha256:" + "a" * 64}}},
                    "status": {"history": [{"ociDigest": "sha256:" + "b" * 64}]}}
        return {"items": []}
    def get_text(self, path, query=None):
        now = smoke.utc_now().isoformat()
        return f"{now} healthy line\n"


def _args(mode="read-only", **over):
    base = dict(mode=mode, manifest=None, manifest_sha256=None, receipt_file=None,
                namespace="palace-sarvent", target_name="palace-sarvent",
                api_base_url="https://fixture.invalid/api/v1", request_timeout=10,
                dependency_timeout_seconds=30, worker_name_fragment="worker",
                log_tail_lines=100, log_since_seconds=7200, restart_alert_threshold=3,
                expected_sentinel_pods=3, pod_label_selector="app.kubernetes.io/instance=palace-sarvent",
                scope_key="k", mcp_url="http://fixture.invalid/mcp", run_id=None,
                skip_kubernetes=False, skip_log_scan=False, control_store_root="/tmp",
                reader_budget=None)
    base.update(over)
    return argparse.Namespace(**base)


def test_read_only_lane_never_loads_an_approval(monkeypatch):
    called = {"load": 0}

    def boom(*a, **k):
        called["load"] += 1
        raise AssertionError("Approval.load must not be called in read-only mode")

    monkeypatch.setattr(smoke.smoke_control.Approval, "load", classmethod(lambda cls, *a, **k: boom()))
    monkeypatch.setattr(smoke, "HttpClient", lambda **k: type("C", (), {"base_url": "https://fixture.invalid/api/v1", "safe_transport": False, "budget": None})())
    monkeypatch.setattr(smoke, "KubernetesClient", _FakeKube)
    monkeypatch.setattr(smoke, "check_api_health", lambda client, report: smoke._record_check(report, "api_health", "passed"))
    monkeypatch.setattr(smoke, "check_runtime_dependencies", lambda client, report, args, kube=None: smoke._record_check(report, "runtime_dependencies", "passed"))
    monkeypatch.setattr(smoke, "resolve_tenant_identity", lambda client, report, args: smoke._record_check(report, "tenant_identity", "passed"))
    async def _sentinel(report):
        return smoke._record_check(report, "sentinel_valkey", "passed")
    monkeypatch.setattr(smoke, "check_sentinel", _sentinel)
    monkeypatch.setattr(smoke, "check_mcp_reachable", lambda url, report, timeout=10, budget=None: smoke._record_check(report, "mcp_health", "passed"))
    monkeypatch.setattr(smoke.smoke_preflight, "health_runtime_binding", lambda s, c, r, a, k: smoke._record_check(r, "artifact_binding", "passed"))
    monkeypatch.setattr(smoke.smoke_preflight, "health_temporal", lambda s, a, k: {"workers": [], "passes": 2})

    report = smoke.build_report(_args())
    codes = [a.get("code") for a in report.get("alerts", [])]
    assert "control_approval_failed" not in codes, report
    assert report["status"] == "passed", report
    assert called["load"] == 0


def test_read_only_lane_fails_closed_on_bad_dependencies(monkeypatch):
    monkeypatch.setattr(smoke, "HttpClient", lambda **k: type("C", (), {"base_url": "https://fixture.invalid/api/v1", "safe_transport": False, "budget": None})())
    monkeypatch.setattr(smoke, "KubernetesClient", _FakeKube)
    monkeypatch.setattr(smoke, "check_api_health", lambda client, report: smoke._record_check(report, "api_health", "passed"))
    monkeypatch.setattr(smoke, "check_runtime_dependencies", lambda client, report, args, kube=None: smoke._alert(report, "runtime_dependencies_not_ready", "dependency check failed"))
    monkeypatch.setattr(smoke, "resolve_tenant_identity", lambda client, report, args: smoke._record_check(report, "tenant_identity", "passed"))
    async def _sentinel(report):
        return smoke._record_check(report, "sentinel_valkey", "passed")
    monkeypatch.setattr(smoke, "check_sentinel", _sentinel)
    monkeypatch.setattr(smoke, "check_mcp_reachable", lambda url, report, timeout=10, budget=None: smoke._record_check(report, "mcp_health", "passed"))
    monkeypatch.setattr(smoke.smoke_preflight, "health_runtime_binding", lambda s, c, r, a, k: smoke._record_check(r, "artifact_binding", "passed"))
    monkeypatch.setattr(smoke.smoke_preflight, "health_temporal", lambda s, a, k: {"workers": [], "passes": 2})

    report = smoke.build_report(_args())
    assert report["status"] == "failed"
    assert any(a.get("code") == "runtime_dependencies_not_ready" for a in report["alerts"])


def test_bounded_mode_still_requires_the_approval(monkeypatch):
    with pytest.raises(Exception):
        smoke.smoke_control.Approval.load(None, None, now=smoke.utc_now())


# --- B: canonical post-renderer ---------------------------------------------

_CANONICAL_RELEASE = textwrap.dedent("""
    apiVersion: helm.toolkit.fluxcd.io/v2
    kind: HelmRelease
    metadata:
      name: palace-sarvent
      namespace: palace-sarvent
    spec:
      suspend: true
      postRenderers:
        - kustomize:
            patches:
              - target:
                  group: postgresql.cnpg.io
                  version: v1
                  kind: Cluster
                  name: palace-sarvent-postgres
                patch: |
                  apiVersion: postgresql.cnpg.io/v1
                  kind: Cluster
                  metadata:
                    name: palace-sarvent-postgres
                    annotations:
                      cnpg.io/fencedInstances: '["*"]'
                      cnpg.io/reconciliationLoop: disabled
                      helm.sh/resource-policy: keep
              - target:
                  group: postgresql.cnpg.io
                  version: v1
                  kind: ScheduledBackup
                  name: palace-sarvent-postgres
                patch: |
                  apiVersion: postgresql.cnpg.io/v1
                  kind: ScheduledBackup
                  metadata:
                    name: palace-sarvent-postgres
                  spec:
                    suspend: true
""").lstrip()


def test_post_renderer_is_built_and_pins_its_source(tmp_path):
    path, sha = runner.build_post_renderer(_CANONICAL_RELEASE, str(tmp_path))
    assert Path(path).exists() and os.access(path, os.X_OK)
    text = Path(path).read_text()
    assert "source_sha256=" + __import__("hashlib").sha256(_CANONICAL_RELEASE.encode()).hexdigest() in text
    assert "palace-sarvent-postgres" in text


def test_post_renderer_preserves_the_fence_and_backup_suspend(tmp_path):
    path, _ = runner.build_post_renderer(_CANONICAL_RELEASE, str(tmp_path))
    rendered = yaml.safe_dump_all([
        {"apiVersion": "postgresql.cnpg.io/v1", "kind": "Cluster",
         "metadata": {"name": "palace-sarvent-postgres", "namespace": "palace-sarvent"}, "spec": {}},
        {"apiVersion": "postgresql.cnpg.io/v1", "kind": "ScheduledBackup",
         "metadata": {"name": "palace-sarvent-postgres", "namespace": "palace-sarvent"},
         "spec": {"schedule": "0 0 3 * * *"}},
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "untouched"}, "data": {"x": "1"}},
    ])
    out = subprocess.run([sys.executable, path], input=rendered, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    docs = {(d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(out.stdout)}
    ann = docs[("Cluster", "palace-sarvent-postgres")]["metadata"]["annotations"]
    assert ann["cnpg.io/fencedInstances"] == '["*"]'
    assert ann["cnpg.io/reconciliationLoop"] == "disabled"
    assert ann["helm.sh/resource-policy"] == "keep"
    assert docs[("ScheduledBackup", "palace-sarvent-postgres")]["spec"]["suspend"] is True
    assert docs[("ConfigMap", "untouched")]["data"] == {"x": "1"}


@pytest.mark.parametrize("source", [
    _CANONICAL_RELEASE.replace("kustomize:", "patchesJson6902:"),
    _CANONICAL_RELEASE.replace("annotations:", "labels:"),
    _CANONICAL_RELEASE.replace("suspend: true", "replicas: 5"),
])
def test_post_renderer_refuses_unsupported_patch_types(tmp_path, source):
    with pytest.raises(runner.ControlError):
        runner.build_post_renderer(source, str(tmp_path))


def test_post_renderer_refuses_a_missing_target(tmp_path):
    path, _ = runner.build_post_renderer(_CANONICAL_RELEASE, str(tmp_path))
    out = subprocess.run([sys.executable, path], input="kind: ConfigMap\nmetadata:\n  name: other\n",
                         capture_output=True, text=True)
    assert out.returncode != 0  # nothing is ignored


def test_helm_argv_passes_the_post_renderer_when_set(tmp_path):
    doc, digest, _ = _manifest_fixture(tmp_path)
    m = runner.ActionManifest.load(_write(tmp_path, doc), digest, now=_now())
    assert "--post-renderer" not in m.helm_argv("/chart", "/values.yaml")
    m.post_renderer_path = "/tmp/post-renderer.py"
    argv = m.helm_argv("/chart", "/values.yaml")
    assert "--post-renderer" in argv and "/tmp/post-renderer.py" in argv


# --- shared fixture ---------------------------------------------------------

def _now():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc)


def _manifest_fixture(tmp_path):
    import hashlib
    values_text = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: v\n"
    doc = {
        "schema_version": 1, "action_id": "sar1402-postrenderer-fixture", "phase": "forward",
        "owner": {"host_id": runner.control_host_id(), "uid": os.getuid(), "owner_id": "barbara"},
        "repo": {"url": runner.CANONICAL_REPO_URL, "canonical_revision": "main@sha1:" + "a" * 40},
        "kube": {"context": "k3s-lab", "cluster_uid": "c", "api_origin": "https://k3slabcontrol:6443"},
        "namespace": "palace-sarvent", "release_name": "palace-sarvent",
        "expected_current_revision": 474,
        "chart": {"oci": runner.ALLOWED_CHART_OCI, "version": "0.1.612",
                  "manifest_digest": "sha256:" + "1" * 64, "layer_digest": "sha256:" + "2" * 64},
        "image_digest": "sha256:" + "3" * 64,
        "values": {"path": "apps/k3s-lab/palace-sarvent/release.yaml",
                   "source_sha256": hashlib.sha256(values_text.encode()).hexdigest(),
                   "digest": "sha256:" + "4" * 64},
        "config_digest": "sha256:" + "4" * 64,
        "flux_suspend": True,
        "starts_at": (_now() - __import__("datetime").timedelta(minutes=1)).isoformat(),
        "expires_at": (_now() + __import__("datetime").timedelta(minutes=30)).isoformat(),
        "deadline_seconds": 900,
    }
    return doc, runner.sha256_json(doc), values_text


def _write(tmp_path, doc):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(doc))
    return str(path)


# --- integration: helm template -> generated post-renderer -------------------

_FIXTURE_CHART = {
    "Chart.yaml": "apiVersion: v2\nname: fixture\nversion: 0.1.0\n",
    "templates/cluster.yaml": (
        "apiVersion: postgresql.cnpg.io/v1\nkind: Cluster\nmetadata:\n  name: palace-sarvent-postgres\n"
        "  namespace: {{ .Release.Namespace }}\nspec:\n  instances: 3\n"),
    "templates/backup.yaml": (
        "apiVersion: postgresql.cnpg.io/v1\nkind: ScheduledBackup\nmetadata:\n  name: palace-sarvent-postgres\n"
        "  namespace: {{ .Release.Namespace }}\nspec:\n  schedule: '0 0 3 * * *'\n"),
    "templates/cm.yaml": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: keepme\ndata:\n  a: b\n",
}


def _write_chart(tmp_path):
    chart = tmp_path / "chart"
    for rel, body in _FIXTURE_CHART.items():
        p = chart / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return chart


@pytest.mark.skipif(not __import__("shutil").which("helm"), reason="helm required")
def test_helm_template_through_generated_post_renderer_restores_the_fence(tmp_path):
    chart = _write_chart(tmp_path)
    env = dict(os.environ)
    env["PATH"] = "/home/asarver/.local/bin" + os.pathsep + env.get("PATH", "")
    base = subprocess.run(["helm", "template", "palace-sarvent", str(chart), "--namespace", "palace-sarvent"],
                          capture_output=True, text=True, env=env)
    assert base.returncode == 0, base.stderr
    without = [d for d in yaml.safe_load_all(base.stdout) if isinstance(d, dict)]

    path, sha = runner.build_post_renderer(_CANONICAL_RELEASE, str(tmp_path))
    patched = subprocess.run([sys.executable, path], input=base.stdout, capture_output=True, text=True)
    assert patched.returncode == 0, patched.stderr
    with_patch = [d for d in yaml.safe_load_all(patched.stdout) if isinstance(d, dict)]

    # same object set / count / namespaces; only the approved fields change
    key = lambda docs: sorted((d["kind"], d["metadata"]["name"], d["metadata"].get("namespace")) for d in docs)
    assert key(without) == key(with_patch)
    cluster = next(d for d in with_patch if d["kind"] == "Cluster")
    assert cluster["metadata"]["annotations"]["cnpg.io/fencedInstances"] == '["*"]'
    backup = next(d for d in with_patch if d["kind"] == "ScheduledBackup")
    assert backup["spec"]["suspend"] is True
    assert not any(d["kind"] == "Cluster" for d in without if "annotations" in d.get("metadata", {}))
