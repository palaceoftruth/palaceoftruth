"""incident476 RED/GREEN — health lane gates must pass on a SETTLED rollout.

RED on current main: (1) artifact_binding counts Job-owned hook pods (incl. the
smoke pod itself, never Ready) among the digest-matching pods and then demands
all of them Ready -> self-defeating. (2) baseline_temporal requires log coverage
back to now - log_since_seconds, impossible for a pod that rolled recently.

GREEN after the fix: hook/Job pods are excluded from the workload artifact
binding (the exact backend digest on workload-owned pods is still required), and
the temporal coverage floor is the container's own start time (bounded), not the
full log window.

Offline only: fake kube adapter; no cluster, no secrets, no POST.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import timedelta
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# Import the real modules (shared, no sys.modules clobbering) so this file does
# not collide with other test modules that also load them.
import check_memory_rollout_smoke as smoke  # noqa: E402

preflight = smoke.smoke_preflight

BACKEND = "sha256:" + "b" * 64
NOW = smoke.utc_now()


def _container(name, digest, ready):
    return {"name": name, "ready": ready, "containerID": "cid-" + name,
            "restartCount": 0, "state": {"running": {"startedAt": NOW.isoformat()}},
            "lastState": {}, "imageID": "ghcr.io/x/" + name + "@" + digest}


def _pod(name, *, owner_kind, owner_name, digest, ready, started_ago_s=240):
    started = (NOW - timedelta(seconds=started_ago_s)).isoformat()
    return {"metadata": {"name": name, "uid": "uid-" + name, "namespace": "palace-sarvent",
                         "labels": {"app.kubernetes.io/instance": "palace-sarvent", "app": name},
                         "ownerReferences": [{"kind": owner_kind, "name": owner_name}]},
            "status": {"phase": "Running",
                       "containerStatuses": [dict(_container(name, digest, ready),
                                                  state={"running": {"startedAt": started}})]}}


class FakeKube:
    def __init__(self, pods):
        self.pods = pods

    def get(self, path, query=None):
        if path.endswith("/pods"):
            return {"items": self.pods}
        if "helmreleases" in path:
            return {"metadata": {"uid": "rel"},
                    "spec": {"suspend": True, "values": {"image": {"backendDigest": BACKEND}}},
                    "status": {"history": [{"ociDigest": "sha256:" + "c" * 64}]}}
        return {"metadata": {"uid": "u"}}

    def get_text(self, path, query=None):
        pod = path.split("/pods/")[1].split("/log")[0]
        started = next(p for p in self.pods if p["metadata"]["name"] == pod)["status"]["containerStatuses"][0]
        start = started["state"]["running"]["startedAt"]
        return f"{start} started\n{NOW.isoformat()} healthy\n"


def _args():
    import argparse
    return argparse.Namespace(namespace="palace-sarvent", target_name="palace-sarvent",
                              worker_name_fragment="worker", log_tail_lines=3000,
                              log_since_seconds=7200, restart_alert_threshold=3,
                              pod_label_selector="app.kubernetes.io/instance=palace-sarvent")


def test_artifact_binding_ignores_job_hook_pods():
    """The smoke/RLS hook pods run the backend image but are never 'Ready';
    they must not defeat the workload artifact binding."""
    pods = [
        _pod("backend-1", owner_kind="ReplicaSet", owner_name="backend-rs", digest=BACKEND, ready=True),
        _pod("mcp-1", owner_kind="ReplicaSet", owner_name="mcp-rs", digest=BACKEND, ready=True),
        _pod("memory-smoke-1", owner_kind="Job", owner_name="memory-smoke", digest=BACKEND, ready=False),
        _pod("tenant-rls-1", owner_kind="Job", owner_name="tenant-rls", digest=BACKEND, ready=False),
    ]
    report = {"checks": [], "alerts": []}
    preflight.health_runtime_binding(smoke, None, report, _args(), FakeKube(pods))
    row = next(c for c in report["checks"] if c["name"] == "artifact_binding")
    assert row["status"] == "passed"


def test_temporal_uses_container_start_as_coverage_floor():
    """A pod that rolled minutes ago cannot have log_since_seconds of history;
    coverage is measured from its own start, not now - log_since_seconds."""
    pods = [_pod("worker-1", owner_kind="ReplicaSet", owner_name="worker-rs",
                 digest="sha256:" + "w" * 64, ready=True, started_ago_s=240)]
    out = preflight.health_temporal(smoke, _args(), FakeKube(pods))
    assert out["passes"] == 2
    assert len(out["workers"]) == 1
