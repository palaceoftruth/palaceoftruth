"""Hermetic contract test for the Sentinel start path (no cluster, no Docker).

Renders the chart, extracts the REAL Sentinel start script, and runs it under a
stub PATH so the monitor-pin logic is exercised exactly as shipped:

  * FRESH boot with a HOSTNAME monitor line -> the line is rewritten to a
    literal address (a hostname is a FATAL config error for Sentinel with
    resolve-hostnames off);
  * FRESH boot whose first DNS answer is LATE -> the bounded wait still lands;
  * EXISTING conf with a LIVE master -> kept byte for byte;
  * EXISTING conf with a STALE literal address -> re-pinned to the live master;
  * the monitor line is NEVER left blank.

This pins the "monitor blank parser" bug: pin_monitor must atomically swap in a
complete replacement line and refuse to write an empty file.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import stat
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[2] / "chart"

GETENT = """#!/bin/sh
# stub: 'getent ahosts NAME' -> $GETENT_IP (or nothing when GETENT_RC != 0)
n=$(cat "$GETENT_COUNT" 2>/dev/null || echo 0)
n=$((n + 1)); echo "$n" > "$GETENT_COUNT"
if [ "$n" -le "${GETENT_EMPTY_FIRST:-0}" ]; then exit 2; fi
if [ -n "${GETENT_IP:-}" ]; then echo "$GETENT_IP"; exit 0; fi
exit 2
"""

VALKEY = """#!/bin/sh
# stub: 'valkey-cli -h HOST ... role' -> contents of $ROLES/HOST
host=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -h) host="$2"; shift 2 ;;
    *) shift ;;
  esac
done
cat "$ROLES/$host" 2>/dev/null || true
"""


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


def _start_script(*sets) -> str:
    m = _render(*sets)
    return _by(m, "Deployment", "palaceoftruth-valkey-sentinel")["spec"]["template"]["spec"]["containers"][0]["args"][0]


def _sentinel_config(*sets) -> str:
    m = _render(*sets)
    return _by(m, "ConfigMap", "palaceoftruth-valkey-sentinel-config")["data"]["sentinel.conf"]


def test_sentinel_config_does_not_resolve_hostnames() -> None:
    # resolve-hostnames yes makes Sentinel re-resolve a service name on every
    # reconnect; the blocking DNS stalls the event loop and TILTS the ensemble
    # (no failover). The chart must not enable it.
    cfg = _sentinel_config("highAvailability.enabled=true")
    assert not any(l.strip().startswith("sentinel resolve-hostnames") for l in cfg.splitlines())
    assert not any(l.strip().startswith("sentinel announce-hostnames") for l in cfg.splitlines())
    assert "sentinel monitor mymaster" in cfg


def _run_script(tmp: Path, script: str, *, getent_ip, roles, empty_first=0, wait_seconds=5):
    """Run the real start script with stubs; return (returncode, conf_text)."""
    bindir = tmp / "bin"; bindir.mkdir(parents=True, exist_ok=True)
    (bindir / "getent").write_text(GETENT)
    (bindir / "valkey-cli").write_text(VALKEY)
    for f in (bindir / "getent", bindir / "valkey-cli"):
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    roles_dir = tmp / "roles"; roles_dir.mkdir(exist_ok=True)
    for host, role in roles.items():
        (roles_dir / host).write_text(role + "\n")
    data = tmp / "data"; data.mkdir(exist_ok=True)
    tpl = tmp / "tpl"; tpl.mkdir(exist_ok=True)
    (tpl / "sentinel.conf").write_text(
        "sentinel monitor mymaster palaceoftruth-valkey-primary 6379 2\n"
        "sentinel down-after-milliseconds mymaster 5000\n"
        "port 26379\n"
    )
    # point the shipped paths at the sandbox and drop the exec so the script is a
    # pure conf transform we can assert on.
    s = script.replace("/etc/sentinel-template", str(tpl)).replace("/data", str(data))
    s = s.replace("exec valkey-sentinel", "true valkey-sentinel")
    s = s.replace("wait_seconds=20", f"wait_seconds={wait_seconds}")
    env = dict(os.environ)
    env.update(PATH=f"{bindir}:{env['PATH']}", ROLES=str(roles_dir),
               VALKEY_PASSWORD="lab-password",
               GETENT_IP=getent_ip or "", GETENT_EMPTY_FIRST=str(empty_first),
               GETENT_COUNT=str(tmp / ".getent_count"))
    r = subprocess.run(["sh", "-ec", s], capture_output=True, text=True, env=env, timeout=60)
    conf = (data / "sentinel.conf")
    return r.returncode, (conf.read_text() if conf.exists() else "")


def _monitor_addr(conf: str) -> str:
    for line in conf.splitlines():
        if line.startswith("sentinel monitor "):
            return line.split()[3]
    return ""


def test_fresh_hostname_is_pinned_to_a_literal_address(tmp_path) -> None:
    # fresh boot, hostname monitor, the pod exists but is not a master yet
    rc, conf = _run_script(tmp_path, _start_script("highAvailability.enabled=true"),
                           getent_ip="10.0.0.5", roles={"10.0.0.5": "replica"})
    assert rc == 0, "start path must not fail with a resolvable service"
    addr = _monitor_addr(conf)
    assert addr == "10.0.0.5", f"monitor must be the literal address, got {addr!r}"
    assert conf.strip(), "conf must never be blank"


def test_fresh_late_dns_answer_is_still_pinned(tmp_path) -> None:
    # the service does not resolve for the first two polls; the bounded wait lands
    rc, conf = _run_script(tmp_path, _start_script("highAvailability.enabled=true"),
                           getent_ip="10.0.0.7", roles={"10.0.0.7": "master"}, empty_first=2)
    assert rc == 0
    assert _monitor_addr(conf) == "10.0.0.7", "late-bounded wait must still pin a literal address"


def test_existing_live_master_is_kept_byte_for_byte(tmp_path) -> None:
    script = _start_script("highAvailability.enabled=true")
    data = tmp_path / "data"; data.mkdir(parents=True, exist_ok=True)
    (data / "sentinel.conf").write_text("sentinel monitor mymaster 10.9.9.9 6379 2\nport 26379\n")
    rc, conf = _run_script(tmp_path, script, getent_ip="10.0.0.5", roles={"10.9.9.9": "master"})
    assert rc == 0
    assert _monitor_addr(conf) == "10.9.9.9", "a live master must be preserved"


def test_existing_stale_address_is_repinned_to_the_live_master(tmp_path) -> None:
    script = _start_script("highAvailability.enabled=true")
    data = tmp_path / "data"; data.mkdir(parents=True, exist_ok=True)
    (data / "sentinel.conf").write_text("sentinel monitor mymaster 10.9.9.9 6379 2\nport 26379\n")
    rc, conf = _run_script(tmp_path, script, getent_ip="10.0.0.5", roles={"10.9.9.9": "replica", "10.0.0.5": "master"})
    assert rc == 0
    assert _monitor_addr(conf) == "10.0.0.5", "a stale literal address must be re-pinned"
    assert conf.count("sentinel monitor ") == 1


def test_pin_guard_is_atomic_and_refuses_to_blank_the_conf(tmp_path) -> None:
    # the shipped guard is present: swap only from a complete, non-empty rewrite
    script = _start_script("highAvailability.enabled=true")
    assert '[ -s "$tmp" ]' in script
    assert "mv \"$tmp\" \"$conf\"" in script
    # if no address can be resolved the recorded line is kept verbatim, never blank
    data = tmp_path / "data"; data.mkdir(parents=True, exist_ok=True)
    (data / "sentinel.conf").write_text("sentinel monitor mymaster 10.9.9.9 6379 2\nport 26379\n")
    rc, conf = _run_script(tmp_path, script, getent_ip="", roles={"10.9.9.9": "replica"}, wait_seconds=2)
    assert rc == 0
    assert conf.strip(), "conf must never be blank"
    assert _monitor_addr(conf) == "10.9.9.9", "an unresolvable service keeps the recorded line"
