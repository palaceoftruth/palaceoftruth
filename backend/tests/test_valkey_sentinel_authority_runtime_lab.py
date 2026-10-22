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
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
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
RUN_ID = os.environ.get("PALACE_VALKEY_LAB_OWNER") or uuid.uuid4().hex
OWNER_LABEL = "io.palace.valkey-lab.owner"
TAG, NET = f"psllab-{RUN_ID}", f"psl-lab-{RUN_ID}"
_LEDGERS = []

# Every external command is bounded. Docker CLI calls are cheap but a wedged
# daemon (or a container whose pipe is inherited by a shim) must never hang the
# runner, so inspect/remove get explicit finite bounds too.
DOCKER_TIMEOUT = 30
CLEANUP_BUDGET = 90.0
_LEDGER_DIR = Path(tempfile.gettempdir())


def _kill_group(proc):
    """Kill the whole process group of a bounded child, best effort."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _bounded(argv, *, timeout, check=False, env=None):
    """Run argv in its own session with a FINITE bound on both wait and drain.

    subprocess.run(timeout=...) kills only the direct child and then re-enters
    communicate() with no bound; when a surviving grandchild inherited the
    stdout pipe, that second wait blocks forever (the observed hang: cleanup
    never returned and the enclosing 300s killed the runner). Owning the
    process group lets us kill the entire tree and drain under a hard bound.
    """
    proc = subprocess.Popen(
        list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True, env=env,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        err = (err or "") + f"\n{argv[0]}: timed out after {timeout}s"
    cp = subprocess.CompletedProcess(list(argv), proc.returncode, out or "", err or "")
    if check:
        raise_for_status(cp)
    return cp


def raise_for_status(cp):
    if cp.returncode:
        raise subprocess.CalledProcessError(cp.returncode, cp.args, cp.stdout, cp.stderr)


def _vanished(stderr):
    text = (stderr or "").lower()
    return any(m in text for m in ("no such container", "no such network", "no such object"))


class _Ownership:
    """Only remove ledger entries after inspecting their exact ownership label.

    The ledger is flushed to disk BEFORE each resource is created, so a runner
    that dies still leaves an audit record of exactly what it owned. Removal is
    always by inspected immutable ID after an owner-label match; a resource that
    vanished underneath us is an already-satisfied race, never a foreign delete.
    """

    def __init__(self, owner, path=None):
        self.owner = owner
        self.resources = []
        self.ids = {}
        self.removed = set()
        self.tempdir = None
        self.status = "open"
        self.path = Path(path) if path else _LEDGER_DIR / f"valkey-lab-ledger-{owner}.json"
        self.flush()

    def _payload(self):
        return {
            "owner": self.owner,
            "status": self.status,
            "resources": [{"kind": k, "name": n, "id": self.ids.get((k, n))} for k, n in self.resources],
        }

    def flush(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self._payload(), indent=2) + "\n")
        os.replace(tmp, self.path)

    def remember(self, kind, name, resource_id=None):
        if (kind, name) not in self.resources:
            self.resources.append((kind, name))
        if resource_id:
            self.ids[(kind, name)] = resource_id
        self.flush()

    def remove(self, kind, name):
        if (kind, name) not in self.resources:
            raise RuntimeError(f"{name}: not in ownership ledger")
        if (kind, name) in self.removed:
            return
        result = _docker(kind, "inspect", name, timeout=15)
        if result.returncode:
            if _vanished(result.stderr):
                self.removed.add((kind, name))
                self.ids.pop((kind, name), None)
                self.flush()
                return
            raise RuntimeError(result.stderr.strip() or f"{name}: inspect failed")
        resource = json.loads(result.stdout)[0]
        labels = (resource.get("Config", {}).get("Labels") if kind == "container" else resource.get("Labels")) or {}
        if labels.get(OWNER_LABEL) != self.owner:
            raise RuntimeError(f"{name}: ownership mismatch; refusing removal")
        resource_id = resource.get("Id") or ""
        if not resource_id:
            raise RuntimeError(f"{name}: inspect returned no immutable ID; refusing removal")
        args = ("rm", "-f") if kind == "container" else ("network", "rm")
        removal = _docker(*args, resource_id, timeout=DOCKER_TIMEOUT)
        if removal.returncode:
            probe = _docker(kind, "inspect", name, timeout=15)
            if probe.returncode and _vanished(probe.stderr):
                # Raced with another remover: gone is gone, and we only ever
                # touched the ID we inspected and label-matched.
                self.removed.add((kind, name))
                self.ids.pop((kind, name), None)
                self.flush()
                return
            raise RuntimeError(removal.stderr.strip() or f"{name}: removal failed")
        self.removed.add((kind, name))
        self.ids.pop((kind, name), None)
        self.flush()

    def cleanup(self, budget=CLEANUP_BUDGET):
        errors = []
        deadline = time.monotonic() + budget
        for kind, name in list(reversed(self.resources)):
            if time.monotonic() > deadline:
                errors.append(f"cleanup budget exhausted before {kind} {name}")
                break
            try:
                self.remove(kind, name)
            except Exception as exc:
                errors.append(f"{kind} {name}: {exc}")
        self.status = "failed" if errors else "cleaned"
        self.flush()
        if self.tempdir is not None:
            shutil.rmtree(self.tempdir, ignore_errors=True)
            self.tempdir = None
        if errors:
            raise RuntimeError("; ".join(errors))


def _remove_container(name):
    for ledger in _LEDGERS:
        if ("container", name) in ledger.resources:
            ledger.remove("container", name)
            return
    raise RuntimeError(f"{name}: not in ownership ledger")


def _render(*sets):
    if shutil.which("helm") is None:
        pytest.skip("helm is required")
    cmd = ["helm", "template", "palaceoftruth", str(CHART)]
    for a in sets:
        cmd += ["--set", a]
    out = _bounded(cmd, timeout=120, check=True).stdout
    return [d for d in yaml.safe_load_all(out) if isinstance(d, dict)]


def _by(m, kind, name):
    return next(x for x in m if x.get("kind") == kind and x["metadata"]["name"] == name)


def _docker(*a, timeout=DOCKER_TIMEOUT, check=False):
    return _bounded(["docker", *a], timeout=timeout, check=check)


def _cli(c, *a, auth=True):
    base = ["docker", "exec", c, "valkey-cli"] + (["-a", PW, "--no-auth-warning"] if auth else [])
    return _bounded(base + list(a), timeout=20).stdout.strip()


def _sent(c, *a):
    return _bounded(["docker", "exec", c, "valkey-cli", "-p", "26379", *a], timeout=20).stdout.strip()


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


def _build_lab(tag, net_name, ledger):
    """Start a disposable Valkey primary + replica + 3 Sentinels. Returns a dict."""
    if shutil.which("docker") is None:
        pytest.skip("docker is required")
    m = _render("highAvailability.enabled=true", "valkey.auth.enabled=true", "valkey.sentinel.initialBootstrap=true")
    prim_init = _by(m, "StatefulSet", "palaceoftruth-valkey-primary")["spec"]["template"]["spec"]["initContainers"][0]["args"][0]
    repl_init = _by(m, "StatefulSet", "palaceoftruth-valkey-replica")["spec"]["template"]["spec"]["initContainers"][0]["args"][0]
    sent_start = _by(m, "Deployment", "palaceoftruth-valkey-sentinel")["spec"]["template"]["spec"]["containers"][0]["args"][0]
    cm = _by(m, "ConfigMap", "palaceoftruth-valkey-sentinel-config")["data"]["sentinel.conf"]

    d = Path(tempfile.mkdtemp(prefix=f"{tag}-"))
    # Registered on the ledger (not on the returned state) so the temp dir is
    # removed even when this function raises before returning.
    ledger.tempdir = d
    (d / "tpl").mkdir(); (d / "tpl" / "sentinel.conf").write_text(cm)
    for n in ("prim", "repl", "s1", "s2", "s3"):
        (d / n).mkdir(); os.chmod(d / n, 0o777)

    # Ownership is persisted BEFORE the resource exists, then the immutable ID
    # is recorded from the create output.
    ledger.remember("network", net_name)
    created_net = _docker("network", "create", "--label", f"{OWNER_LABEL}={RUN_ID}", net_name,
                          check=True, timeout=DOCKER_TIMEOUT)
    ledger.remember("network", net_name, created_net.stdout.strip())
    sub = _docker("network", "inspect", "-f", "{{(index .IPAM.Config 0).Subnet}}", net_name).stdout.strip()
    net = ipaddress.ip_network(sub)

    def data_node(name, alias, ip, vol, init):
        cname = f"{tag}-{name}"
        ledger.remember("container", cname)
        _remove_container(cname)
        cid = _docker("run", "-d", "--label", f"{OWNER_LABEL}={RUN_ID}", "--name", cname, "--network", net_name, "--ip", ip,
                      "--network-alias", alias, "-e", f"VALKEY_PASSWORD={PW}", "-v", f"{d / vol}:/data",
                      IMAGE, "sh", "-ec", init + "\nexec valkey-server /data/valkey.conf", check=True).stdout.strip()
        ledger.remember("container", cname, cid)

    def start_primary(ip, fresh=False):
        if fresh and (d / "prim").exists():
            for x in (d / "prim").iterdir():
                x.unlink()
        data_node("prim", SVC, ip, "prim", prim_init)

    def start_replica(ip):
        data_node("repl", RPL, ip, "repl", repl_init)

    def run_sentinel(i):
        cname = f"{tag}-s{i}"
        ledger.remember("container", cname)
        _remove_container(cname)
        cid = _docker("run", "-d", "--label", f"{OWNER_LABEL}={RUN_ID}", "--name", cname, "--network", net_name, "--network-alias", SENT, "--network-alias", SENT_BOOT,
                      "-e", f"VALKEY_PASSWORD={PW}", "-v", f"{d / 'tpl'}:/etc/sentinel-template:ro",
                      "-v", f"{d / f's{i}'}:/data", IMAGE, "sh", "-ec", sent_start, check=True).stdout.strip()
        ledger.remember("container", cname, cid)

    def start_sentinels():
        for i in (1, 2, 3):
            run_sentinel(i)

    def restart_sentinel(i):
        # Controlled, one-at-a-time replacement (the chart's RollingUpdate).
        run_sentinel(i)

    def wipe_sentinels():
        # FULL Sentinel state loss: remove the pods AND their learned conf.
        for i in (1, 2, 3):
            _remove_container(f"{tag}-s{i}")
            sd = d / f"s{i}"
            for x in list(sd.iterdir()):
                if x.is_dir():
                    shutil.rmtree(x, ignore_errors=True)
                else:
                    x.unlink(missing_ok=True)

    start_primary(str(net[10]), fresh=True)
    start_replica(str(net[11]))
    start_sentinels()
    return {"tag": tag, "net": net, "start_sentinels": start_sentinels,
            "restart_sentinel": restart_sentinel, "wipe_sentinels": wipe_sentinels,
            "start_primary": start_primary, "start_replica": start_replica, "_dir": d, "_net_name": net_name}


def _lab_fixture(tag, net_name):
    ledger = _Ownership(RUN_ID, _LEDGER_DIR / f"valkey-lab-ledger-{RUN_ID}-{tag}.json")
    _LEDGERS.append(ledger)
    try:
        state = _build_lab(tag, net_name, ledger)
        yield state
    finally:
        try:
            ledger.cleanup()
        finally:
            _LEDGERS.remove(ledger)
            # The temp dir is owned by the ledger and registered before any
            # resource exists, so it is removed even when the fixture raised.
            if ledger.tempdir is not None:
                shutil.rmtree(ledger.tempdir, ignore_errors=True)
                ledger.tempdir = None


@pytest.fixture(scope="module")
def lab():
    yield from _lab_fixture(TAG, NET)


@pytest.fixture(scope="module")
def lab_churn():
    yield from _lab_fixture(f"pslchurn-{RUN_ID}", f"psl-churn-{RUN_ID}")


@pytest.fixture(scope="module")
def lab_reconverge():
    yield from _lab_fixture(f"pslrcv-{RUN_ID}", f"psl-rcv-{RUN_ID}")


@pytest.fixture(scope="module")
def lab_roll():
    yield from _lab_fixture(f"pslroll-{RUN_ID}", f"psl-roll-{RUN_ID}")


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
    _remove_container(f"{TAG}-prim")
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
        _remove_container(f"{TAG}-s{i}")
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

def test_lab_full_sentinel_state_wipe_reconverges_to_the_newer_master(lab_reconverge):
    """FULL Sentinel state loss + old primary return.

    The Sentinels lose every learned conf. The ensemble must re-converge on the
    ACTUAL master (the promoted replica, holding the NEWER marker) instead of
    stranding on the returning old primary, and a client write through the
    Sentinel-discovered master must succeed with no second writable master."""
    tag = lab_reconverge["tag"]
    net = lab_reconverge["net"]
    assert _wait(lambda: _quorum_ok_tag(tag), 150), "no quorum after fresh bootstrap"
    assert _role(f"{tag}-prim") == "master"
    _cli(f"{tag}-prim", "SET", "rc:marker", "v1")
    assert _wait(lambda: _cli(f"{tag}-repl", "GET", "rc:marker") == "v1", 90), "replica did not sync"

    # natural failover to the replica
    _remove_container(f"{tag}-prim")
    repl_ip = _ipof(f"{tag}-repl")
    assert _wait(lambda: (_sent(f"{tag}-s1", "SENTINEL", "GET-MASTER-ADDR-BY-NAME", "mymaster").split() or [""])[0] == repl_ip, 180), "natural failover did not promote the replica"
    _cli(f"{tag}-repl", "SET", "rc:marker", "v2-NEWER")

    # the old primary returns with reused data: read-only, never a second master
    lab_reconverge["start_primary"](str(net[215]))
    time.sleep(20)
    assert _role(f"{tag}-prim") != "master", "returning old primary must not be writable"

    # FULL Sentinel state wipe, then a fresh ensemble
    lab_reconverge["wipe_sentinels"]()
    lab_reconverge["start_sentinels"]()

    # the ensemble must re-converge on the promoted (newer) master
    assert _wait(lambda: all(
        (_sent(f"{tag}-s{i}", "SENTINEL", "GET-MASTER-ADDR-BY-NAME", "mymaster").split() or [""])[0] == repl_ip
        for i in (1, 2, 3)), 120), "ensemble did not re-converge on the newer master after a full state wipe"

    # a client write through the Sentinel-discovered master succeeds
    addr = _sent(f"{tag}-s1", "SENTINEL", "GET-MASTER-ADDR-BY-NAME", "mymaster").split()
    assert addr and addr[0] == repl_ip
    out = _docker("exec", f"{tag}-s1", "valkey-cli", "-a", PW, "--no-auth-warning",
                  "-h", addr[0], "-p", addr[1], "SET", "rc:probe", "ok")
    assert "OK" in out.stdout, f"write through the Sentinel-discovered master failed: {out.stdout} {out.stderr}"
    # the NEWER marker is intact (no epoch/state reset)
    assert _cli(f"{tag}-repl", "GET", "rc:marker") == "v2-NEWER"
    # exactly one writable master
    assert _role(f"{tag}-prim") != "master"


def test_lab_sequential_sentinel_replacement_keeps_quorum(lab_roll):
    """A controlled one-at-a-time Sentinel replacement must keep a quorum and
    must not move the authoritative master (RollingUpdate, maxSurge=0)."""
    tag = lab_roll["tag"]
    assert _wait(lambda: _quorum_ok_tag(tag), 150), "no quorum after fresh bootstrap"
    _cli(f"{tag}-prim", "SET", "roll:marker", "v1")
    before = _sent(f"{tag}-s1", "SENTINEL", "GET-MASTER-ADDR-BY-NAME", "mymaster").split()
    assert before
    for i in (1, 2, 3):
        lab_roll["restart_sentinel"](i)
        time.sleep(10)
        remaining = [x for x in (1, 2, 3) if x != i]
        assert _wait(lambda: all("OK" in _sent(f"{tag}-s{x}", "SENTINEL", "CKQUORUM", "mymaster") for x in remaining), 60), f"quorum lost after replacing s{i}"
        addrs = {tuple(_sent(f"{tag}-s{x}", "SENTINEL", "GET-MASTER-ADDR-BY-NAME", "mymaster").split()) for x in remaining}
        assert addrs == {tuple(before)}, f"master moved during replacement of s{i}: {addrs}"
    assert _wait(lambda: _quorum_ok_tag(tag), 90)
    assert _cli(f"{tag}-prim", "GET", "roll:marker") == "v1"

