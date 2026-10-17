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
def _now():
    """Resolve the fixture clock at CALL time.

    The synthetic pod/log/event timestamps must be relative to execution time,
    not to module import time: the live freshness bound in health_temporal is
    compared against wall-clock now, so a fixture base stamped at collection
    goes stale on a suite that takes longer than the bound and fails
    test_temporal_settled_worker_passes for a harness reason, not a source one.
    """
    return smoke.utc_now()


REPLICAS = {"backend": 2, "mcp": 1}


def _pod(name, container, *, owner=("ReplicaSet", True), digest=BACKEND, ready=True,
         started_ago_s=240, last_terminated=None, restart_count=0):
    started = (_now() - timedelta(seconds=started_ago_s)).isoformat()
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
        fresh = (_now() - timedelta(seconds=30)).isoformat()
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
             "lastTimestamp": (_now() - timedelta(seconds=1687)).isoformat()}
    preflight.health_kubernetes_alerts(smoke, _args(), FakeKube(_healthy_pods(), events=[event]), _now())


def test_E3_real_oom_killing_event_fails():
    event = {"reason": "OOMKilling", "involvedObject": {"namespace": "palace-sarvent", "kind": "Pod"},
             "lastTimestamp": (_now() - timedelta(seconds=60)).isoformat()}
    with pytest.raises(control.ControlError):
        preflight.health_kubernetes_alerts(smoke, _args(), FakeKube(_healthy_pods(), events=[event]), _now())


def test_E3_oom_killing_on_prior_job_pod_today_fails():
    """A real OOM on an earlier hook Job pod (same namespace, today) IS a failure."""
    event = {"reason": "OOMKilling",
             "involvedObject": {"namespace": "palace-sarvent", "kind": "Pod",
                                "name": "palace-sarvent-memory-smoke-43c72fbe-x", "uid": "pod-uid-old"},
             "lastTimestamp": (_now() - timedelta(seconds=600)).isoformat()}
    with pytest.raises(control.ControlError):
        preflight.health_kubernetes_alerts(smoke, _args(), FakeKube(_healthy_pods(), events=[event]), _now())


# --- C4: coverage must be fresh up to now ----------------------------------

def test_C4_stale_last_line_fails():
    stale = (_now() - timedelta(seconds=7100)).isoformat()
    kube = FakeKube([_pod("worker-1", "worker", started_ago_s=10000)],
                    lines={"worker-1": f"{stale} only-line\n"})
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(), kube)


def test_C4_empty_logs_fail():
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(), FakeKube([_pod("worker-1", "worker")], lines={"worker-1": ""}))


def test_C4_truncated_capture_fails():
    fresh = (_now() - timedelta(seconds=30)).isoformat()
    kube = FakeKube([_pod("worker-1", "worker")],
                    lines={"worker-1": "".join(f"{fresh} line{i}\n" for i in range(3000))})
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(), kube)


# --- C5: kubernetes_alerts queries real evidence ---------------------------

def test_C5_recent_oom_event_fails():
    event = {"reason": "OOMKilled", "involvedObject": {"namespace": "palace-sarvent"},
             "lastTimestamp": (_now() - timedelta(seconds=60)).isoformat()}
    with pytest.raises(control.ControlError):
        preflight.health_kubernetes_alerts(smoke, _args(), FakeKube(_healthy_pods(), events=[event]), _now())


def test_C5_recent_oom_termination_fails():
    term = {"reason": "OOMKilled", "exitCode": 137, "finishedAt": (_now() - timedelta(seconds=60)).isoformat()}
    pods = _healthy_pods() + [_pod("worker-1", "worker", last_terminated=term)]
    with pytest.raises(control.ControlError):
        preflight.health_kubernetes_alerts(smoke, _args(), FakeKube(pods), _now())


def test_C5_no_recent_alert_passes():
    old = {"reason": "OOMKilled", "involvedObject": {"namespace": "palace-sarvent"},
           "lastTimestamp": (_now() - timedelta(seconds=20000)).isoformat()}
    preflight.health_kubernetes_alerts(smoke, _args(), FakeKube(_healthy_pods(), events=[old]), _now())


def test_C5_recent_subthreshold_restart_termination_fails():
    term = {"reason": "Error", "exitCode": 1, "finishedAt": (_now() - timedelta(seconds=60)).isoformat()}
    pods = [_pod("worker-1", "worker", last_terminated=term, restart_count=1)]
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(), FakeKube(pods))


# --- C4b: the coverage floor must not demand a line at an arbitrary boundary --

def test_C4_sparse_long_running_worker_resuming_after_the_window_floor_passes():
    """A worker older than the window is sampled from an arbitrary boundary; a
    sparse emitter that resumes well after it is healthy, not a false alarm."""
    first = (_now() - timedelta(seconds=6500)).isoformat()   # floor is now-7200
    last = (_now() - timedelta(seconds=20)).isoformat()
    kube = FakeKube([_pod("worker-1", "worker", started_ago_s=3 * 24 * 3600)],
                    lines={"worker-1": f"{first} resumed\n{last} heartbeat\n"})
    out = preflight.health_temporal(smoke, _args(), kube)
    assert out["coverage"][0]["collection_complete"] is True


def test_C4_container_started_inside_window_must_log_near_its_start():
    """A container that began inside the window must log near its own start."""
    first = (_now() - timedelta(seconds=100)).isoformat()    # 500s after a 600s start
    last = (_now() - timedelta(seconds=20)).isoformat()
    kube = FakeKube([_pod("worker-1", "worker", started_ago_s=600)],
                    lines={"worker-1": f"{first} late\n{last} heartbeat\n"})
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(), kube)


# --- C4c: PR #589 fail-closed edges (F1 missing start, F2 boundary equality) ---

def test_C4_missing_container_start_metadata_fails_closed():
    """Startup metadata is required; absent startedAt must fail closed, not skip
    the coverage-start guard (PR #589 F1)."""
    pod = _pod("worker-1", "worker")
    del pod["status"]["containerStatuses"][0]["state"]["running"]["startedAt"]
    fresh = (_now() - timedelta(seconds=20)).isoformat()
    kube = FakeKube([pod], lines={"worker-1": f"{fresh} line\n"})
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(), kube)


def test_C4_container_started_exactly_at_window_floor_must_log_near_its_start(monkeypatch):
    """A container whose start equals the window floor began AT the window and
    must log near its start; the strict-'|>' skip was a fail-open edge (F2)."""
    fixed = smoke.utc_now()
    monkeypatch.setattr(smoke, "utc_now", lambda: fixed)
    started_ago = 7200  # == log_since_seconds -> started == floor exactly
    first = (fixed - timedelta(seconds=started_ago - 180)).isoformat()
    last = (fixed - timedelta(seconds=20)).isoformat()
    kube = FakeKube([_pod("worker-1", "worker", started_ago_s=started_ago)],
                    lines={"worker-1": f"{first} late\n{last} heartbeat\n"})
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(), kube)


# --- C6: an idle healthy ARQ worker is covered by state + a live heartbeat ----
#
# A long-running worker whose log window is EMPTY is not automatically a failure:
# an idle media worker legitimately emits no line for hours. It is covered only
# when it is a KNOWN ARQ worker (broker queue resolvable), the container has run
# continuously across the whole window (start metadata present and <= floor), the
# container state is healthy (no termination/OOM/restart in _health_workers), and
# the pod-specific broker heartbeat is present and fresh. The coverage record then
# carries the explicit kind `state_and_events_for_idle_worker`, never `log`.

def _idle_pulse(present=True, ttl=9, qlen=0):
    def broker(queue_name, pod_name):
        return {"present": present, "ttl_seconds": ttl, "queue_length": qlen,
                "as_of": _now().isoformat()}
    return broker


def _idle_worker_pod(**kw):
    kw.setdefault("started_ago_s", 3 * 24 * 3600)
    return _pod("media-worker-1", "media-worker", **kw)


def test_C6_idle_worker_with_live_broker_heartbeat_passes():
    kube = FakeKube([_idle_worker_pod()], lines={"media-worker-1": ""})
    out = preflight.health_temporal(smoke, _args(), kube, broker=_idle_pulse())
    rec = out["coverage"][0]
    assert rec["coverage_kind"] == "state_and_events_for_idle_worker"
    assert rec["coverage_kind"] != "log"
    assert rec["collection_complete"] is True
    assert rec["pod"] == "media-worker-1"
    assert out["passes"] == 2


def test_C6_logged_worker_coverage_kind_stays_log():
    out = preflight.health_temporal(smoke, _args(), FakeKube([_pod("worker-1", "worker")]))
    assert out["coverage"][0]["coverage_kind"] == "log"


def test_C6_empty_worker_with_unavailable_broker_probe_fails_closed():
    """An absent probe is not proof: the empty capture still fails closed."""
    kube = FakeKube([_idle_worker_pod()], lines={"media-worker-1": ""})
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(), kube, broker=lambda *a: None)


def test_C6_empty_worker_with_stale_heartbeat_is_a_real_failure():
    """A stale task queue (heartbeat gone, jobs waiting) is NOT idle health."""
    kube = FakeKube([_idle_worker_pod()], lines={"media-worker-1": ""})
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(), kube, broker=_idle_pulse(present=False, qlen=5))


def test_C6_empty_non_worker_container_is_not_covered():
    """A non-worker API service with empty logs must fail, not borrow the kind."""
    kube = FakeKube([_pod("api-1", "backend", started_ago_s=3 * 24 * 3600)], lines={"api-1": ""})
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(), kube, broker=_idle_pulse())


def test_C6_empty_degenerate_tail_cap_fails_closed():
    """A zero/negative cap is not a complete capture, even if the read is empty."""
    kube = FakeKube([_idle_worker_pod()], lines={"media-worker-1": ""})
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(log_tail_lines=0), kube, broker=_idle_pulse())


def test_C6_empty_worker_started_inside_window_fails():
    """A worker that began inside the window is not a long-idle worker."""
    kube = FakeKube([_idle_worker_pod(started_ago_s=600)], lines={"media-worker-1": ""})
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(), kube, broker=_idle_pulse())


def test_C6_empty_worker_missing_start_metadata_fails():
    pod = _idle_worker_pod()
    del pod["status"]["containerStatuses"][0]["state"]["running"]["startedAt"]
    kube = FakeKube([pod], lines={"media-worker-1": ""})
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(), kube, broker=_idle_pulse())


def test_C6_empty_terminated_worker_fails():
    pod = _idle_worker_pod()
    pod["status"]["containerStatuses"][0]["state"] = {"terminated": {"exitCode": 1}}
    kube = FakeKube([pod], lines={"media-worker-1": ""})
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(), kube, broker=_idle_pulse())


def test_C6_empty_recent_oom_worker_fails():
    term = {"reason": "OOMKilled", "exitCode": 137, "finishedAt": (_now() - timedelta(seconds=60)).isoformat()}
    kube = FakeKube([_idle_worker_pod(last_terminated=term)], lines={"media-worker-1": ""})
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(), kube, broker=_idle_pulse())


def test_C6_empty_worker_at_restart_threshold_fails():
    kube = FakeKube([_idle_worker_pod(restart_count=3)], lines={"media-worker-1": ""})
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(), kube, broker=_idle_pulse())


def test_C6_empty_worker_malformed_heartbeat_ttl_fails():
    kube = FakeKube([_idle_worker_pod()], lines={"media-worker-1": ""})
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(), kube, broker=_idle_pulse(ttl=None))


def test_C6_empty_worker_future_heartbeat_stamp_fails():
    """A grossly-future heartbeat stamp is not proof (only a bounded skew is)."""
    future = {"present": True, "ttl_seconds": 9, "queue_length": 0,
              "as_of": (_now() + timedelta(seconds=3600)).isoformat()}
    kube = FakeKube([_idle_worker_pod()], lines={"media-worker-1": ""})
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(), kube, broker=lambda q, p: future)
