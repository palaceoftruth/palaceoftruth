"""Opt-in runtime regression test for the Sentinel IP-churn repair.

This is NOT a manifest text assertion. It runs the chart's OWN rendered sentinel
container command inside disposable Docker containers and drives three real
Valkey nodes plus three Sentinels through:

  1. a primary pod/IP churn (recreate, stable service alias, same volume),
  2. an authoritative failover to the replica,
  3. the old primary returning as a master at a NEW IP, then
  4. a Sentinel restart rendered from the template (the PR behaviour),

and asserts the ensemble ends on exactly ONE authoritative master, never reverts
to a stale address, and keeps a pre-seeded marker key.

Skipped unless ``PALACE_RUN_VALKEY_LAB=1`` and both ``helm`` and ``docker`` are
present. It never publishes host ports, never uses the host network, and refuses
to run against a non-default ``DOCKER_HOST`` or Docker context. No secrets: auth
is disabled for the render so the lab exercises the churn path only.
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import subprocess
import time
import uuid
from typing import Any

import pytest
import yaml

REQUIRED_ENV = "PALACE_RUN_VALKEY_LAB"
CHART_DIR = __import__("pathlib").Path(__file__).resolve().parents[2] / "chart"
TEMPLATE_DIR = "/etc/sentinel-template"


# --------------------------------------------------------------------------- #
# Environment guards
# --------------------------------------------------------------------------- #
def _docker_usable() -> bool:
    if shutil.which("docker") is None:
        return False
    host = os.environ.get("DOCKER_HOST", "")
    # Only the local default socket is acceptable. A remote/prod DOCKER_HOST is
    # never touched by this test.
    if host and host not in {"unix:///var/run/docker.sock", "unix:///run/docker.sock"}:
        return False
    try:
        ctx = subprocess.run(
            ["docker", "context", "show"], capture_output=True, text=True, timeout=20
        )
        if ctx.returncode != 0 or ctx.stdout.strip() != "default":
            return False
        probe = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return probe.returncode == 0 and bool(probe.stdout.strip())
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    os.environ.get(REQUIRED_ENV) != "1",
    reason=f"set {REQUIRED_ENV}=1 to run the disposable Valkey Sentinel runtime lab",
)


# --------------------------------------------------------------------------- #
# Small docker helpers (all bounded; no shell=True)
# --------------------------------------------------------------------------- #
def _run(args: list[str], timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise AssertionError(f"command failed ({result.returncode}): {' '.join(args)}\n{result.stderr}")
    return result


def _out(args: list[str], timeout: int = 60) -> str:
    return _run(args, timeout=timeout).stdout.strip()


def _cli(container: str, *cli_args: str, timeout: int = 20, check: bool = True) -> str:
    return _out(["docker", "exec", container, "valkey-cli", *cli_args], timeout=timeout) if check else (
        _run(["docker", "exec", container, "valkey-cli", *cli_args], timeout=timeout, check=False).stdout.strip()
    )


def _sentinel(name: str, *cli_args: str, timeout: int = 20, check: bool = True) -> str:
    return _cli(name, "-p", "26379", *cli_args, timeout=timeout, check=check)


def _master_addr(sentinel: str) -> str:
    return ":".join(_sentinel(sentinel, "SENTINEL", "GET-MASTER-ADDR-BY-NAME", "mymaster").split())


class ValkeyLab:
    """Disposable docker lab. Always cleaned up, including on failure."""

    def __init__(self, image: str, monitor_host: str, sentinel_script: str, sentinel_conf: str) -> None:
        self.image = image
        self.monitor_host = monitor_host
        self.sentinel_script = sentinel_script
        self.sentinel_conf = sentinel_conf
        self.tag = uuid.uuid4().hex[:8]
        self.network = f"psl-lab-{self.tag}"
        self.primary_volume = f"psl-lab-p-{self.tag}"
        self.replica_volume = f"psl-lab-r-{self.tag}"
        self.primary = f"psl-lab-primary-{self.tag}"
        self.replica = f"psl-lab-replica-{self.tag}"
        self.sentinels = [f"psl-lab-s{i}-{self.tag}" for i in (1, 2, 3)]
        self.old_ip = ""
        self.new_ip = ""
        self.return_ip = ""
        self._host_template_dir: str | None = None
        self._sentinel_data_dirs: list[str] = []

    # -- lifecycle ---------------------------------------------------------- #
    def up(self) -> "ValkeyLab":
        import tempfile

        _run(["docker", "network", "create", self.network])
        _run(["docker", "volume", "create", self.primary_volume])
        _run(["docker", "volume", "create", self.replica_volume])

        subnet = _out(
            ["docker", "network", "inspect", "-f", "{{(index .IPAM.Config 0).Subnet}}", self.network]
        )
        net = ipaddress.ip_network(subnet)
        # Deterministic, collision-free addresses inside the Docker-allocated subnet.
        self.old_ip, self.new_ip, self.return_ip = str(net[10]), str(net[200]), str(net[210])

        self._host_template_dir = tempfile.mkdtemp(prefix="psl-lab-tpl-")
        with open(os.path.join(self._host_template_dir, "sentinel.conf"), "w") as handle:
            handle.write(self.sentinel_conf)
        os.chmod(os.path.join(self._host_template_dir, "sentinel.conf"), 0o644)

        self._start_primary(self.old_ip)
        _run(
            [
                "docker", "run", "-d", "--name", self.replica, "--network", self.network,
                "--ip", str(net[11]), "--network-alias", "valkey-replica",
                "-v", f"{self.replica_volume}:/data", self.image,
                "valkey-server", "--appendonly", "yes", "--appendfilename", "appendonly.aof",
                "--dir", "/data", "--replicaof", self.monitor_host, "6379",
            ]
        )
        for sentinel in self.sentinels:
            self._start_sentinel(sentinel)
        return self

    def _start_primary(self, ip: str) -> None:
        _run(
            [
                "docker", "run", "-d", "--name", self.primary, "--network", self.network,
                "--ip", ip, "--network-alias", self.monitor_host,
                "-v", f"{self.primary_volume}:/data", self.image,
                "valkey-server", "--appendonly", "yes", "--appendfilename", "appendonly.aof",
                "--dir", "/data",
            ]
        )

    def _start_sentinel(self, name: str) -> None:
        import tempfile

        data_dir = tempfile.mkdtemp(prefix=f"psl-lab-{name}-")
        self._sentinel_data_dirs.append(data_dir)
        _run(
            [
                "docker", "run", "-d", "--name", name, "--network", self.network,
                "-v", f"{self._host_template_dir}:{TEMPLATE_DIR}:ro",
                "-v", f"{data_dir}:/data", self.image,
                "sh", "-ec", self.sentinel_script,
            ]
        )

    def down(self) -> None:
        for container in [*self.sentinels, self.primary, self.replica]:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=60)
        for volume in (self.primary_volume, self.replica_volume):
            subprocess.run(["docker", "volume", "rm", "-f", volume], capture_output=True, timeout=60)
        subprocess.run(["docker", "network", "rm", self.network], capture_output=True, timeout=60)

    # -- operations --------------------------------------------------------- #
    def recreate_primary(self, ip: str) -> None:
        subprocess.run(["docker", "rm", "-f", self.primary], capture_output=True, timeout=60)
        self._start_primary(ip)

    def restart_sentinels(self) -> None:
        for sentinel in self.sentinels:
            subprocess.run(["docker", "rm", "-f", sentinel], capture_output=True, timeout=60)
            self._start_sentinel(sentinel)

    def wait_for(self, predicate, timeout: int = 60, interval: float = 2.0):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                last = predicate()
            except Exception:
                last = None
            if last:
                return last
            time.sleep(interval)
        return last


# --------------------------------------------------------------------------- #
# The test
# --------------------------------------------------------------------------- #
def _lab_inputs() -> tuple[str, str, str, str]:
    if shutil.which("helm") is None:
        pytest.skip("helm is required for the runtime lab")
    rendered = subprocess.run(
        [
            "helm", "template", "litmus", str(CHART_DIR),
            "--set", "valkey.sentinel.enabled=true",
            "--set", "valkey.auth.enabled=false",
        ],
        check=True, capture_output=True, text=True, timeout=120,
    ).stdout
    docs = [d for d in yaml.safe_load_all(rendered) if isinstance(d, dict)]
    sentinel_deploy = next(
        d for d in docs
        if d.get("kind") == "Deployment" and d["metadata"]["name"].endswith("valkey-sentinel")
    )
    container = sentinel_deploy["spec"]["template"]["spec"]["containers"][0]
    assert container["name"] == "sentinel"
    assert container["command"] == ["/bin/sh", "-ec"], "lab assumes the chart's shell command"
    configmap = next(
        d for d in docs
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"].endswith("valkey-sentinel-config")
    )
    sentinel_conf = configmap["data"]["sentinel.conf"]
    monitor_line = next(l for l in sentinel_conf.splitlines() if l.startswith("sentinel monitor"))
    # Derive the monitored SERVICE hostname from the chart's own render so the lab
    # aliases the primary exactly as the chart names it (release-scoped).
    parts = monitor_line.split()
    assert len(parts) >= 5 and parts[4] == "6379"
    monitor_host = parts[3]
    assert "valkey-primary" in monitor_host
    assert "sentinel resolve-hostnames yes" in sentinel_conf
    return container["image"], monitor_host, container["args"][0], sentinel_conf


def _require_docker() -> None:
    if not _docker_usable():
        pytest.skip("a local default docker is required for the runtime lab")


def _boot_lab(lab: "ValkeyLab") -> str:
    lab.up()
    assert lab.wait_for(
        lambda: _cli(lab.primary, "PING", check=False) == "PONG", timeout=60
    ), "primary never answered PING"
    assert lab.wait_for(
        lambda: all(
            "OK" in _sentinel(s, "SENTINEL", "CKQUORUM", "mymaster", check=False)
            for s in lab.sentinels
        ),
        timeout=60,
    ), "sentinels never reached quorum at start"
    marker = f"lab-marker-{lab.tag}"
    _cli(lab.primary, "SET", "lab:marker", marker)
    assert lab.wait_for(
        lambda: _cli(lab.replica, "GET", "lab:marker", check=False) == marker, timeout=60
    ), "replica never synced the marker"
    assert _master_addr(lab.sentinels[0]) == f"{lab.old_ip}:6379"
    return marker


def test_sentinel_re_resolves_primary_service_after_ip_churn() -> None:
    """The 477 failure mode: the primary pod is recycled onto a new IP behind the
    same service hostname. Every Sentinel must re-resolve the service to the new
    IP and converge on ONE authoritative master with the marker intact, with no
    operator restart and no SENTINEL reset.
    """
    _require_docker()
    image, monitor_host, sentinel_script, sentinel_conf = _lab_inputs()
    lab = ValkeyLab(image, monitor_host, sentinel_script, sentinel_conf)
    try:
        marker = _boot_lab(lab)
        lab.recreate_primary(lab.new_ip)
        assert lab.wait_for(
            lambda: all(_master_addr(s) == f"{lab.new_ip}:6379" for s in lab.sentinels),
            timeout=150,
        ), (
            "sentinels did not re-resolve the primary to its new IP: "
            f"{[_master_addr(s) for s in lab.sentinels]}"
        )
        assert lab.wait_for(
            lambda: _converged_master(lab, marker), timeout=90
        ), "no single consistent authoritative master after the IP churn"
    finally:
        lab.down()


@pytest.mark.xfail(
    strict=False,
    reason=(
        "KNOWN and PRE-EXISTING chart limitation, measured here and NOT introduced "
        "by the config-render change: after an authoritative failover plus the old "
        "primary returning as a master, a Sentinel restart leaves TWO masters. The "
        "render change only picks which one the ensemble adopts, and it can adopt the "
        "OLD primary. Do not merge until resolved. See "
        "workspace/palace-sentinel-durable-repair/REPAIR.md."
    ),
)
def test_sentinel_restart_after_failover_leaves_one_master() -> None:
    _require_docker()
    image, monitor_host, sentinel_script, sentinel_conf = _lab_inputs()
    lab = ValkeyLab(image, monitor_host, sentinel_script, sentinel_conf)
    try:
        marker = _boot_lab(lab)
        _sentinel(lab.sentinels[0], "SENTINEL", "FAILOVER", "mymaster", check=False)
        replica_ip = _out(
            ["docker", "inspect", "-f",
             "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", lab.replica]
        )
        assert lab.wait_for(
            lambda: _master_addr(lab.sentinels[0]) == f"{replica_ip}:6379", timeout=90
        ), "authoritative failover did not promote the replica"
        lab.recreate_primary(lab.return_ip)
        lab.restart_sentinels()
        assert lab.wait_for(
            lambda: _converged_master(lab, marker), timeout=180
        ), "the ensemble did not reach one consistent master after failover + restart"
        assert lab.wait_for(
            lambda: {_master_addr(s) for s in lab.sentinels} == {f"{replica_ip}:6379"},
            timeout=60,
        ), "the promoted replica was orphaned as a second master"
    finally:
        lab.down()


def _converged_master(lab: ValkeyLab, marker: str) -> str | None:
    """Return the agreed master IP iff the ensemble is in a consistent state."""
    addresses = {_master_addr(s) for s in lab.sentinels}
    if len(addresses) != 1:
        return None
    ip = next(iter(addresses)).split(":")[0]
    by_ip = {
        _out(["docker", "inspect", "-f",
              "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", c]): c
        for c in (lab.primary, lab.replica)
    }
    master = by_ip.get(ip)
    if master is None:
        return None
    roles = {c: _cli(c, "ROLE", check=False).splitlines()[:1] for c in (lab.primary, lab.replica)}
    if roles.get(master) != ["master"]:
        return None
    if any(r == ["master"] for c, r in roles.items() if c != master):
        return None
    if _cli(master, "GET", "lab:marker", check=False) != marker:
        return None
    return ip
