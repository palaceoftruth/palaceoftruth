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


def test_prepared_publication_invalidates_cache_once(provider, monkeypatch):
    monkeypatch.setattr(provider, "on_session_end", lambda messages: None, raising=False)
    provider._prefetch_cache = {"text": "old"}
    provider._tenant_id = "old-tenant"
    provider._server_identity_loaded = True
    provider._server_agent_scope_key = "old-agent"
    provider._server_containment_mode = "old-mode"
    publish, complete = provider.prepare_session_boundary([], new_session_id="new")
    assert provider._prefetch_cache["text"] == "old"
    publish()
    assert provider._prefetch_cache == {
        "query": "", "session_id": "", "workspace": "", "text": "",
        "epoch": provider._current_turn_epoch(),
    }
    assert (provider._tenant_id, provider._server_identity_loaded,
            provider._server_agent_scope_key, provider._server_containment_mode) == (
                "", False, "", "")
    # Repeated publication and delayed completion must preserve newer cache data.
    provider._prefetch_cache["text"] = "new text"
    provider._tenant_id = "new-tenant"
    provider._server_identity_loaded = True
    provider._server_agent_scope_key = "new-agent"
    provider._server_containment_mode = "new-mode"
    publish()
    complete()
    assert (provider._prefetch_cache["text"], provider._tenant_id,
            provider._server_identity_loaded, provider._server_agent_scope_key,
            provider._server_containment_mode) == (
                "new text", "new-tenant", True, "new-agent", "new-mode")


def test_closed_session_publication_fails(provider):
    publish, _ = provider.prepare_session_boundary([], new_session_id="new")
    provider.shutdown()
    with pytest.raises(RuntimeError, match="closed"):
        publish()
