"""Offline session admission versus FIFO execution contract."""
import pytest
from .test_hermes_memory_lifecycle import provider  # noqa: F401


def test_prepared_sessions_keep_execution_and_quotas_separate(provider, monkeypatch):
    seen = []
    monkeypatch.setattr(provider, "on_session_end", lambda messages: seen.append(
        (provider._session_id, provider._active_write_quota())), raising=False)
    old = provider._write_quota
    publish, complete = provider.prepare_session_boundary([], new_session_id="new")
    assert provider._snapshot_write_context()["session_id"] == "old-session"
    with pytest.raises(RuntimeError, match="published"):
        complete()
    publish()
    new = provider._write_quota
    publish()
    assert provider._write_quota is new
    assert provider._session_id == "old-session"
    assert provider._snapshot_write_context()["session_id"] == "new"
    publish2, complete2 = provider.prepare_session_boundary([], new_session_id="newer")
    publish2()
    newest = provider._write_quota
    complete()
    complete()
    complete2()
    assert seen == [("old-session", old), ("new", new)]
    assert provider._session_id == "newer"
    assert provider._write_quota is newest
    assert len({id(q.session_writes) for q in (old, new, newest)}) == 3


def test_discarded_and_stale_session_tokens(provider):
    old = provider._write_quota
    publish, complete = provider.prepare_session_boundary([], new_session_id="new")
    assert provider._write_quota is old
    assert provider._snapshot_write_context()["session_id"] == "old-session"
    provider.on_session_switch("other")
    with pytest.raises(RuntimeError, match="stale"):
        publish()
    with pytest.raises(RuntimeError, match="published"):
        complete()
    assert provider._snapshot_write_context()["session_id"] == "other"


def test_closed_session_publication_fails(provider):
    publish, _ = provider.prepare_session_boundary([], new_session_id="new")
    provider.shutdown()
    with pytest.raises(RuntimeError, match="closed"):
        publish()
