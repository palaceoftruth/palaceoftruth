"""Actual exported native session-quota ownership (no production initialization)."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.test_hermes_checkpoint_composed import composed
from tests.test_hermes_checkpoint_repair import event, wire

LATEST_SIBLING_SHA = '545dd75a584523ed8247f9a2c88c6cfab155cd40'


def test_checkpoint_charges_snapshot_session_not_stale_current_bucket():
    p = composed(); calls, _ = wire(p)
    with p._lifecycle_lock:
        correct = p._quota_for_session(p._session_id)
        unrelated = p._quota_for_session('unrelated-session')
        p._write_quota = unrelated  # Real delayed-epoch/stale-field condition.
    assert p.on_pre_compress([event()], require_checkpoint=True)
    assert p.flush_pending(timeout=1)
    assert correct.session_writes[0] == 1
    assert unrelated.session_writes[0] == 0
    assert not p._session_quota_jobs
    assert len(calls) == 2


def test_checkpoint_cannot_bypass_snapshot_session_cap_using_other_quota():
    p = composed(); calls, _ = wire(p)
    with p._lifecycle_lock:
        correct = p._quota_for_session(p._session_id)
        correct.session_writes[0] = p._max_writes_per_session
        unrelated = p._quota_for_session('unrelated-session')
        p._write_quota = unrelated
    with pytest.raises(RuntimeError): p.on_pre_compress([event()], require_checkpoint=True)
    assert p.flush_pending(timeout=1)
    assert not calls
    assert correct.session_writes[0] == p._max_writes_per_session
    assert unrelated.session_writes[0] == 0
    assert p.shutdown_drain_state['status'] == 'failed'


def test_queued_checkpoint_keeps_captured_turn_quota_across_rotation(monkeypatch):
    p = composed(); calls, _ = wire(p)
    entered, release, admitted = threading.Event(), threading.Event(), threading.Event()
    def blocker(): entered.set(); assert release.wait(2)
    with p._lifecycle_lock:
        correct = p._quota_for_session(p._session_id)
        unrelated = p._quota_for_session('unrelated-session')
        p._queue_write(blocker, unrelated)
    assert entered.wait(1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(p.on_pre_compress, [event()], require_checkpoint=True)
        try:
            import time
            until = time.perf_counter() + 1
            while time.perf_counter() < until:
                with p._lifecycle_lock:
                    if p._accepted_writes == 2:
                        admitted.set()
                        break
                time.sleep(.001)
            assert admitted.is_set()
            with p._lifecycle_lock:
                assert p._session_quota_jobs[p._session_id] == 1
                p._reset_write_quota(session_id=p._session_id)
                rotated = p._quota_for_session(p._session_id)
                assert rotated is not correct
        finally: release.set()
        assert 'durable' in result.result(timeout=2)
    assert p.flush_pending(timeout=1)
    assert correct.turn_writes == 1 and rotated.turn_writes == 0
    assert correct.session_writes is rotated.session_writes
    assert rotated.session_writes[0] == 1 and unrelated.session_writes[0] == 0
    assert not p._session_quota_jobs
    assert len(calls) == 2


@pytest.mark.parametrize('gate', ['suppressed', 'closed'])
def test_latest_lifecycle_gate_cannot_be_ignored(gate):
    p = composed(); calls, _ = wire(p)
    if gate == 'suppressed': p._lifecycle_writes_disabled = True
    else: p.shutdown(timeout=1)
    with pytest.raises(RuntimeError): p.on_pre_compress([event()], require_checkpoint=True)
    assert not calls
    assert p._accepted_writes == 0


def test_historical_source_hash_preserved_actual_export_executes():
    p = composed(reviewed=True); wire(p)
    assert p.on_pre_compress([event()], require_checkpoint=True)
    assert p.flush_pending(timeout=1)
    assert p._completed_writes == 1
