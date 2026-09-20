"""Offline provider half of the prepared sync admission contract."""
import contextvars
import threading

import pytest
from .test_hermes_memory_lifecycle import provider  # noqa: F401


def test_prepared_sync_publishes_once_and_preserves_ending_quota(provider, monkeypatch):
    seen = []
    monkeypatch.setattr(provider, "_post_memory_entries", lambda *args: seen.append(provider._active_write_quota()))
    old = provider._write_quota
    publish, complete = provider.prepare_sync_turn("user", "assistant")
    assert provider._write_quota is old
    assert seen == []
    with pytest.raises(RuntimeError, match="published"):
        complete()
    publish()
    newer = provider._write_quota
    assert newer is not old
    assert newer.session_writes is old.session_writes
    publish()
    assert provider._write_quota is newer
    provider._reset_write_quota()
    newest = provider._write_quota
    complete()
    complete()
    assert seen == [old]
    assert provider._write_quota is newest


def test_prepared_sync_rejects_stale_publication(provider):
    publish, complete = provider.prepare_sync_turn("u", "a")
    provider.on_session_switch("new-session")
    quota = provider._write_quota
    with pytest.raises(RuntimeError, match="stale"):
        publish()
    with pytest.raises(RuntimeError, match="published"):
        complete()
    assert provider._write_quota is quota


def test_prepared_sync_freezes_context_and_waits_for_transport(provider, monkeypatch):
    profile = contextvars.ContextVar("sync_profile", default="wrong")
    entered, release, done = (threading.Event() for _ in range(3))
    seen = []
    def post(path, payload):
        entered.set()
        assert release.wait(3)
        seen.append((profile.get(), payload["metadata"]["session_id"]))
    monkeypatch.setattr(provider, "_post_memory_entries", post)
    context_token = profile.set("prepared-profile")
    try:
        publish, complete = provider.prepare_sync_turn("u", "a")
    finally:
        profile.reset(context_token)
    publish()
    provider.on_session_switch("new-session")
    def run():
        complete()
        done.set()
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(2)
        assert not done.wait(0.05)
    finally:
        release.set()
        worker.join(3)
    assert done.is_set()
    assert seen == [("prepared-profile", "old-session")]


def test_discarded_sync_token_does_not_rotate_or_write(provider, monkeypatch):
    seen = []
    monkeypatch.setattr(provider, "_post_memory_entries", lambda *args: seen.append(args))
    quota = provider._write_quota
    epoch = provider._current_turn_epoch()
    provider.prepare_sync_turn("u", "a")  # Host rejected enqueue: discard both callbacks.
    assert provider._write_quota is quota
    assert provider._current_turn_epoch() == epoch
    assert not provider._write_queue
    assert seen == []


def test_prepared_sync_rejects_closed_publication(provider):
    publish, _ = provider.prepare_sync_turn("u", "a")
    provider.shutdown()
    with pytest.raises(RuntimeError, match="closed"):
        publish()
