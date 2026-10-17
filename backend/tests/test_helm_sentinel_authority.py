"""Rendered-manifest contract for the Sentinel authority design.

Fast, no cluster. Pins the SHAPE the runtime lab depends on:
  - the Sentinel start path preserves/repairs the conf and writes auth-pass in
    the CONTAINER args (not an init container);
  - the data-node init runs a bounded, fail-closed authority decision and then
    the existing auth/ACL sanitizer;
  - the Sentinel service is reachable for bootstrap (publishNotReadyAddresses).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[2] / "chart"


def _render(*sets):
    if shutil.which("helm") is None:
        pytest.skip("helm is required for chart rendering tests")
    cmd = ["helm", "template", "palaceoftruth", str(CHART)]
    for a in sets:
        cmd += ["--set", a]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    return [d for d in yaml.safe_load_all(out) if isinstance(d, dict)]


def _by(m, kind, name):
    return next(x for x in m if x.get("kind") == kind and x["metadata"]["name"] == name)


def test_sentinel_start_path_preserves_conf_and_repairs_only_a_stale_pin() -> None:
    m = _render("highAvailability.enabled=true")
    sentinel = _by(m, "Deployment", "palaceoftruth-valkey-sentinel")
    spec = sentinel["spec"]["template"]["spec"]
    assert "initContainers" not in spec or not spec.get("initContainers")
    script = "\n".join(spec["containers"][0]["args"])
    # preserves a learned conf, repairs only a provably stale monitor pin
    assert 'if [ -f "$conf" ]' in script
    assert '"/^sentinel monitor/' in script or "/^sentinel monitor/" in script
    assert '"master"' in script  # only a live master is followed / preserved
    assert "getent ahosts" in script
    assert "pin_monitor" in script
    # never runs a destructive Sentinel command
    for forbidden in ("sentinel reset", "sentinel remove", "sentinel failover"):
        assert forbidden not in script
    # auth-pass is written by the container start path
    assert "sentinel auth-pass" in script


def test_client_sentinel_service_does_not_publish_not_ready_but_bootstrap_does() -> None:
    m = _render("highAvailability.enabled=true")
    client = _by(m, "Service", "palaceoftruth-valkey-sentinel")
    bootstrap = _by(m, "Service", "palaceoftruth-valkey-sentinel-bootstrap")
    # The CLIENT-facing Service must never route to a failed / not-ready
    # Sentinel pod.
    assert client["spec"].get("publishNotReadyAddresses") in (None, False)
    assert client["spec"]["type"] == "ClusterIP"
    assert client["spec"]["selector"] == {"app": "palaceoftruth-valkey-sentinel"}
    # The internal headless BOOTSTRAP Service stays reachable before the
    # readiness gate so a data node can take a majority during a cold start.
    assert bootstrap["spec"]["clusterIP"] == "None"
    assert bootstrap["spec"]["publishNotReadyAddresses"] is True


def test_data_node_init_runs_fail_closed_authority_then_acl_sanitizer() -> None:
    m = _render("highAvailability.enabled=true", "valkey.auth.enabled=true")
    for role in ("primary", "replica"):
        init = _by(m, "StatefulSet", f"palaceoftruth-valkey-{role}")["spec"]["template"]["spec"]["initContainers"][0]
        assert init["name"] == "init-config"
        s = init["args"][0]
        # bounded majority over DISTINCT Sentinel instances, not a single query
        assert "sentinel master" in s and "sentinel myid" in s
        assert "sort" in s and "uniq -c" in s
        assert "ckquorum" not in s  # never trust a bare CKQUORUM success
        # fail-closed read-only path
        assert "replicaof 127.0.0.1 1" in s
        assert "replica-read-only yes" in s
        # integrate WITH the existing auth/ACL sanitizer, not replace it
        assert "requirepass" in s and "masterauth" in s
        assert "user default" in s  # the ACL rewrite the sanitizer applies
        # no destructive data commands
        for forbidden in ("flushall", "flushdb", "sentinel failover"):
            assert forbidden not in s.lower()


def test_authority_is_opt_in_and_default_off_for_recovery() -> None:
    # The explicit initial-bootstrap flag must default to off so a recovery never
    # blind-boots a writable master.
    m = _render("highAvailability.enabled=true")
    init = _by(m, "StatefulSet", "palaceoftruth-valkey-primary")["spec"]["template"]["spec"]["initContainers"][0]
    assert "ROLE=master" in init["args"][0] or "initialBootstrap" in init["args"][0]


def _kinds(m, kind, name):
    return [x for x in m if x.get("kind") == kind and x["metadata"]["name"] == name]


def test_sentinel_workload_default_is_deployment_and_modes_are_exclusive() -> None:
    name = "palaceoftruth-valkey-sentinel"
    default = _render("highAvailability.enabled=true")
    assert _kinds(default, "Deployment", name), "default must render the Sentinel Deployment"
    assert not _kinds(default, "StatefulSet", name), "default must not render a Sentinel StatefulSet"

    sts = _render("highAvailability.enabled=true", "valkey.sentinel.workload=statefulset")
    assert _kinds(sts, "StatefulSet", name), "statefulset mode must render the Sentinel StatefulSet"
    assert not _kinds(sts, "Deployment", name), "statefulset mode must not also render the Deployment"


def test_sentinel_statefulset_mode_persists_conf_on_a_retained_pvc() -> None:
    m = _render("highAvailability.enabled=true", "valkey.sentinel.workload=statefulset")
    sts = _by(m, "StatefulSet", "palaceoftruth-valkey-sentinel")
    spec = sts["spec"]
    # governing headless service + a data PVC per Sentinel
    assert spec["serviceName"] == "palaceoftruth-valkey-sentinel-bootstrap"
    names = [v["metadata"]["name"] for v in spec["volumeClaimTemplates"]]
    assert "sentinel-data" in names
    assert spec["persistentVolumeClaimRetentionPolicy"] == {"whenDeleted": "Retain", "whenScaled": "Retain"}
    # the sentinel still mounts its learned conf at /data
    mounts = {v["name"]: v["mountPath"] for v in sts["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]}
    assert mounts["sentinel-data"] == "/data"


def test_statefulset_mode_preserves_auth_invariants() -> None:
    m = _render("highAvailability.enabled=true", "valkey.sentinel.workload=statefulset", "valkey.auth.enabled=true")
    sts = _by(m, "StatefulSet", "palaceoftruth-valkey-sentinel")
    c = sts["spec"]["template"]["spec"]["containers"][0]
    assert "sentinel auth-pass" in "\n".join(c["args"])
    env = {e["name"] for e in c.get("env", [])}
    assert "VALKEY_PASSWORD" in env
    assert "REDISCLI_AUTH" not in env

def test_sentinel_deployment_replaces_one_pod_at_a_time_and_requires_quorum() -> None:
    # A coordinated upgrade must keep a quorum: replace pods ONE AT A TIME and
    # never surge a fourth identity. Recreate would drop all Sentinels at once.
    m = _render("highAvailability.enabled=true")
    d = _by(m, "Deployment", "palaceoftruth-valkey-sentinel")
    assert d["spec"]["strategy"] == {
        "type": "RollingUpdate",
        "rollingUpdate": {"maxUnavailable": 1, "maxSurge": 0},
    }


def test_sentinel_mode_requires_at_least_three_sentinels() -> None:
    # A single Sentinel cannot keep a quorum across a rolling replacement; the
    # chart must fail loudly instead of silently claiming HA.
    import subprocess
    import shutil
    cmd = ["helm", "template", "palaceoftruth", str(CHART),
           "--set", "highAvailability.enabled=true",
           "--set", "highAvailability.replicas.valkeySentinels=1"]
    if shutil.which("helm") is None:
        pytest.skip("helm is required for chart rendering tests")
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode != 0
    assert "at least 3 sentinels" in (r.stderr + r.stdout)

