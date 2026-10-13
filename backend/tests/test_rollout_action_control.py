"""Synthetic tests for the external single Helm action fence.

No production call is made. The invoker is a fake local process; the preflight
is synthetic; the store is a private temp directory. Tests never run real Helm,
never contact a cluster, and never read the environment.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = Path(__file__).resolve().parent.parent / "scripts/rollout_action_control.py"
spec = importlib.util.spec_from_file_location("rollout_action_control", SCRIPT)
g = importlib.util.module_from_spec(spec)
assert spec is not None and spec.loader is not None
spec.loader.exec_module(g)

NOW = datetime(2026, 10, 13, 5, 0, 0, tzinfo=timezone.utc)
REV = "main@sha1:" + "a" * 40
HOST = g.control_host_id()


def _manifest(**over):
    doc = {
        "schema_version": 1,
        "action_id": "sar1402-forward-01",
        "phase": "forward",
        "owner": {"host_id": HOST, "uid": os.getuid(), "owner_id": "barbara"},
        "repo": {"url": "https://github.com/heysarver/flux-control.git", "canonical_revision": REV},
        "namespace": "palace-sarvent",
        "release_name": "palace-sarvent",
        "expected_current_revision": 474,
        "chart_ref": "oci://ghcr.io/palaceoftruth/palaceoftruth/chart",
        "chart_version": "0.1.610",
        "image_digest": "sha256:" + "1" * 64,
        "chart_digest": "sha256:" + "2" * 64,
        "config_digest": "sha256:" + "3" * 64,
        "flux_suspend": True,
        "starts_at": (NOW - timedelta(minutes=1)).isoformat(),
        "expires_at": (NOW + timedelta(minutes=30)).isoformat(),
        "deadline_seconds": 30,
    }
    doc.update(over)
    return doc


def _write(tmp_path, doc):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(doc))
    return str(path), g.sha256_json(doc)


def _load(tmp_path, **over):
    path, digest = _write(tmp_path, _manifest(**over))
    return g.ActionManifest.load(path, digest, now=NOW)


def _preflight(**over):
    base = dict(repo_revision=REV, flux_suspend=True, current_revision=474,
                active_job=False, hr_history_revision=474, hr_history_pointer="helmrelease/history/474")
    base.update(over)
    return g.Preflight(**base)


def _ok_invoker(argv, *, deadline_seconds):
    assert argv[0] == "helm"
    return {"returncode": 0, "stdout": "Release upgraded", "stderr": ""}


# --- refusal surface -------------------------------------------------------

@pytest.mark.parametrize("forbidden", [
    {"unsuspend": True}, {"manual_helm": "helm upgrade x"}, {"extra_args": ["--set", "a=b"]},
    {"values": {"x": 1}}, {"values_files": ["v.yaml"]}, {"proxy": "http://p:8080"},
    {"print_secrets": True}, {"debug": True},
])
def test_unsafe_caller_surfaces_are_refused(tmp_path, forbidden):
    path, digest = _write(tmp_path, _manifest(**forbidden))
    with pytest.raises(g.ControlError):
        g.ActionManifest.load(path, digest, now=NOW)


def test_flux_must_stay_suspended(tmp_path):
    with pytest.raises(g.ControlError):
        _load(tmp_path, flux_suspend=False)


def test_digest_mismatch_refused(tmp_path):
    path, _ = _write(tmp_path, _manifest())
    with pytest.raises(g.ControlError):
        g.ActionManifest.load(path, "b" * 64, now=NOW)


def test_wrong_phase_refused(tmp_path):
    with pytest.raises(g.ControlError):
        _load(tmp_path, phase="rollback")


def test_credential_field_refused(tmp_path):
    doc = _manifest()
    doc["owner"]["token"] = "x"
    path, digest = _write(tmp_path, doc)
    with pytest.raises(g.ControlError):
        g.ActionManifest.load(path, digest, now=NOW)


# --- fail-closed live identity --------------------------------------------

@pytest.mark.parametrize("bad", [
    {"repo_revision": "main@sha1:" + "b" * 40},
    {"flux_suspend": False},
    {"current_revision": 475},
    {"active_job": True},
    {"hr_history_revision": 473},
    {"hr_history_pointer": ""},
])
def test_preflight_identity_mismatch_refuses_before_consume(tmp_path, bad):
    manifest = _load(tmp_path)
    store = g.SlotStore(tmp_path / "store")
    with pytest.raises(g.ControlError):
        g.run_action(manifest, _preflight(**bad), store=store, invoker=_ok_invoker, now=NOW)
    assert not store.consumed(manifest.slot)


def test_happy_path_consumes_once(tmp_path):
    manifest = _load(tmp_path)
    store = g.SlotStore(tmp_path / "store")
    result = g.run_action(manifest, _preflight(), store=store, invoker=_ok_invoker, now=NOW)
    assert result["slot"] == manifest.slot
    assert store.consumed(manifest.slot)


# --- exactly-once, crash, no-retry ----------------------------------------

def test_second_run_refused_after_success(tmp_path):
    manifest = _load(tmp_path)
    store = g.SlotStore(tmp_path / "store")
    g.run_action(manifest, _preflight(), store=store, invoker=_ok_invoker, now=NOW)
    with pytest.raises(g.ControlError):
        g.run_action(manifest, _preflight(), store=store, invoker=_ok_invoker, now=NOW)


def test_crash_before_invocation_still_consumes_slot(tmp_path):
    manifest = _load(tmp_path)
    store = g.SlotStore(tmp_path / "store")

    calls = []

    def crashing(argv, *, deadline_seconds):
        calls.append(argv)
        raise g.ControlError("simulated crash after consumption")

    with pytest.raises(g.ControlError):
        g.run_action(manifest, _preflight(), store=store, invoker=crashing, now=NOW)
    assert len(calls) == 1
    assert store.consumed(manifest.slot)
    # A restart must not re-invoke.
    with pytest.raises(g.ControlError):
        g.run_action(manifest, _preflight(), store=store, invoker=_ok_invoker, now=NOW)


def test_timeout_kills_and_never_reinvokes(tmp_path):
    manifest = _load(tmp_path)
    store = g.SlotStore(tmp_path / "store")
    script = tmp_path / "fake_helm.sh"
    script.write_text("#!/bin/sh\nsleep 30\n")
    script.chmod(0o700)

    def hanging(argv, *, deadline_seconds):
        return g.default_invoker([str(script)], deadline_seconds=1)

    start = time.monotonic()
    with pytest.raises(g.ControlError):
        g.run_action(manifest, _preflight(), store=store, invoker=hanging, now=NOW)
    assert time.monotonic() - start < 10
    assert store.consumed(manifest.slot)
    with pytest.raises(g.ControlError):
        g.run_action(manifest, _preflight(), store=store, invoker=_ok_invoker, now=NOW)


def test_expired_manifest_refused(tmp_path):
    manifest = _load(tmp_path)
    store = g.SlotStore(tmp_path / "store")
    later = NOW + timedelta(hours=2)
    with pytest.raises(g.ControlError):
        g.run_action(manifest, _preflight(), store=store, invoker=_ok_invoker, now=later)


# --- atomic cross-process concurrency -------------------------------------

def test_concurrent_processes_consume_exactly_once(tmp_path):
    store_dir = tmp_path / "store"
    slot = "s" * 64
    worker = tmp_path / "worker.py"
    worker.write_text(textwrap.dedent(f'''
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location("rac", {str(SCRIPT)!r})
        g = importlib.util.module_from_spec(spec); spec.loader.exec_module(g)
        store = g.SlotStore({str(store_dir)!r})
        try:
            store.consume({slot!r}, {{"probe": 1}})
            print("WON")
        except g.ControlError:
            print("LOST")
    '''))
    procs = [subprocess.Popen([sys.executable, str(worker)], stdout=subprocess.PIPE, text=True)
             for _ in range(8)]
    outs = [p.communicate()[0].strip() for p in procs]
    assert outs.count("WON") == 1, outs
    assert outs.count("LOST") == 7, outs


# --- store hardening -------------------------------------------------------

def test_store_rejects_world_readable(tmp_path):
    root = tmp_path / "store"
    root.mkdir(mode=0o755)
    os.chmod(root, 0o755)
    store = g.SlotStore(root)
    with pytest.raises(g.ControlError):
        store.consume("a" * 64, {"x": 1})


# --- fake external helm stdio safety --------------------------------------

def test_fake_helm_stdio_is_secret_free(tmp_path):
    manifest = _load(tmp_path)
    store = g.SlotStore(tmp_path / "store")
    script = tmp_path / "fake_helm.sh"
    script.write_text("#!/bin/sh\necho 'Release palace-sarvent upgraded to 0.1.610'\nexit 0\n")
    script.chmod(0o700)

    def invoker(argv, *, deadline_seconds):
        result = g.default_invoker([str(script)], deadline_seconds=5)
        g._scan_output(result["stdout"] + result["stderr"])
        return result

    result = g.run_action(manifest, _preflight(), store=store, invoker=invoker, now=NOW)
    assert result["returncode"] == 0


def test_secret_shaped_child_output_is_refused(tmp_path):
    script = tmp_path / "leaky.sh"
    script.write_text("#!/bin/sh\necho 'api_key=abc123'\nexit 0\n")
    script.chmod(0o700)
    result = g.default_invoker([str(script)], deadline_seconds=5)
    with pytest.raises(g.ControlError):
        g._scan_output(result["stdout"] + result["stderr"])


def test_helm_argv_has_no_caller_extras(tmp_path):
    manifest = _load(tmp_path)
    argv = manifest.helm_argv()
    assert argv[0] == "helm"
    assert "--set" not in argv
    assert "--values" not in argv
    assert "--namespace" in argv and "palace-sarvent" in argv
