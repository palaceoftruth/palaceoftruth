"""Deterministic, offline lifecycle regressions for the portable provider."""
import contextvars
import threading

import pytest

from .test_hermes_memory_plugin import load_palaceoftruth_plugin


@pytest.fixture(params=["legacy-host", "context-aware-host"])
def provider(monkeypatch, request):
    def host_context_thread(target, *, name, daemon=True):
        context = contextvars.copy_context()
        return threading.Thread(target=lambda: context.run(target), name=name, daemon=daemon)

    factory = host_context_thread if request.param == "context-aware-host" else None
    module = load_palaceoftruth_plugin(context_thread_factory=factory)
    if factory is not None:
        assert module.spawn_context_thread is factory
    instance = module.PalaceOfTruthMemoryProvider()
    instance._session_id = "old-session"
    instance._scope_type = "session"
    instance._api_key = "offline-test-key"
    monkeypatch.setattr(instance, "_has_api_auth", lambda: True)
    monkeypatch.setattr(instance, "_resolve_tenant_id", lambda: "offline-tenant")
    monkeypatch.setattr(instance, "_post_memory_entries", lambda *args: None)
    yield instance
    instance.shutdown()


@pytest.mark.parametrize("operation", ["prefetch", "sync", "mirror"])
def test_background_work_inherits_callers_context(provider, monkeypatch, operation):
    profile = contextvars.ContextVar("test_profile", default="wrong-profile")
    seen = []
    if operation == "prefetch":
        monkeypatch.setattr(provider, "_prefetch_with_budget", lambda *args: seen.append(profile.get()) or "")
    else:
        monkeypatch.setattr(provider, "_resolve_tenant_id", lambda: seen.append(profile.get()) or "offline-tenant")
    token = profile.set("requested-profile")
    try:
        if operation == "prefetch":
            provider.queue_prefetch("query")
        elif operation == "sync":
            provider.sync_turn("user", "assistant")
        else:
            provider.on_memory_write("add", "memory", "content")
    finally:
        profile.reset(token)
    provider.shutdown()
    assert seen == ["requested-profile"]


@pytest.mark.parametrize("first_operation", ["sync", "mirror"])
@pytest.mark.parametrize("second_operation", ["sync", "mirror"])
def test_writes_are_fifo_nonblocking_and_keep_each_jobs_context(
    provider, monkeypatch, first_operation, second_operation
):
    profile = contextvars.ContextVar("queued_profile", default="wrong")
    entered, release, submitted = (threading.Event() for _ in range(3))
    seen = []

    def post(path, payload):
        if not seen:
            entered.set()
            assert release.wait(5)
        label = "first" if "first" in payload["body"] else "second"
        seen.append((label, profile.get()))

    monkeypatch.setattr(provider, "_post_memory_entries", post)
    token = profile.set("first-profile")
    if first_operation == "sync":
        provider.sync_turn("first", "answer")
    else:
        provider.on_memory_write("add", "memory", "first")
    profile.reset(token)
    assert entered.wait(2)

    def submit_second():
        profile.set("second-profile")
        if second_operation == "sync":
            provider.sync_turn("second", "answer")
        else:
            provider.on_memory_write("add", "memory", "second")
        submitted.set()

    submitter = threading.Thread(target=submit_second)
    submitter.start()
    try:
        assert submitted.wait(0.5), "submitting must not join an in-flight write"
        assert seen == [], "second write must not overtake the blocked first write"
    finally:
        release.set()
        submitter.join(3)
        provider.shutdown()
    assert seen == [("first", "first-profile"), ("second", "second-profile")]


@pytest.mark.parametrize("operation", ["sync", "mirror"])
def test_queued_write_keeps_submission_session_and_scope(provider, monkeypatch, operation):
    entered, release = threading.Event(), threading.Event()
    payloads = []
    provider._scope_type = "workspace"
    provider._agent_workspace = "old-workspace"

    def tenant():
        entered.set()
        assert release.wait(5)
        return "offline-tenant"

    monkeypatch.setattr(provider, "_resolve_tenant_id", tenant)
    monkeypatch.setattr(provider, "_post_memory_entries", lambda path, payload: payloads.append(payload))
    if operation == "sync":
        provider.sync_turn("user", "assistant")
    else:
        provider.on_memory_write("add", "memory", "content")
    try:
        assert entered.wait(2)
        provider.on_session_switch("new-session")
        provider._agent_workspace = "new-workspace"
        provider._scope_type = "session"
    finally:
        release.set()
        provider.shutdown()
    assert len(payloads) == 1
    assert payloads[0]["metadata"]["session_id"] == "old-session"
    assert payloads[0]["metadata"]["agent_workspace"] == "old-workspace"
    assert payloads[0]["scope"] == {"type": "workspace", "key": "old-workspace"}


def test_old_sync_completion_does_not_reset_new_turn_quota(provider, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def post(*args):
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(provider, "_post_memory_entries", post)
    provider.sync_turn("old user", "old assistant")
    try:
        assert entered.wait(2)
        provider.sync_turn("", "")
        provider._turn_write_count = 3
        provider._turn_bulk_call_count = 2
    finally:
        release.set()
        provider.shutdown()
    assert provider._turn_write_count == 3
    assert provider._turn_bulk_call_count == 2


def test_sync_finishing_during_next_turn_cannot_restore_its_allowance(provider, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def post(*args):
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(provider, "_post_memory_entries", post)
    provider._max_writes_per_turn = 2
    provider.sync_turn("turn A", "answer A")
    try:
        assert entered.wait(2)
        # Turn B is already active: there is no second sync_turn until it ends.
        provider._reserve_write_quota(is_bulk=False)
        provider._reserve_write_quota(is_bulk=False)
    finally:
        release.set()
        provider.shutdown()
    with pytest.raises(Exception, match="per-turn write cap exceeded"):
        provider._reserve_write_quota(is_bulk=False)


def test_old_session_backlog_does_not_charge_new_session(provider, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    seen = []

    def tenant():
        entered.set()
        assert release.wait(5)
        return "offline-tenant"

    monkeypatch.setattr(provider, "_resolve_tenant_id", tenant)
    # Exercise real quota reservation in the posting path, but no network.
    monkeypatch.setattr(provider, "_post_memory_entries", type(provider)._post_memory_entries.__get__(provider))
    monkeypatch.setattr(provider, "_request_json", lambda *args: seen.append(args[2]) or {})
    provider.on_memory_write("add", "memory", "old-session content")
    try:
        assert entered.wait(2)
        provider.on_session_switch("new-session")
        provider._reserve_write_quota(is_bulk=False)
    finally:
        release.set()
        provider.shutdown()
    assert len(seen) == 1
    assert provider._session_write_count == 1
    assert provider._turn_write_count == 1


@pytest.mark.parametrize("operation", ["prefetch", "sync", "mirror"])
def test_shutdown_rejects_new_background_work(provider, monkeypatch, operation):
    seen = []
    monkeypatch.setattr(provider, "_prefetch_with_budget", lambda *args: seen.append("read") or "")
    monkeypatch.setattr(provider, "_post_memory_entries", lambda *args: seen.append("write"))
    provider.shutdown()
    if operation == "prefetch":
        provider.queue_prefetch("late query")
    elif operation == "sync":
        provider.sync_turn("late user", "late assistant")
    else:
        provider.on_memory_write("add", "memory", "late content")
    provider.shutdown()
    assert seen == []


def test_shutdown_timeout_retains_worker_and_reports_unfinished_work(provider, monkeypatch, caplog):
    entered, release = threading.Event(), threading.Event()
    seen = []

    def post(path, payload):
        entered.set()
        assert release.wait(5)
        seen.append(payload["body"])

    monkeypatch.setattr(provider, "_post_memory_entries", post)
    provider.on_memory_write("add", "memory", "first")
    assert entered.wait(2)
    provider.on_memory_write("add", "memory", "second")
    worker = provider._sync_thread
    try:
        # Simulate exhausting the join budget without a slow wall-clock test.
        with monkeypatch.context() as m:
            m.setattr(worker, "join", lambda timeout=None: None)
            provider.shutdown()
        assert worker.is_alive()
        assert provider._sync_thread is worker
        assert "unfinished" in caplog.text.lower()
        provider.on_memory_write("add", "memory", "rejected")
    finally:
        release.set()
        provider.shutdown()
    assert not worker.is_alive()
    assert seen == ["first", "second"]
