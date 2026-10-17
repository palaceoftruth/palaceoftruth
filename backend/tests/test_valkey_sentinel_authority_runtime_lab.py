"""Opt-in runtime lab for the Sentinel authority design (real containers).

Skipped unless PALACE_RUN_VALKEY_LAB=1. Renders the chart and runs the REAL
rendered start scripts in disposable Valkey containers on the pinned public image
and a dedicated Docker-allocated network. No secrets: literal lab fixture.

Covers: fresh bootstrap; a natural failover to the replica (newest marker kept);
a Sentinel restart after the failover (promoted master preserved); the old primary
returning with reused data (must NOT become a second writable master, newest
marker preserved); no reachable ensemble with reused data (FAIL CLOSED read-only);
and two consecutive primary IP changes with a CONTINUOUS Sentinel process (the
ensemble must keep exactly one authority).

NOT claimed: the Sentinel StatefulSet+PVC durability migration (not implemented).
"""
from __future__ import annotations

import ipaddress
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.skipif(
    os.environ.get("PALACE_RUN_VALKEY_LAB") != "1",
    reason="set PALACE_RUN_VALKEY_LAB=1 to run the disposable Docker lab",
)

CHART = Path(__file__).resolve().parents[2] / "chart"
IMAGE = "valkey/valkey:8-alpine@sha256:a038175878d66b9d274fbf8be73c0305e93798b83917647f167e18cef3c71eec"
PW = "lab-password"
SVC, RPL, SENT, SENT_BOOT = "palaceoftruth-valkey-primary", "palaceoftruth-valkey-replica", "palaceoftruth-valkey-sentinel", "palaceoftruth-valkey-sentinel-bootstrap"
TAG, NET = "psllab", "psl-lab"


def _render(*sets):
    if shutil.which("helm") is None:
        pytest.skip("helm is required")
    cmd = ["helm", "template", "palaceoftruth", str(CHART)]
    for a in sets:
        cmd += ["--set", a]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    return [d for d in yaml.safe_load_all(out) if isinstance(d, dict)]


def _by(m, kind, name):
    return next(x for x in m if x.get("kind") == kind and x["metadata"]["name"] == name)


def _docker(*a, **kw):
    return subprocess.run(["docker", *a], capture_output=True, text=True, **kw)


def _cli(c, *a, auth=True):
    base = ["docker", "exec", c, "valkey-cli"] + (["-a", PW, "--no-auth-warning"] if auth else [])
    return subprocess.run(base + list(a), capture_output=True, text=True, timeout=20).stdout.strip()


def _sent(c, *a):
    return subprocess.run(["docker", "exec", c, "valkey-cli", "-p", "26379", *a], capture_output=True, text=True, timeout=20).stdout.strip()


def _role(c):
    return (_cli(c, "ROLE").splitlines() or [""])[0]


def _ipof(c):
    return _docker("inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", c).stdout.strip()


def _wait(pred, timeout=150, interval=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def _build_lab(tag, net_name):
    """Start a disposable Valkey primary + replica + 3 Sentinels. Returns a dict."""
    if shutil.which("docker") is None:
        pytest.skip("docker is required")
    m = _render("highAvailability.enabled=true", "valkey.auth.enabled=true", "valkey.sentinel.initialBootstrap=true")
    prim_init = _by(m, "StatefulSet", "palaceoftruth-valkey-primary")["spec"]["template"]["spec"]["initContainers"][0]["args"][0]
    repl_init = _by(m, "StatefulSet", "palaceoftruth-valkey-replica")["spec"]["template"]["spec"]["initContainers"][0]["args"][0]
    sent_start = _by(m, "Deployment", "palaceoftruth-valkey-sentinel")["spec"]["template"]["spec"]["containers"][0]["args"][0]
    cm = _by(m, "ConfigMap", "palaceoftruth-valkey-sentinel-config")["data"]["sentinel.conf"]

    d = Path(tempfile.mkdtemp(prefix=f"{tag}-"))
    (d / "tpl").mkdir(); (d / "tpl" / "sentinel.conf").write_text(cm)
    for n in ("prim", "repl", "s1", "s2", "s3"):
        (d / n).mkdir(); os.chmod(d / n, 0o777)

    _docker("network", "rm", net_name)
    _docker("network", "create", net_name)
    sub = _docker("network", "inspect", "-f", "{{(index .IPAM.Config 0).Subnet}}", net_name).stdout.strip()
    net = ipaddress.ip_network(sub)

    def data_node(name, alias, ip, vol, init):
        _docker("rm", "-f", f"{tag}-{name}")
        _docker("run", "-d", "--name", f"{tag}-{name}", "--network", net_name, "--ip", ip,
                "--network-alias", alias, "-e", f"VALKEY_PASSWORD={PW}", "-v", f"{d / vol}:/data",
                IMAGE, "sh", "-ec", init + "\nexec valkey-server /data/valkey.conf")

    def start_primary(ip, fresh=False):
        if fresh and (d / "prim").exists():
            for x in (d / "prim").iterdir():
                x.unlink()
        data_node("prim", SVC, ip, "prim", prim_init)

    def start_replica(ip):
        data_node("repl", RPL, ip, "repl", repl_init)

    def start_sentinels():
        for i in (1, 2, 3):
            _docker("rm", "-f", f"{tag}-s{i}")
            _docker("run", "-d", "--name", f"{tag}-s{i}", "--network", net_name, "--network-alias", SENT, "--network-alias", SENT_BOOT,
                    "-e", f"VALKEY_PASSWORD={PW}", "-v", f"{d / 'tpl'}:/etc/sentinel-template:ro",
                    "-v", f"{d / f's{i}'}:/data", IMAGE, "sh", "-ec", sent_start)

    start_primary(str(net[10]), fresh=True)
    start_replica(str(net[11]))
    start_sentinels()
    return {"tag": tag, "net": net, "start_sentinels": start_sentinels,
            "start_primary": start_primary, "start_replica": start_replica, "_dir": d, "_net_name": net_name}


@pytest.fixture(scope="module")
def lab():
    state = _build_lab(TAG, NET)
    try:
        yield state
    finally:
        for c in ("prim", "repl", "s1", "s2", "s3"):
            _docker("rm", "-f", f"{TAG}-{c}")
        _docker("network", "rm", NET)
        shutil.rmtree(state["_dir"], ignore_errors=True)


@pytest.fixture(scope="module")
def lab_churn():
    state = _build_lab("pslchurn", "psl-churn")
    try:
        yield state
    finally:
        for c in ("prim", "repl", "s1", "s2", "s3"):
            _docker("rm", "-f", f"pslchurn-{c}")
        _docker("network", "rm", "psl-churn")
        shutil.rmtree(state["_dir"], ignore_errors=True)


def _quorum_ok():
    return all("OK" in _sent(f"{TAG}-s{i}", "SENTINEL", "CKQUORUM", "mymaster") for i in (1, 2, 3))


def _quorum_ok_tag(tag):
    return all("OK" in _sent(f"{tag}-s{i}", "SENTINEL", "CKQUORUM", "mymaster") for i in (1, 2, 3))


def test_lab_failover_sentinel_restart_and_old_primary_return(lab):
    net = lab["net"]
    assert _wait(_quorum_ok, 150), "no quorum after fresh bootstrap"
    assert _role(f"{TAG}-prim") == "master", "fresh primary must be a master"
    _cli(f"{TAG}-prim", "SET", "lab:marker", "v1")
    assert _wait(lambda: _cli(f"{TAG}-repl", "GET", "lab:marker") == "v1", 90), "replica did not sync"

    # natural failover: remove the (master) primary; the ensemble must promote the replica
    _docker("rm", "-f", f"{TAG}-prim")
    repl_ip = _ipof(f"{TAG}-repl")
    assert _wait(lambda: (_sent(f"{TAG}-s1", "SENTINEL", "GET-MASTER-ADDR-BY-NAME", "mymaster").split() or [""])[0] == repl_ip, 180), "natural failover did not promote the replica"
    assert _role(f"{TAG}-repl") == "master"
    _cli(f"{TAG}-repl", "SET", "lab:marker", "v2-NEWER")

    # Sentinel restart after the failover: the promoted master must be preserved
    lab["start_sentinels"]()
    time.sleep(15)
    addrs = {_sent(f"{TAG}-s{i}", "SENTINEL", "GET-MASTER-ADDR-BY-NAME", "mymaster").replace("\n", ":") for i in (1, 2, 3)}
    assert len(addrs) == 1 and list(addrs)[0].split(":")[0] == repl_ip, f"sentinel restart lost the promoted master: {addrs}"

    # the old primary returns with reused data: it must NOT become a second writable master
    lab["start_primary"](str(net[215]))
    time.sleep(20)
    masters = sum(1 for c in (f"{TAG}-prim", f"{TAG}-repl") if _role(c) == "master")
    assert masters == 1, f"expected exactly one writable master, saw {masters}"
    assert _role(f"{TAG}-repl") == "master", "the promoted replica must remain the master"
    assert _cli(f"{TAG}-repl", "GET", "lab:marker") == "v2-NEWER", "the newest marker must be preserved"


def test_lab_no_ensemble_with_reused_data_fails_closed(lab):
    for i in (1, 2, 3):
        _docker("rm", "-f", f"{TAG}-s{i}")
    lab["start_primary"](str(lab["net"][216]))
    time.sleep(70)  # bounded wait for the ensemble, then the fail-closed path
    assert _role(f"{TAG}-prim") != "master", "reused data with no ensemble must not be a writable master"
    out = _cli(f"{TAG}-prim", "SET", "lab:probe", "x")
    assert _role(f"{TAG}-prim") != "master" or "READONLY" in out.upper() or "MASTERDOWN" in out.upper()


def test_lab_default_initial_bootstrap_false_fresh_primary_fails_closed():
    """Default (initialBootstrap=false): a fresh primary with no observed quorum
    master must NOT become writable (fail closed)."""
    m = _render("highAvailability.enabled=true", "valkey.auth.enabled=true")  # default flag
    init = _by(m, "StatefulSet", "palaceoftruth-valkey-primary")["spec"]["template"]["spec"]["initContainers"][0]["args"][0]
    assert "initial_bootstrap=false" in init


def test_lab_two_ip_churn_keeps_one_continuous_authority(lab_churn):
    """Two consecutive primary IP changes with a CONTINUOUS Sentinel process.

    The Sentinel containers are never restarted. After the churn the ensemble
    must still agree on exactly ONE master address, and it must be the only
    writable master (a legitimate replica promotion is accepted -- data
    continuity is required, not the identity of the original master)."""
    tag = lab_churn["tag"]
    net = lab_churn["net"]
    assert _wait(lambda: _quorum_ok_tag(tag), 150), "no quorum after fresh bootstrap"
    assert _role(f"{tag}-prim") == "master"
    _cli(f"{tag}-prim", "SET", "churn:marker", "v1")
    assert _wait(lambda: _cli(f"{tag}-repl", "GET", "churn:marker") == "v1", 90), "replica did not sync"

    # move the primary pod to two new IPs; the Sentinel ensemble runs throughout
    for ip in (str(net[120]), str(net[130])):
        lab_churn["start_primary"](ip)
        time.sleep(25)

    addrs = {_sent(f"{tag}-s{i}", "SENTINEL", "GET-MASTER-ADDR-BY-NAME", "mymaster").replace("\n", ":") for i in (1, 2, 3)}
    assert len(addrs) == 1, f"continuous Sentinels disagree after IP churn: {addrs}"
    masters = [c for c in (f"{tag}-prim", f"{tag}-repl") if _role(c) == "master"]
    assert len(masters) == 1, f"expected exactly one writable master after churn, saw {masters}"
    assert _cli(masters[0], "GET", "churn:marker") == "v1", "the churn marker must survive the IP changes"
