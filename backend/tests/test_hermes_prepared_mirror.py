"""Offline completion-bound Palace mirror regressions."""
import contextvars
import threading

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
