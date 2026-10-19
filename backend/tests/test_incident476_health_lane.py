"""incident476 health lane — strict read-only contract (PR573 + C1-C5 repairs).

Offline only: a fake kube adapter, no cluster, no secrets, no POST.

The lane passes ONLY on a settled rollout and fails on every weakness the
independent review reproduced: C1 missing expected replica, C2 wrong image,
C3 owner forgery, C4 stale/empty/truncated coverage, C5 real alert evidence.
"""
from __future__ import annotations

import argparse
import asyncio
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

def _idle_pulse(present=True, ttl=9, qlen=0, queue="arq:queue:media", pod="media-worker-1"):
    def broker(queue_name, pod_name):
        return {"present": present, "ttl_seconds": ttl, "queue": queue, "pod": pod,
                "queue_length": qlen, "as_of": _now().isoformat()}
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


def test_C6_empty_worker_stable_counter_empty_laststate_old_container_passes():
    """A non-zero counter with EMPTY lastState is admitted only with the
    continuous-container proof: the live container started at/before the window
    floor and the counter is stable across the two passes. The count's history is
    unprovable, but the WINDOW is provably restart-free."""
    kube = FakeKube([_idle_worker_pod(restart_count=3)], lines={"media-worker-1": ""})
    out = preflight.health_temporal(smoke, _args(), kube, broker=_idle_pulse())
    assert out["passes"] == 2
    assert out["historical_restarts"][0]["restart_count"] == 3


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


# --- C7: the TTL must be a RAW, refreshing value (1..45), never normalized -----
#
# Redis TTL semantics: -2 = key missing, -1 = exists with NO expiry (NOT being
# refreshed), 0 = expires immediately, >= 1 = seconds remaining. A persistent or
# expired key must NEVER be clamped into the acceptable range. Covered at BOTH
# layers: the probe's raw value path AND the canonical checker, plus end to end.

def _pulse(queue="arq:queue:media", pod="media-worker-1", ttl=9, present=True, as_of=None):
    return {"present": present, "ttl_seconds": ttl, "queue": queue, "pod": pod,
            "queue_length": 0, "as_of": as_of or _now().isoformat()}


@pytest.mark.parametrize("raw_ttl,ok", [(-2, False), (-1, False), (0, False), (1, True), (45, True), (46, False)])
def test_C7_checker_accepts_only_raw_ttl_in_range(raw_ttl, ok):
    now = _now()
    proof = preflight._broker_heartbeat_proof(lambda q, p: _pulse(ttl=raw_ttl),
                                              "arq:queue:media", "media-worker-1", now)
    assert (proof is not None) is ok
    if ok:
        assert proof["ttl_seconds"] == raw_ttl          # seen raw, not clamped


def test_C7_checker_rejects_bool_and_non_int_ttl():
    now = _now()
    for bad in (True, False, "9", 9.0, None):
        assert preflight._broker_heartbeat_proof(lambda q, p: _pulse(ttl=bad),
                                                 "arq:queue:media", "media-worker-1", now) is None


def test_C7_checker_rejects_mismatched_queue_or_pod():
    now = _now()
    assert preflight._broker_heartbeat_proof(lambda q, p: _pulse(queue="arq:queue"),
                                             "arq:queue:media", "media-worker-1", now) is None
    assert preflight._broker_heartbeat_proof(lambda q, p: _pulse(pod="other-pod"),
                                             "arq:queue:media", "media-worker-1", now) is None


def test_C7_checker_rejects_stale_or_future_caller_stamp():
    now = _now()
    stale = (now - timedelta(seconds=600)).isoformat()
    future = (now + timedelta(seconds=600)).isoformat()
    assert preflight._broker_heartbeat_proof(lambda q, p: _pulse(as_of=stale),
                                             "arq:queue:media", "media-worker-1", now) is None
    assert preflight._broker_heartbeat_proof(lambda q, p: _pulse(as_of=future),
                                             "arq:queue:media", "media-worker-1", now) is None


class _FakeRedis:
    def __init__(self, ttl, qlen=0):
        self._ttl, self._qlen = ttl, qlen

    async def ttl(self, key):
        return self._ttl

    async def llen(self, key):
        return self._qlen

    async def aclose(self):
        return None


class _FakeSentinel:
    def __init__(self, *a, **k):
        pass

    async def discover_master(self, name):
        return ("127.0.0.1", 6379)

    async def aclose(self):
        return None


class _FakeConfig:
    hosts = [("127.0.0.1", 26379)]
    master_name = "mymaster"


@pytest.mark.parametrize("raw_ttl", [-2, -1, 0, 1, 45, 46])
def test_C7_probe_returns_raw_ttl_without_normalization(monkeypatch, raw_ttl):
    monkeypatch.setattr(smoke, "load_config_from_env", lambda: _FakeConfig())
    monkeypatch.setattr(smoke, "Sentinel", lambda *a, **k: _FakeSentinel())
    monkeypatch.setattr(smoke, "Redis", lambda *a, **k: _FakeRedis(raw_ttl))
    monkeypatch.setattr(smoke, "redis_auth_kwargs", lambda: {})
    pulse = asyncio.run(smoke._broker_pulse_async("arq:queue:media", "media-worker-1", timeout=1.0))
    assert pulse["ttl_seconds"] == raw_ttl                  # NEVER clamped
    assert pulse["present"] is (raw_ttl != -2)
    assert pulse["queue"] == "arq:queue:media" and pulse["pod"] == "media-worker-1"


@pytest.mark.parametrize("raw_ttl,ok", [(-1, False), (-2, False), (0, False), (1, True), (45, True), (46, False)])
def test_C7_temporal_end_to_end_ttl_matrix(raw_ttl, ok):
    """A persistent (-1), expired (-2), imminent (0), or stale (>45) key fails."""
    kube = FakeKube([_idle_worker_pod()], lines={"media-worker-1": ""})
    broker = _idle_pulse(ttl=raw_ttl, present=(raw_ttl != -2))
    if ok:
        out = preflight.health_temporal(smoke, _args(), kube, broker=broker)
        assert out["coverage"][0]["coverage_kind"] == "state_and_events_for_idle_worker"
    else:
        with pytest.raises(control.ControlError):
            preflight.health_temporal(smoke, _args(), kube, broker=broker)


# --- C8: windowed restart-counter semantics (semantic repair) ----------------
#
# The restart counter is scoped to the CURRENT observation window, not to the
# pod's whole life. An at/over-threshold counter is admitted ONLY with proof it
# is historical: the most recent termination (lastState.finishedAt) is OUTSIDE
# the window, the live container has run across the WHOLE window
# (state.running.startedAt <= floor), the termination is coherent with that start
# (startedAt > finishedAt), and the counter does NOT move between the two passes
# (a delta of even 1 is an active restart). Threshold 3 and the >=3600s window are
# preserved: this is not a threshold raise and not a label-capture bypass.
#
# RED (pre-repair): the counter test was `count >= threshold` UNCONDITIONALLY, so
# test_C8_historical_counter_outside_window_passes failed. GREEN: it passes with
# the full proof; every unprovable or contemporary case still fails closed.

def _old_term(age_s=7300, reason="Error", exit_code=1):
    """A termination strictly OLDER than the 7200s window (floor = now-7200)."""
    return {"reason": reason, "exitCode": exit_code,
            "finishedAt": (_now() - timedelta(seconds=age_s)).isoformat()}


class _StepKube(FakeKube):
    """Return a DIFFERENT pod snapshot on each successive /pods read.

    health_temporal reads the worker pod set twice (two passes); this feeds the
    first snapshot to pass one and the second snapshot to pass two so the
    two-pass delta/identity guards can be exercised deterministically.
    """

    def __init__(self, first, second, **kw):
        super().__init__(first, **kw)
        self._snaps = [first, second]
        self._reads = 0

    def get(self, path, query=None):
        if path.endswith("/pods"):
            snap = self._snaps[min(self._reads, len(self._snaps) - 1)]
            self._reads += 1
            return {"items": snap}
        return super().get(path, query)


def test_C8_historical_counter_outside_window_passes():
    """A stable counter of 10 whose container has run since BEFORE the window
    floor, with its last termination older still, is historical, not an active
    fault. The live container start must postdate that termination yet precede
    the floor (floor = now-7200)."""
    pod = _pod("worker-1", "worker", started_ago_s=7250, restart_count=10,
               last_terminated=_old_term(7300))
    out = preflight.health_temporal(smoke, _args(), FakeKube([pod]))
    assert out["passes"] == 2
    assert out["historical_restarts"][0]["restart_count"] == 10
    assert out["historical_restarts"][0]["pod"] == "worker-1"
    assert out["lookback_start"]


def test_C8_stable_counter_empty_laststate_old_container_passes():
    """The live PR #623 case: restartCount>0 with EMPTY lastState but a container
    that has run continuously across the whole window. The counter's history is
    unprovable, yet the WINDOW is provably restart-free (start before floor,
    stable across both passes), so it is not a current fault."""
    pod = _pod("worker-1", "worker", started_ago_s=10800, restart_count=10)
    out = preflight.health_temporal(smoke, _args(), FakeKube([pod]))
    assert out["passes"] == 2
    assert out["historical_restarts"][0]["restart_count"] == 10
    assert out["historical_restarts"][0]["last_terminated_at"] is None


def test_C8_empty_laststate_started_inside_window_fails():
    pod = _pod("worker-1", "worker", started_ago_s=600, restart_count=10)
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(), FakeKube([pod]))


def test_C8_empty_laststate_missing_start_fails():
    pod = _pod("worker-1", "worker", restart_count=10)
    del pod["status"]["containerStatuses"][0]["state"]["running"]["startedAt"]
    fresh = (_now() - timedelta(seconds=20)).isoformat()
    kube = FakeKube([pod], lines={"worker-1": f"{fresh} line\n"})
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(), kube)


def test_C8_empty_laststate_future_start_fails():
    future = (_now() + timedelta(seconds=3600)).isoformat()
    pod = _pod("worker-1", "worker", restart_count=10)
    pod["status"]["containerStatuses"][0]["state"]["running"]["startedAt"] = future
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(), FakeKube([pod]))


def test_C8_empty_laststate_delta_during_observation_fails():
    p1 = _pod("worker-1", "worker", started_ago_s=10800, restart_count=3)
    p2 = _pod("worker-1", "worker", started_ago_s=10800, restart_count=4)
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(), _StepKube([p1], [p2]))


def test_C8_empty_laststate_uid_changed_fails():
    p1 = _pod("worker-1", "worker", started_ago_s=10800, restart_count=3)
    p2 = _pod("worker-1", "worker", started_ago_s=10800, restart_count=3)
    p2["metadata"]["uid"] = "uid-worker-1-replaced"
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(), _StepKube([p1], [p2]))


def test_C8_recent_termination_at_threshold_fails():
    """A termination inside the window is a current fault even at the threshold."""
    pod = _pod("worker-1", "worker", started_ago_s=10800, restart_count=3,
               last_terminated={"reason": "Error", "exitCode": 1,
                                "finishedAt": (_now() - timedelta(seconds=60)).isoformat()})
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(), FakeKube([pod]))


def test_C8_recent_oom_termination_fails():
    """A recent OOM is a current fault, independent of the historical allowance."""
    term = {"reason": "OOMKilled", "exitCode": 137,
            "finishedAt": (_now() - timedelta(seconds=60)).isoformat()}
    pod = _pod("worker-1", "worker", started_ago_s=10800, restart_count=10, last_terminated=term)
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(), FakeKube([pod]))


def test_C8_future_termination_fails():
    """A future finishedAt is never proof."""
    pod = _pod("worker-1", "worker", started_ago_s=10800, restart_count=10,
               last_terminated={"reason": "Error", "exitCode": 1,
                                "finishedAt": (_now() + timedelta(seconds=3600)).isoformat()})
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(), FakeKube([pod]))


def test_C8_container_started_inside_window_fails():
    """Old termination but the live container began INSIDE the window: the whole
    window cannot be proven restart-free, so the historical scope is refused."""
    pod = _pod("worker-1", "worker", started_ago_s=600, restart_count=10,
               last_terminated=_old_term(7300))
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(), FakeKube([pod]))


def test_C8_incoherent_start_before_termination_fails():
    """startedAt <= finishedAt is incoherent metadata: fail closed."""
    pod = _pod("worker-1", "worker", started_ago_s=7300, restart_count=10,
               last_terminated={"reason": "Error", "exitCode": 1,
                                "finishedAt": (_now() - timedelta(seconds=7200)).isoformat()})
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(), FakeKube([pod]))


def test_C8_missing_start_metadata_for_historical_counter_fails():
    """A historical counter with no container start metadata cannot be scoped."""
    pod = _pod("worker-1", "worker", restart_count=10, last_terminated=_old_term(7300))
    del pod["status"]["containerStatuses"][0]["state"]["running"]["startedAt"]
    fresh = (_now() - timedelta(seconds=20)).isoformat()
    kube = FakeKube([pod], lines={"worker-1": f"{fresh} line\n"})
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(), kube)


def test_C8_fresh_restart_delta_during_observation_fails():
    """A counter that advances by 1 between the two passes is an active restart,
    even though each pass alone looks historical."""
    p1 = _pod("worker-1", "worker", started_ago_s=10800, restart_count=3, last_terminated=_old_term(7300))
    p2 = _pod("worker-1", "worker", started_ago_s=10800, restart_count=4, last_terminated=_old_term(7300))
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(), _StepKube([p1], [p2]))


def test_C8_uid_changed_during_observation_fails():
    p1 = _pod("worker-1", "worker", started_ago_s=240, restart_count=0)
    p2 = _pod("worker-1", "worker", started_ago_s=240, restart_count=0)
    p2["metadata"]["uid"] = "uid-worker-1-replaced"
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(), _StepKube([p1], [p2]))


def test_C8_container_id_changed_during_observation_fails():
    p1 = _pod("worker-1", "worker", started_ago_s=240, restart_count=0)
    p2 = _pod("worker-1", "worker", started_ago_s=240, restart_count=0)
    p2["status"]["containerStatuses"][0]["containerID"] = "cid-worker-1-replaced"
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(), _StepKube([p1], [p2]))


class _EventFailKube(FakeKube):
    """A kube whose event query fails (cap/partial query is not proof)."""

    def get(self, path, query=None):
        if path.endswith("/events"):
            raise RuntimeError("event query unavailable")
        return super().get(path, query)


def test_C8_partial_event_query_is_not_proof():
    with pytest.raises(control.ControlError):
        preflight.health_kubernetes_alerts(smoke, _args(), _EventFailKube(_healthy_pods()), _now())


def test_C8_weaker_threshold_or_window_is_still_forbidden():
    """The windowed scope does NOT relax the required mode: threshold 3 and a
    >=3600s window remain mandatory."""
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(restart_alert_threshold=5), FakeKube([_pod("worker-1", "worker")]))
    with pytest.raises(control.ControlError):
        preflight.health_temporal(smoke, _args(log_since_seconds=600), FakeKube([_pod("worker-1", "worker")]))
