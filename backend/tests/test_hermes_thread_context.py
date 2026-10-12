"""Offline caller-context regressions for every Palace-owned thread.

The real-host control is optional locally and mandatory in the execution receipt.
It imports the official ABC/helper, not a fork or a runtime replacement.
"""
from __future__ import annotations

import contextvars
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from tests.test_hermes_memory_plugin import load_palaceoftruth_plugin, PLUGIN_PATH


@pytest.mark.parametrize("hook", ["prefetch", "sync", "mirror"])
@pytest.mark.parametrize("fail", [False, True])
def test_background_hook_preserves_submission_context(monkeypatch, hook, fail):
    module = load_palaceoftruth_plugin()
    provider = module.PalaceOfTruthMemoryProvider()
    monkeypatch.setattr(provider, "_has_api_auth", lambda: True)
    monkeypatch.setattr(provider, "_resolve_tenant_id", lambda: "offline-tenant")
    marker = contextvars.ContextVar("offline-profile", default="wrong-profile")
    entered, release = threading.Event(), threading.Event()
    seen = []

    def offline(*args, **kwargs):
        entered.set()
        assert release.wait(3), "Test transport was not released"
        seen.append(marker.get())
        if fail:
            raise RuntimeError("offline fixture failure")
        return "fixture recall" if hook == "prefetch" else {"ok": True}

    if hook == "prefetch":
        monkeypatch.setattr(provider, "_prefetch_with_budget", offline)
    else:
        monkeypatch.setattr(provider, "_post_memory_entries", offline)
    token = marker.set("submission-profile")
    try:
        if hook == "prefetch":
            provider.queue_prefetch("offline query", session_id="fixture-session")
            worker = provider._prefetch_thread
        elif hook == "sync":
            provider.sync_turn("offline user", "offline assistant", session_id="fixture-session")
            worker = provider._sync_thread
        else:
            provider.on_memory_write("add", "memory", "offline fixture")
            worker = provider._sync_thread
        assert entered.wait(3)
        marker.set("later-parent-profile")
        release.set()
        worker.join(3)
        assert not worker.is_alive()
        assert seen == ["submission-profile"]
        assert marker.get() == "later-parent-profile"
    finally:
        release.set()
        provider.shutdown()
        marker.reset(token)


def test_optional_real_upstream_context_control(tmp_path):
    root = os.environ.get("HERMES_CONTRACT_ROOT")
    if not root:
        pytest.skip("Set HERMES_CONTRACT_ROOT to a trusted official Hermes checkout")
    host = Path(root).resolve()
    assert (host / "agent/memory_provider.py").is_file()
    # Fresh isolated interpreter: no stub modules, .pth startup or inherited secrets.
    script = r'''
import contextvars, importlib.util, inspect, json, pathlib, socket, sys, threading
host, plugin = map(pathlib.Path, sys.argv[1:])
def audit(event, args):
    if event.startswith("socket.") or event in {"subprocess.Popen", "os.system"}:
        raise AssertionError("Offline control forbids network/process execution")
sys.addaudithook(audit)
sys.path.insert(0, str(host))
from agent.memory_provider import MemoryProvider, spawn_context_thread
assert pathlib.Path(inspect.getfile(MemoryProvider)).resolve() == host / "agent/memory_provider.py"
spec = importlib.util.spec_from_file_location("offline_palace", plugin)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
assert module.spawn_context_thread is spawn_context_thread
marker = contextvars.ContextVar("real-host-profile", default="wrong-profile")
token = marker.set("official-helper-control")
seen = []
thread = spawn_context_thread(lambda: seen.append(marker.get()), name="offline-control")
thread.start(); thread.join(3)
assert seen == ["official-helper-control"]
marker.reset(token)
assert issubclass(module.PalaceOfTruthMemoryProvider, MemoryProvider)
for hook in ("prefetch", "sync", "mirror"):
    p = module.PalaceOfTruthMemoryProvider()
    p._has_api_auth = lambda: True
    p._resolve_tenant_id = lambda: "offline-tenant"
    seen = []
    def offline(*args, **kwargs):
        seen.append(marker.get())
        return "recall" if hook == "prefetch" else {"ok": True}
    p._prefetch_with_budget = offline
    p._post_memory_entries = offline
    token = marker.set(hook + "-profile")
    try:
        if hook == "prefetch":
            p.queue_prefetch("query", session_id="offline-session")
            thread = p._prefetch_thread
        elif hook == "sync":
            p.sync_turn("user", "assistant", session_id="offline-session")
            thread = p._sync_thread
        else:
            p.on_memory_write("add", "memory", "fixture")
            thread = p._sync_thread
        thread.join(3)
        assert not thread.is_alive()
        assert seen == [hook + "-profile"], (hook, seen)
    finally:
        p.shutdown()
        marker.reset(token)
print(json.dumps({"official_helper": "passed", "real_abc": "passed", "plugin_hooks": ["prefetch", "sync", "mirror"], "network": "denied by audit hook", "scope": "caller ContextVars only; not drain or remote durability"}))
'''
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script, str(host), str(PLUGIN_PATH)],
        env={"HOME": str(tmp_path), "HERMES_HOME": str(tmp_path / ".hermes"),
             "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["official_helper"] == receipt["real_abc"] == "passed"
