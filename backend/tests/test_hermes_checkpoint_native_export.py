"""F1/F2 inverse acceptance on the exported package, not fixture composition."""
import contextvars
import threading
import time
import pytest
from tests.test_hermes_checkpoint_repair import ready as fixture_ready, wire, event


def ready():
    import os
    if not os.environ.get('SAR1406_NORMAL_LOADER'):
        return fixture_ready()
    import sys
    from plugins.memory import load_memory_provider
    p = load_memory_provider('palaceoftruth', register_skills=False)
    assert p is not None
    assert type(p).__module__.startswith('_hermes_user_memory.')
    p._config = {'precompression_checkpoint_enabled': True, 'agent_name': 'barbara'}
    p._base_url = 'https://fixture.invalid'
    p._api_key = 'fixture-only'
    p._session_id = 'session-fixture'
    p._tenant_id = 'tenant-fixture'
    p._scope_type = 'agent'
    p._scope_key = 'barbara'
    return sys.modules[type(p).__module__], p


def test_export_contains_native_lifecycle_and_checkpoint():
    _, p = ready()
    assert hasattr(p, '_write_queue')
    assert callable(p.flush_pending)
    assert callable(p.on_pre_compress)
    calls, _ = wire(p)
    assert p.on_pre_compress([event()], require_checkpoint=True)
    assert p.flush_pending(timeout=1)
    state = p.shutdown_drain_state
    assert state['accepted_writes'] == state['completed_writes'] == 1
    assert state['active_tasks'] == state['failed_writes'] == 0
    assert len(calls) == 2


def test_closed_capture_rejected_no_context_lost_and_old_quota_not_reset():
    _, p = ready()
    profile = contextvars.ContextVar('profile', default='missing')
    token = profile.set('submitted')
    entered, release = threading.Event(), threading.Event()
    seen = []
    p._resolve_tenant_id = lambda: 'tenant-fixture'
    def transport(*a, **kw):
        seen.append(profile.get()); entered.set(); release.wait(1)
    p._post_memory_entries = transport
    p.sync_turn('user', 'assistant', session_id=p._session_id)
    assert entered.wait(1)
    p._reset_write_quota()
    p._turn_write_count = 3
    release.set()
    assert p.flush_pending(timeout=1)
    assert seen == ['submitted']
    assert p._turn_write_count == 3
    p.shutdown(timeout=1)
    with pytest.raises(RuntimeError):
        p.sync_turn('later', 'later', session_id=p._session_id)
    assert seen == ['submitted']
    profile.reset(token)


def test_checkpoint_cleanup_caller_deadline_and_eventual_accounting(monkeypatch):
    _, p = ready()
    wire(p)
    monkeypatch.setattr(type(p), 'CHECKPOINT_TOTAL_SECONDS', .05)
    original = p._checkpoint_request_json
    locked, release, done = threading.Event(), threading.Event(), threading.Event()
    errors = []
    def owner():
        with p._lifecycle_lock:
            locked.set(); release.wait(1)
    holder = threading.Thread(target=owner)
    def transport(method, path, payload=None, **kw):
        result = original(method, path, payload, **kw)
        if method == 'GET':
            holder.start(); assert locked.wait(.5)
        return result
    p._checkpoint_request_json = transport
    def run():
        try: p.on_pre_compress([event()], require_checkpoint=True)
        except RuntimeError: errors.append(True)
        finally: done.set()
    runner = threading.Thread(target=run); runner.start()
    assert locked.wait(.5)
    try:
        assert done.wait(.18)
        start = time.perf_counter()
        assert p.flush_pending(timeout=.03) is False
        assert time.perf_counter() - start < .15
        start = time.perf_counter()
        p.shutdown(timeout=.03)
        assert time.perf_counter() - start < .15
    finally:
        release.set(); holder.join(1); runner.join(1)
    assert errors == [True]
    assert p.flush_pending(timeout=1)
    state = p.shutdown_drain_state
    assert state['active_tasks'] == state['queued_writes'] == 0
    assert state['accepted_writes'] == state['failed_writes'] == 1
    assert state['completed_writes'] == 0


def test_cron_explicit_546_write_allowed_automatic_checkpoint_rejected(monkeypatch):
    import json
    module, p = ready()
    monkeypatch.setattr(module, '_load_config', lambda *_: {
        'precompression_checkpoint_enabled': True, 'base_url': 'https://fixture.invalid',
        'api_key': 'fixture', 'scope_type': 'agent', 'scope_key': 'barbara',
    })
    p.initialize('cron_fixture', agent_context='cron', platform='cron')
    p._tenant_id = 'tenant-fixture'
    seen = []
    p._request_json = lambda *a, **kw: seen.append(a) or {'job_id': 'fixture'}
    result = json.loads(p.handle_tool_call('palace_remember', {
        'content': 'explicit operator fixture', 'target': 'memory',
    }))
    assert result['ok'] is True and len(seen) == 1
    calls, _ = wire(p)
    with pytest.raises(RuntimeError):
        p.on_pre_compress([event()], require_checkpoint=True)
    p.sync_turn('automatic', 'automatic', session_id=p._session_id)
    assert not calls and p._accepted_writes == 0


def test_flush_wait_reacquisition_cannot_exceed_deadline():
    _, p = ready()
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    def worker(): entered.set(); release.wait(1)
    with p._lifecycle_lock:
        p._queue_write(worker, p._quota_for_session(p._session_id))
    assert entered.wait(1)
    result = []
    def flush():
        result.append(p.flush_pending(timeout=.04)); done.set()
    thread = threading.Thread(target=flush); thread.start()
    # Let the barrier enter its wait, then contend its re-acquisition.
    time.sleep(.01)
    p._lifecycle_lock.acquire()
    try:
        assert done.wait(.15), 'Condition timeout must not reacquire without budget'
        assert result == [False]
    finally:
        p._lifecycle_lock.release(); release.set(); thread.join(1)
    assert p.flush_pending(timeout=1)
