"""Pre-upgrade (held, declared-new) artifact binding — strict read-only contract.

Fixtures only: no cluster, no secrets, no real images, no network. Proves that
the separate PRE-UPGRADE lane binds the RUNNING runtime to the ACTUAL INSTALLED
artifact (the max attempted Helm release manifest) and the DECLARED target to the
frozen, digest-verified action tuple, while the AUTOMATIC post-upgrade lane stays
strict. No image-ignore path exists.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import json
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
RELEASE = "palace-sarvent"
NS = "palace-sarvent"
UID = "release-uid-623"
TARGET = "sha256:" + "1" * 64          # declared NEW backend digest
CHART = "sha256:" + "2" * 64           # declared chart OCI digest
WORKER = "sha256:" + "3" * 64
FRONTEND = "sha256:" + "4" * 64
INSTALLED = "sha256:" + "a" * 64       # running/installed OLD backend digest
INSTALLED_IMG = "ghcr.io/x/backend@" + INSTALLED
TARGET_IMG = "ghcr.io/x/backend@" + TARGET


def _now():
    return smoke.utc_now()


def _digest(tag):
    return tag


def _encode_release(version, backend_image, mcp_image):
    """Encode a Helm v3 release Secret payload exactly as Helm stores it."""
    manifest = (
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: palace-sarvent-backend\n"
        "spec:\n  template:\n    spec:\n      containers:\n"
        f"      - name: backend\n        image: {backend_image}\n"
        "---\n"
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: palace-sarvent-mcp\n"
        "spec:\n  template:\n    spec:\n      containers:\n"
        f"      - name: mcp\n        image: {mcp_image}\n"
    )
    rel = {"version": version, "chart": {"metadata": {"version": "0.1.621"}}, "manifest": manifest}
    inner = base64.b64encode(gzip.compress(json.dumps(rel).encode()))
    return base64.b64encode(inner).decode()


def _hr(*, suspend=True, backend=TARGET, worker=WORKER, frontend=FRONTEND,
        uid=UID, chart_ref=("OCIRepository", "palaceoftruth-ghcr", "flux-system"),
        versions=(471, 472, 477)):
    return {
        "metadata": {"uid": uid, "name": RELEASE, "namespace": NS},
        "spec": {
            "suspend": suspend,
            "values": {"image": {"backendDigest": backend, "workerDigest": worker,
                                 "frontendDigest": frontend},
                       "highAvailability": {"replicas": {"backend": 2, "mcp": 1}}},
            "chartRef": {"kind": chart_ref[0], "name": chart_ref[1], "namespace": chart_ref[2]},
        },
        "status": {"history": [{"version": v, "status": "failed" if v == 477 else "deployed"}
                               for v in versions]},
    }


def _oci(digest=CHART):
    return {"spec": {"ref": {"digest": digest}}}


class Kube:
    """Minimal fixture adapter for the pre-upgrade binding reads."""

    def __init__(self, *, hr=None, oci=None, secret=None, pods=None, ns=NS,
                 secret_versions=(471, 472, 477)):
        self.hr = hr if hr is not None else _hr()
        self.oci = oci if oci is not None else _oci()
        self.secret = secret
        self.pods = pods if pods is not None else _pods()
        self.ns = ns
        self.secret_versions = secret_versions

    def _secret_items(self):
        return [{"metadata": {"name": "sh.helm.release.v1." + RELEASE + ".v" + str(v)}}
                for v in self.secret_versions]

    def get(self, path, query=None):
        if path.endswith("/pods"):
            return {"items": self.pods}
        if path.endswith("/secrets"):
            return {"items": self._secret_items()}
        if "/helmreleases/" in path:
            return self.hr
        if "/ocirepositories/" in path:
            return self.oci
        if "/secrets/" in path:
            if self.secret is None:
                raise control.ControlError("secret missing")
            return self.secret
        if "/deployments/" in path:
            name = path.rsplit("/", 1)[1]
            return {"metadata": {"uid": "dep-" + name, "name": name}}
        if path.endswith("/replicasets"):
            items = []
            for name in ("backend", "mcp"):
                dep = RELEASE + "-" + name
                items.append({"metadata": {"name": "rs-" + dep, "uid": "rs-" + dep,
                                           "ownerReferences": [{"kind": "Deployment",
                                                                "controller": True,
                                                                "uid": "dep-" + dep}]}})
            return {"items": items}
        return {"metadata": {"uid": "u"}}


def _pod(name, container, image, *, uid=None, owner="rs", ready=True):
    return {
        "metadata": {"name": name, "uid": uid or ("uid-" + name), "namespace": NS,
                     "labels": {"app.kubernetes.io/instance": RELEASE, "app": container},
                     "ownerReferences": [{"kind": "ReplicaSet", "controller": True,
                                          "name": owner, "uid": owner}]},
        "status": {"phase": "Running",
                   "containerStatuses": [{"name": container, "ready": ready,
                                          "containerID": "cid-" + name,
                                          "imageID": image,
                                          "state": {"running": {"startedAt": (_now() - timedelta(days=2)).isoformat()}},
                                          "lastState": {}}]},
    }


def _pods(image=INSTALLED_IMG):
    return [_pod("backend-a", "backend", image, owner="rs-" + RELEASE + "-backend"),
            _pod("backend-b", "backend", image, owner="rs-" + RELEASE + "-backend"),
            _pod("mcp-a", "mcp", image, owner="rs-" + RELEASE + "-mcp")]


def _secret(version=477, backend=INSTALLED_IMG, mcp=INSTALLED_IMG):
    return {"data": {"release": _encode_release(version, backend, mcp)}}


def _args():
    return argparse.Namespace(namespace=NS, target_name=RELEASE,
                              api_base_url="https://fixture.invalid/api/v1",
                              mcp_url="https://fixture.invalid/mcp",
                              request_timeout=10.0, scope_key="rollout-smoke",
                              pod_label_selector="app.kubernetes.io/instance=" + RELEASE,
                              restart_alert_threshold=3, log_since_seconds=3600,
                              log_tail_lines=1000)


def _manifest(**over):
    now = _now()
    doc = {
        "schema_version": 1, "action_id": "fixture-action",
        "namespace": NS, "release_name": RELEASE, "release_uid": UID,
        "target_digest": TARGET, "chart_digest": CHART, "expected_max_revision": 477,
        "starts_at": (now - timedelta(seconds=600)).isoformat(),
        "expires_at": (now + timedelta(seconds=600)).isoformat(),
    }
    doc.update(over)
    return control.PreUpgradeManifest(doc, control.sha256_json(doc), now=now)


def _run(kube, manifest=None):
    report = {"checks": [], "alerts": []}
    preflight.pre_upgrade_artifact_binding(smoke, None, report, _args(), kube, manifest or _manifest())
    return report


# --- positive: declared-new, running-installed, held, max revision frozen ------

def test_pre_upgrade_binds_installed_artifact_and_declared_target():
    report = _run(Kube(secret=_secret()))
    row = next(c for c in report["checks"] if c["name"] == "artifact_binding")
    assert row["status"] == "passed"
    assert row["installed_revision"] == 477
    assert row["declared_target_digest"] == TARGET
    assert row["installed_pins"]["backend"].endswith("@" + INSTALLED)
    assert sum(len(v) for v in row["observations"].values()) == 3


def test_pre_upgrade_does_not_require_release_ready():
    """The max revision is FAILED (partial apply); a held window is still Green."""
    hr = _hr(versions=(471, 472, 477))
    assert hr["status"]["history"][-1]["status"] == "failed"
    report = _run(Kube(hr=hr, secret=_secret()))
    assert report["checks"][0]["name"] == "artifact_binding"


# --- negative: declared target drift -----------------------------------------

def test_declared_image_that_differs_from_approved_target_fails():
    with pytest.raises(control.ControlError):
        _run(Kube(hr=_hr(backend=INSTALLED), secret=_secret()))


def test_declared_chart_oci_that_differs_from_approved_target_fails():
    with pytest.raises(control.ControlError):
        _run(Kube(oci=_oci("sha256:" + "9" * 64), secret=_secret()))


def test_incomplete_declared_image_pin_tuple_fails():
    with pytest.raises(control.ControlError):
        _run(Kube(hr=_hr(worker=""), secret=_secret()))


def test_release_uid_drift_fails():
    with pytest.raises(control.ControlError):
        _run(Kube(hr=_hr(uid="other-uid"), secret=_secret()))


def test_release_not_suspended_fails():
    with pytest.raises(control.ControlError):
        _run(Kube(hr=_hr(suspend=False), secret=_secret()))


def test_latest_revision_newer_than_approved_max_fails():
    """A new attempt (e.g. 478) since the frozen window must abort."""
    with pytest.raises(control.ControlError):
        _run(Kube(hr=_hr(versions=(471, 477, 478)), secret=_secret(),
                  secret_versions=(471, 472, 477, 478)))


# --- negative: installed artifact unprovable / running drift ------------------

def test_running_declared_target_instead_of_installed_fails():
    """Declared-new running (the pre-upgrade reality is installed-old): a drifted
    pod that already runs the declared target is NOT the installed artifact."""
    with pytest.raises(control.ControlError):
        _run(Kube(secret=_secret(), pods=_pods(TARGET_IMG)))


def test_missing_installed_release_secret_fails_closed():
    with pytest.raises(control.ControlError):
        _run(Kube(secret=None))


def test_unparsable_installed_release_secret_fails_closed():
    with pytest.raises(control.ControlError):
        _run(Kube(secret={"data": {"release": "not-base64!!"}}))


def test_installed_manifest_image_not_digest_pinned_fails_closed():
    with pytest.raises(control.ControlError):
        _run(Kube(secret=_secret(backend="ghcr.io/x/backend:0.1.621")))


def test_installed_manifest_missing_revision_mismatch_fails():
    with pytest.raises(control.ControlError):
        _run(Kube(secret=_secret(version=470)))


def test_missing_replica_fails():
    pods = _pods()[:2]   # one backend missing
    with pytest.raises(control.ControlError):
        _run(Kube(secret=_secret(), pods=pods))


def test_foreign_replicaset_owner_fails():
    pods = _pods()
    pods[0]["metadata"]["ownerReferences"][0]["uid"] = "rs-foreign"
    with pytest.raises(control.ControlError):
        _run(Kube(secret=_secret(), pods=pods))


# --- the AUTOMATIC post-upgrade lane is unchanged (no relaxation) ------------

def test_post_upgrade_lane_still_rejects_declared_vs_running_drift():
    """health_runtime_binding binds DECLARED==RUNNING; it must NOT accept the
    pre-upgrade declared-new/running-old state."""
    report = {"checks": [], "alerts": []}
    with pytest.raises(control.ControlError):
        preflight.health_runtime_binding(smoke, None, report, _args(), Kube(secret=_secret()))


# --- the frozen tuple itself is digest-verified, not an arbitrary CLI arg -----

def test_manifest_digest_mismatch_fails():
    doc = {"schema_version": 1, "action_id": "a", "namespace": NS, "release_name": RELEASE,
           "release_uid": UID, "target_digest": TARGET, "chart_digest": CHART,
           "expected_max_revision": 477,
           "starts_at": (_now() - timedelta(seconds=600)).isoformat(),
           "expires_at": (_now() + timedelta(seconds=600)).isoformat()}
    with pytest.raises(control.ControlError):
        control.PreUpgradeManifest(doc, "0" * 64, now=_now())


def test_manifest_rejects_unknown_fields():
    doc = {"schema_version": 1, "action_id": "a", "namespace": NS, "release_name": RELEASE,
           "release_uid": UID, "target_digest": TARGET, "chart_digest": CHART,
           "expected_max_revision": 477,
           "starts_at": (_now() - timedelta(seconds=600)).isoformat(),
           "expires_at": (_now() + timedelta(seconds=600)).isoformat(),
           "expected_digest": "sha256:" + "0" * 64}
    with pytest.raises(control.ControlError):
        control.PreUpgradeManifest(doc, control.sha256_json(doc), now=_now())


def test_manifest_outside_window_fails():
    with pytest.raises(control.ControlError):
        _manifest(starts_at=(_now() + timedelta(seconds=600)).isoformat(),
                  expires_at=(_now() + timedelta(seconds=1200)).isoformat())
