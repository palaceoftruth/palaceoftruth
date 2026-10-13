"""incident476 health lane — strict read-only contract (PR573 + C1-C5 repairs).

Offline only: a fake kube adapter, no cluster, no secrets, no POST.

The lane passes ONLY on a settled rollout and fails on every weakness the
independent review reproduced: C1 missing expected replica, C2 wrong image,
C3 owner forgery, C4 stale/empty/truncated coverage, C5 real alert evidence.
"""
from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import check_memory_rollout_smoke as smoke  # noqa: E402
import scripts.smoke_control as control  # noqa: E402

preflight = smoke.smoke_preflight
BACKEND = "sha256:" + "b" * 64
WRONG = "sha256:" + "e" * 64
NOW = smoke.utc_now()
REPLICAS = {"backend": 2, "mcp": 1}


def _pod(name, container, *, owner=("ReplicaSet", True), digest=BACKEND, ready=True,
         started_ago_s=240, last_terminated=None, restart_count=0):
    started = (NOW - timedelta(seconds=started_ago_s)).isoformat()
    if owner is None:
        refs = []
    else:
        kind, controller = owner
        refs = [{"kind": kind, "controller": controller, "name": name + "-rs", "uid": "rsuid-" + name}]
    return {
        "metadata": {"name": name, "uid": "uid-" + name, "namespace": "palace-sarvent",
                     "labels": {"app.kubernetes.io/instance": "palace-sarvent", "app": name},
                     "ownerReferences": refs},
        "status": {"phase": "Running",
                   "containerStatuses": [{"name": container, "ready": ready, "containerID": "cid-" + name,
                                          "restartCount": restart_count,
                                          "state": {"running": {"startedAt": started}},
                                          "lastState": ({"terminated": last_terminated} if last_terminated else {}),
                                          "imageID": "ghcr.io/x/" + name + "@" + digest}]},
    }


def _healthy_pods():
    return [_pod("backend-a", "backend"), _pod("backend-b", "backend"), _pod("mcp-a", "mcp")]


class FakeKube:
    def __init__(self, pods, *, events=None, lines=None, history=None, digest=BACKEND, replicas=REPLICAS,
                 owner_deploy=None, owner_ok=None):
        self.pods = pods
        self.owner_deploy = owner_deploy or {}
        self.owner_ok = owner_ok or {}
        self.events = events or []
        self.lines = lines or {}
        self.history = history or [{"ociDigest": "sha256:" + "c" * 64}]
        self.digest = digest
        self.replicas = replicas

    def get(self, path, query=None):
        if path.endswith("/pods"):
            return {"items": self.pods}
        if path.endswith("/events"):
            return {"items": self.events}
        if "/deployments/" in path:
            name = path.rsplit("/", 1)[1]
            return {"metadata": {"uid": "dep-" + name, "name": name}}
        if path.endswith("/replicasets"):
            # The lane labels by the RELEASE instance; the owning Deployment is
            # <release>-<container>. Derive from the pod's container name.
            items = []
            for pod in self.pods:
                cs = (pod.get("status") or {}).get("containerStatuses") or []
                if not cs:
                    continue
                deploy = "palace-sarvent-" + cs[0]["name"]
                refs = (pod.get("metadata") or {}).get("ownerReferences") or []
                if len(refs) != 1 or refs[0].get("kind") != "ReplicaSet":
                    continue
                items.append({"metadata": {"name": refs[0].get("name"), "uid": refs[0].get("uid"),
                                           "ownerReferences": ([{"kind": "Deployment", "controller": True,
                                                                 "uid": "dep-" + self.owner_deploy.get(pod["metadata"]["name"], deploy)}]
                                                               if self.owner_ok.get(pod["metadata"]["name"], True) else
                                                               [{"kind": "Deployment", "controller": True, "uid": "dep-foreign"}])}})
            return {"items": items}
        if "helmreleases" in path:
            return {"metadata": {"uid": "rel"},
                    "spec": {"suspend": True,
                             "values": {"image": {"backendDigest": self.digest},
                                        "highAvailability": {"replicas": self.replicas}}},
                    "status": {"history": self.history}}
        return {"metadata": {"uid": "u"}}

    def get_text(self, path, query=None):
        pod = path.split("/pods/")[1].split("/log")[0]
        if pod in self.lines:
            return self.lines[pod]
        # A settled pod's log reaches back to its own start and has a fresh line.
        started = next(p for p in self.pods if p["metadata"]["name"] == pod)["status"]["containerStatuses"][0]["state"]["running"]["startedAt"]
        fresh = (NOW - timedelta(seconds=30)).isoformat()
        return f"{started} started\n{fresh} heartbeat\n"


def _args(**kw):
    base = dict(namespace="palace-sarvent", target_name="palace-sarvent",
                worker_name_fragment="worker", log_tail_lines=3000,
                log_since_seconds=7200, restart_alert_threshold=3,
                pod_label_selector="app.kubernetes.io/instance=palace-sarvent")
    base.update(kw)
    return argparse.Namespace(**base)


def _binding(kube):
    report = {"checks": [], "alerts": []}
    preflight.health_runtime_binding(smoke, None, report, _args(), kube)
    return report


# --- the contract passes only on a settled rollout --------------------------

def test_settled_rollout_passes():
    report = _binding(FakeKube(_healthy_pods()))
    row = next(c for c in report["checks"] if c["name"] == "artifact_binding")
    assert row["status"] == "passed"
    assert sum(len(v) for v in row["observations"].values()) == sum(REPLICAS.values())


def test_artifact_binding_ignores_job_hook_pods():
    """Hook/Job pods run the backend image but are never Ready; they must not
    defeat the workload artifact binding."""
    pods = _healthy_pods() + [_pod("memory-smoke-1", "memory-rollout-smoke", owner=("Job", None), ready=False)]
    assert _binding(FakeKube(pods))["checks"][0]["status"] == "passed"


def test_temporal_settled_worker_passes():
    out = preflight.health_temporal(smoke, _args(), FakeKube([_pod("worker-1", "worker")]))
    assert out["passes"] == 2
    assert out["coverage"][0]["collection_complete"] is True
    assert len(out["workers"]) == 1


# --- C1: expected replica counts are required ------------------------------

def test_C1_missing_replica_fails():
    pods = [_pod("backend-a", "backend"), _pod("mcp-a", "mcp")]   # only 1 of 2 backends
    with pytest.raises(control.ControlError):
        _binding(FakeKube(pods))


def test_C1_missing_desired_count_fails():
    with pytest.raises(control.ControlError):
        _binding(FakeKube(_healthy_pods(), replicas={}))


# --- C2: a wrong image cannot hide -----------------------------------------

def test_C2_wrong_digest_workload_pod_fails():
    pods = _healthy_pods() + [_pod("backend-c", "backend", digest=WRONG)]
    with pytest.raises(control.ControlError):
        _binding(FakeKube(pods))


# --- C3: owner forgery ------------------------------------------------------

def test_C3_pod_without_owner_fails():
    with pytest.raises(control.ControlError):
        _binding(FakeKube([_pod("backend-a", "backend", owner=None)]))


def test_C3_foreign_controller_owner_fails():
    with pytest.raises(control.ControlError):
        _binding(FakeKube([_pod("backend-a", "backend", owner=("DaemonSet", True))]))


def test_E1_replicaset_not_owned_by_the_current_deployment_fails():
    """A controlled ReplicaSet that is not the Deployment's current one fails."""
    pods = _healthy_pods()
    kube = FakeKube(pods, owner_ok={"backend-a": False})
    with pytest.raises(control.ControlError):
        _binding(kube)


def test_E4_prior_failed_hook_job_backoff_is_not_a_health_blocker():
    """A prior failed hook Job's BackoffLimitExceeded must not poison the gate."""
    event = {"reason": "BackoffLimitExceeded",
             "involvedObject": {"namespace": "palace-sarvent", "kind": "Job",
                                "name": "palace-sarvent-memory-smoke-4a631ed7", "uid": "job-uid-old"},
             "lastTimestamp": (NOW - timedelta(seconds=1687)).isoformat()}
    preflight.health_kubernetes_alerts(smoke, _args(), FakeKube(_healthy_pods(), events=[event]), NOW)


def test_E3_real_oom_killing_event_fails():
    event = {"reason": "OOMKilling", "involvedObject": {"namespace": "palace-sarvent", "kind": "Pod"},
             "lastTimestamp": (NOW - timedelta(seconds=60)).isoformat()}
    with pytest.raises(control.ControlError):
        preflight.health_kubernetes_alerts(smoke, _args(), FakeKube(_healthy_pods(), events=[event]), NOW)


def test_E3_oom_killing_on_prior_job_pod_today_fails():
    """A real OOM on an earlier hook Job pod (same namespace, today) IS a failure."""
    event = {"reason": "OOMKilling",
             "involvedObject": {"namespace": "palace-sarvent", "kind": "Pod",
                                "name": "palace-sarvent-memory-smoke-43c72fbe-x", "uid": "pod-uid-old"},
             "lastTimestamp": (NOW - timedelta(seconds=600)).isoformat()}
    with pytest.raises(control.ControlError):
        preflight.health_kubernetes_alerts(smoke, _args(), FakeKube(_healthy_pods(), events=[event]), NOW)


# --- C4: coverage must be fresh up to now ----------------------------------

def test_C4_stale_last_line_fails():
    stale = (NOW - timedelta(seconds=7100)).isoformat()
    kube = FakeKube([_pod("worker-1", "worker", started_ago_s=10000)],
                    lines={"worker-1": f"{stale} only-line\n"})
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(), kube)


def test_C4_empty_logs_fail():
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(), FakeKube([_pod("worker-1", "worker")], lines={"worker-1": ""}))


def test_C4_truncated_capture_fails():
    fresh = (NOW - timedelta(seconds=30)).isoformat()
    kube = FakeKube([_pod("worker-1", "worker")],
                    lines={"worker-1": "".join(f"{fresh} line{i}\n" for i in range(3000))})
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(), kube)


# --- C5: kubernetes_alerts queries real evidence ---------------------------

def test_C5_recent_oom_event_fails():
    event = {"reason": "OOMKilled", "involvedObject": {"namespace": "palace-sarvent"},
             "lastTimestamp": (NOW - timedelta(seconds=60)).isoformat()}
    with pytest.raises(control.ControlError):
        preflight.health_kubernetes_alerts(smoke, _args(), FakeKube(_healthy_pods(), events=[event]), NOW)


def test_C5_recent_oom_termination_fails():
    term = {"reason": "OOMKilled", "exitCode": 137, "finishedAt": (NOW - timedelta(seconds=60)).isoformat()}
    pods = _healthy_pods() + [_pod("worker-1", "worker", last_terminated=term)]
    with pytest.raises(control.ControlError):
        preflight.health_kubernetes_alerts(smoke, _args(), FakeKube(pods), NOW)


def test_C5_no_recent_alert_passes():
    old = {"reason": "OOMKilled", "involvedObject": {"namespace": "palace-sarvent"},
           "lastTimestamp": (NOW - timedelta(seconds=20000)).isoformat()}
    preflight.health_kubernetes_alerts(smoke, _args(), FakeKube(_healthy_pods(), events=[old]), NOW)


def test_C5_recent_subthreshold_restart_termination_fails():
    term = {"reason": "Error", "exitCode": 1, "finishedAt": (NOW - timedelta(seconds=60)).isoformat()}
    pods = [_pod("worker-1", "worker", last_terminated=term, restart_count=1)]
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(), FakeKube(pods))
