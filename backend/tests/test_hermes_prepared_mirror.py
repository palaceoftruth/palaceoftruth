"""Offline completion-bound Palace mirror regressions."""
import contextvars
import threading

import pytest

from .test_hermes_memory_lifecycle import provider  # noqa: F401


def test_prepared_mirror_snapshots_without_io_and_waits_for_fifo(provider, monkeypatch):
    profile = contextvars.ContextVar("prepared_profile", default="wrong")
    entered, release, finished = (threading.Event() for _ in range(3))
    seen = []
    old_quota = provider._write_quota

    def post(path, payload):
        if payload["body"] == "first":
            entered.set()
            assert release.wait(5)
        seen.append((payload["body"], payload["metadata"]["session_id"],
                     profile.get(), provider._active_write_quota()))

    monkeypatch.setattr(provider, "_post_memory_entries", post)
    token = profile.set("admission-profile")
    try:
        completion = provider.prepare_memory_write("add", "memory", "second", metadata={})
    finally:
        profile.reset(token)
    assert callable(completion)
    assert seen == []
    assert not provider._write_queue
    provider.on_memory_write("add", "memory", "first")
    assert entered.wait(2)
    provider.on_session_switch("new-session")
    def run():
        completion()
        finished.set()
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert not finished.wait(0.1)
    finally:
        release.set()
        worker.join(3)
    assert finished.is_set()
    assert [row[0] for row in seen] == ["first", "second"]
    assert seen[1][1:] == ("old-session", "admission-profile", old_quota)


def test_prepared_mirror_filters_and_rejects_after_shutdown(provider, monkeypatch):
    seen = []
    monkeypatch.setattr(provider, "_post_memory_entries", lambda *args: seen.append(args))
    assert provider.prepare_memory_write("remove", "memory", "x") is None
    completion = provider.prepare_memory_write("add", "memory", "x")
    provider.shutdown()
    completion()
    assert provider.prepare_memory_write("add", "memory", "x") is None
    assert seen == []


@pytest.mark.parametrize("mode", ["legacy", "prepared"])
def test_callback_mirror_inside_old_session_completion_binds_old_quota(
    provider, monkeypatch, mode
):
    """Completion callbacks must debit the ending session's quota, not the new one.

    ``publish()`` admits the newer session and rotates admission before
    ``complete()`` runs extraction. A mirror issued inside that completion
    (legacy ``on_memory_write`` or a prepared completion token) must still be
    bound to the snapshot/ending quota; newer admission counters stay untouched.
    """
    observed = []
    completion_binding = []
    pending = []
    old_quota = provider._write_quota
    monkeypatch.setattr(provider, "_write_quotas_enabled", True)

    def post(method, path, payload, **kwargs):  # noqa: ARG001
        observed.append((
            payload["metadata"]["session_id"],
            provider._active_write_quota() is old_quota,
            provider._turn_write_count,
            provider._session_write_count,
        ))
        return {}

    # Restore the real transport wrapper (the shared fixture stubs it) but stub
    # only the HTTP call, so quota reservation still happens as in production.
    monkeypatch.setattr(
        provider, "_post_memory_entries", type(provider)._post_memory_entries.__get__(provider)
    )
    monkeypatch.setattr(provider, "_request_json", post)

    def finish(messages):  # noqa: ARG001
        completion_binding.append(provider._active_write_quota() is old_quota)
        if mode == "legacy":
            provider.on_memory_write("add", "memory", "callback-origin write")
        else:
            pending.append(
                provider.prepare_memory_write("add", "memory", "callback-origin write")
            )

    provider.on_session_end = finish
    publish, complete = provider.prepare_session_boundary([], new_session_id="new-session")
    publish()
    newer = provider._write_quota
    assert newer is not old_quota
    newer_counters = (newer.turn_writes, newer.session_writes[0])
    complete()
    for completion in pending:
        assert callable(completion)
        completion()
    # Legacy mirrors run on the shared FIFO worker; wait for it to drain.
    assert provider._write_idle.wait(5)

    assert completion_binding == [True]
    assert [(session, is_old) for session, is_old, _, _ in observed] == [("old-session", True)]
    # Reservation landed on the ending quota; the newer admission counters are
    # byte-for-byte unchanged.
    assert [(turn, session) for _, _, turn, session in observed] == [(1, 1)]
    assert old_quota.turn_writes == 1
    assert provider._active_write_quota() is newer
    assert (provider._write_quota.turn_writes, provider._write_quota.session_writes[0]) == (
        newer_counters
    )
