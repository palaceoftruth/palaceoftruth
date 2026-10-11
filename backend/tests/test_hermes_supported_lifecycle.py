"""TDD lifecycle acceptance; offline transports and event-driven concurrency."""
from __future__ import annotations

import contextvars
import threading
from time import perf_counter

import pytest

from tests.test_hermes_memory_plugin import load_palaceoftruth_plugin


@pytest.fixture
def provider(monkeypatch):
    module = load_palaceoftruth_plugin()
    p = module.PalaceOfTruthMemoryProvider()
    p._session_id = "old"
    p._scope_type = "session"
    p._scope_key = ""
    monkeypatch.setattr(p, "_has_api_auth", lambda: True)
    monkeypatch.setattr(p, "_resolve_tenant_id", lambda: "offline-tenant")
    yield p
    p.shutdown()


def test_fifo_nonblocking_admission_and_context(provider, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    marker = contextvars.ContextVar("profile", default="wrong")
    writes = []

    def transport(method, path, payload):
        if "first" in payload["body"]:
            entered.set()
            assert release.wait(5)
        writes.append((payload["body"], marker.get()))
        return {"ok": True}

    monkeypatch.setattr(provider, "_request_json", transport)
    token = marker.set("first-profile")
    try:
        provider.sync_turn("first", "answer", session_id="old")
        assert entered.wait(3)
        marker.set("second-profile")
        started = perf_counter()
        provider.on_memory_write("add", "memory", "second")
        assert perf_counter() - started < 0.2, "Admission waited for old transport"
        assert len(writes) == 0, "Later write overtook blocked first write"
        assert provider.flush_pending(timeout=0.02) is False
        release.set()
        assert provider.flush_pending(timeout=3)
        assert [x[1] for x in writes] == ["first-profile", "second-profile"]
        assert "first" in writes[0][0] and "second" in writes[1][0]
    finally:
        release.set()
        marker.reset(token)


@pytest.mark.parametrize("hook", ["sync", "mirror"])
def test_submission_snapshot_before_tenant_lookup(provider, monkeypatch, hook):
    entered, release = threading.Event(), threading.Event()
    writes = []

    def tenant():
        entered.set()
        assert release.wait(3)
        return "offline-tenant"

    monkeypatch.setattr(provider, "_resolve_tenant_id", tenant)
    monkeypatch.setattr(provider, "_request_json", lambda method, path, payload: writes.append(payload) or {"ok": True})
    try:
        if hook == "sync":
            provider.sync_turn("snapshot", "answer", session_id="old")
        else:
            provider.on_memory_write("add", "memory", "snapshot")
        assert entered.wait(3)
        provider.on_session_switch("new")
        provider._scope_type = "workspace"
        provider._scope_key = "new-workspace"
        provider._agent_workspace = "new-workspace"
        release.set()
        assert provider.flush_pending(timeout=3)
        assert writes[0]["metadata"]["session_id"] == "old"
        assert writes[0]["scope"] == {"type": "session", "key": "old"}
        assert writes[0]["metadata"]["agent_workspace"] != "new-workspace"
    finally:
        release.set()


def test_old_completion_cannot_reset_new_session_quota(provider, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def transport(*args):
        entered.set()
        assert release.wait(3)
        return {"ok": True}

    monkeypatch.setattr(provider, "_request_json", transport)
    try:
        provider.sync_turn("quota", "answer", session_id="old")
        assert entered.wait(3)
        provider.on_session_switch("new")
        provider._reserve_write_quota(is_bulk=False)
        release.set()
        assert provider.flush_pending(timeout=3)
        assert provider._turn_write_count == provider._session_write_count == 1
    finally:
        release.set()


def test_shutdown_timeout_retains_all_work_and_rejects_late_admission(provider, monkeypatch, caplog):
    entered, release = threading.Event(), threading.Event()
    writes = []

    def transport(method, path, payload):
        entered.set()
        assert release.wait(4)
        writes.append(payload)
        return {"ok": True}

    monkeypatch.setattr(provider, "_request_json", transport)
    try:
        provider.sync_turn("first", "answer")
        assert entered.wait(3)
        provider.on_memory_write("add", "memory", "queued")
        provider.shutdown(timeout=0.02)
        state = provider.shutdown_drain_state
        assert state["status"] == "timed_out"
        assert state["active_tasks"] == 1 and state["queued_writes"] == 1
        assert "unfinished" in caplog.text.lower()
        provider.shutdown(timeout=0.01)
        assert provider.shutdown_drain_state["status"] == "timed_out"
        with pytest.raises(RuntimeError, match="closed"):
            provider.on_memory_write("add", "memory", "late")
        release.set()
        assert provider.flush_pending(timeout=3)
        provider.shutdown(timeout=0.02)
        assert provider.shutdown_drain_state["status"] == "drained"
        assert len(writes) == 2
    finally:
        release.set()


def test_transport_failure_visible_and_fifo_continues(provider, monkeypatch):
    writes = []

    def transport(method, path, payload):
        if "bad" in payload["body"]:
            raise RuntimeError("offline transport failed")
        writes.append(payload)
        return {"ok": True}

    monkeypatch.setattr(provider, "_request_json", transport)
    provider.sync_turn("bad", "answer")
    provider.on_memory_write("add", "memory", "good")
    assert provider.flush_pending(timeout=3)
    provider.shutdown(timeout=0.02)
    state = provider.shutdown_drain_state
    assert state["status"] == "failed"
    assert state["failed_writes"] == 1 and state["completed_writes"] == 1
    assert len(writes) == 1


def test_concurrent_admission_single_writer(provider, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    active = maximum = 0
    writes = []
    provider._max_writes_per_turn = 40

    def transport(method, path, payload):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        entered.set()
        assert release.wait(3)
        writes.append(payload["body"])
        with lock:
            active -= 1
        return {"ok": True}

    monkeypatch.setattr(provider, "_request_json", transport)
    threads = [threading.Thread(target=lambda i=i: provider.on_memory_write("add", "memory", f"fixture-{i}")) for i in range(20)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(2)
            assert not t.is_alive(), "Admission blocked"
        assert entered.wait(3)
        release.set()
        assert provider.flush_pending(timeout=3)
        assert maximum == 1 and len(set(writes)) == 20
        assert provider.shutdown_drain_state["accepted_writes"] == 20
    finally:
        release.set()


def test_prefetch_closed_admission_and_bounded_tracking(provider, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def recall(*args):
        entered.set()
        assert release.wait(3)
        return "fixture"

    monkeypatch.setattr(provider, "_prefetch_with_budget", recall)
    try:
        provider.queue_prefetch("query")
        assert entered.wait(3)
        provider.shutdown(timeout=0.02)
        assert provider.shutdown_drain_state["active_prefetches"] == 1
        assert provider.shutdown_drain_state["status"] == "timed_out"
        with pytest.raises(RuntimeError, match="closed"):
            provider.queue_prefetch("late")
        release.set()
        assert provider.flush_pending(timeout=3)
    finally:
        release.set()


def test_thread_start_failure_rolls_back_admission_and_allows_retry(provider, monkeypatch):
    module_globals = provider._queue_write.__func__.__globals__
    original = module_globals["spawn_context_thread"]
    class FailedThread:
        def start(self):
            raise RuntimeError("offline start failure")
    monkeypatch.setitem(module_globals, "spawn_context_thread", lambda *args, **kwargs: FailedThread())
    with pytest.raises(RuntimeError, match="start failure"):
        provider.sync_turn("retry", "answer")
    assert provider.shutdown_drain_state["accepted_writes"] == 0
    assert provider.shutdown_drain_state["queued_writes"] == 0
    monkeypatch.setitem(module_globals, "spawn_context_thread", original)
    writes = []
    monkeypatch.setattr(provider, "_request_json", lambda method, path, payload: writes.append(payload) or {"ok": True})
    provider.sync_turn("retry", "answer")
    assert provider.flush_pending(timeout=3)
    assert len(writes) == 1


def test_failed_write_retry_preserves_idempotency(provider, monkeypatch):
    keys = []
    def transport(method, path, payload):
        keys.append(payload["idempotency_key"])
        if len(keys) == 1:
            raise RuntimeError("offline first attempt")
        return {"ok": True}
    monkeypatch.setattr(provider, "_request_json", transport)
    provider.sync_turn("retry", "same answer")
    assert provider.flush_pending(timeout=3)
    provider.sync_turn("retry", "same answer")
    assert provider.flush_pending(timeout=3)
    assert len(keys) == 2 and keys[0] == keys[1]
    assert provider.shutdown_drain_state["failed_writes"] == 1
    assert provider.shutdown_drain_state["completed_writes"] == 1


@pytest.mark.parametrize("completed_prior", [False, True])
def test_prefetch_start_failure_flush_retry_shutdown(provider, monkeypatch, completed_prior):
    reads = []
    monkeypatch.setattr(provider, "_prefetch_with_budget", lambda query, session: reads.append(query) or "offline recall")
    if completed_prior:
        provider.queue_prefetch("prior")
        assert provider.flush_pending(timeout=3)
    prior = provider._prefetch_thread
    module_globals = provider._queue_prefetch.__func__.__globals__
    original = module_globals["spawn_context_thread"]

    def failed_factory(target, **kwargs):
        thread = threading.Thread(target=target)
        def fail():
            raise RuntimeError("offline prefetch start failure")
        thread.start = fail
        return thread

    monkeypatch.setitem(module_globals, "spawn_context_thread", failed_factory)
    with pytest.raises(RuntimeError, match="prefetch start failure"):
        provider.queue_prefetch("rejected")
    assert provider._prefetch_thread is prior
    assert provider.flush_pending(timeout=0.1)
    monkeypatch.setitem(module_globals, "spawn_context_thread", original)
    provider.queue_prefetch("retry")
    assert provider.flush_pending(timeout=3)
    assert reads == (["prior", "retry"] if completed_prior else ["retry"])
    provider.shutdown(timeout=0.1)
    assert provider.shutdown_drain_state["status"] == "drained"
    assert provider.shutdown_drain_state["active_prefetches"] == 0
