"""Deterministic FIFO barrier / order tests, separated from real SQLite durability.

The provider-owned outbox processes accepted writes on one FIFO drain worker. The
old harness asserted ``flush_pending(timeout=1)``, which silently required the
durable FULL-sync SQLite bootstrap to finish inside one second and so failed
intermittently on a loaded runner. These tests drive completion with an explicit
event-controlled store (the real plugin writer still runs) so barrier, ordering,
ownership and notification semantics are deterministic, while real-SQLite
durability stays in its own test below without any wall-clock budget.
"""
import hashlib
import json
import sqlite3
import threading
import time

import pytest

from tests.test_hermes_outbox import setup, drain, TENANT


class ControlledStore:
    """Minimal outbox store protocol with a deterministic commit gate.

    Substitutes only the plugin's ``OutboxStore``; the provider's real writer,
    FIFO queue, context capture and receipt handling are exercised unchanged.
    """

    def __init__(self, directory, binding, *, blocked):
        self.binding = binding
        self.directory = str(directory)
        self._blocked = blocked
        self.entered = threading.Event()
        self.release = threading.Event()
        if not blocked:
            self.release.set()
        self.admissions = []

    def is_duplicate(self, payload):
        return False

    def admit(self, payload, *, max_session):
        self.entered.set()
        assert self.release.wait(10), 'fixture commit gate was never released'
        stored = json.loads(json.dumps(payload, sort_keys=True))
        self.admissions.append(stored)
        return {
            'local_durable': True,
            'remote_durable': False,
            'boundary': 'local_sqlite_FULL_commit_readback',
            'idempotency_key': stored['idempotency_key'],
            'payload_sha256': hashlib.sha256(
                json.dumps(stored, sort_keys=True, separators=(',', ':')).encode()
            ).hexdigest(),
            'duplicate': False,
        }


class StoreFactory:
    """Stand-in for ``OutboxStore``; exposes the single created instance."""

    def __init__(self, *, blocked):
        self.blocked = blocked
        self.last = None

    def __call__(self, directory, binding):
        self.last = ControlledStore(directory, binding, blocked=self.blocked)
        return self.last


def _provider(tmp_path, monkeypatch, outbox, *, blocked):
    factory = StoreFactory(blocked=blocked)
    monkeypatch.setattr(outbox, 'OutboxStore', factory)
    return factory


def _store(factory, timeout=10.0):
    """Wait for the drain worker to create the fixture store (it is lazy)."""
    deadline = time.perf_counter() + timeout
    while factory.last is None and time.perf_counter() < deadline:
        time.sleep(0.002)
    assert factory.last is not None, 'worker did not create the fixture store'
    return factory.last


def _submit(p, index):
    p.on_turn_start(index, f'u{index}', author_id='alice', author_is_bot=False)
    p.sync_turn(f'u{index}', f'a{index}', session_id=p._session_id,
                turn_author={'id': 'alice', 'is_bot': False})


def test_flush_deadline_is_absolute_while_commit_blocked_then_completes(tmp_path, monkeypatch):
    outbox, p, binding = setup(tmp_path)
    factory = _provider(tmp_path, monkeypatch, outbox, blocked=True)
    posts = []
    p._request_json = lambda *a, **k: posts.append(a) or {}
    _submit(p, 1)
    store = _store(factory)
    assert store.entered.wait(10), 'worker did not reach the blocked commit'
    # Nothing is durable yet and the short-budget barrier must say so.
    assert p.last_outbox_receipt is None
    assert p.flush_pending(timeout=0.05) is False
    # Release the commit: the barrier then completes and the receipt is immutable.
    store.release.set()
    assert p.flush_pending(timeout=2) is True
    receipt = p.last_outbox_receipt
    assert receipt['local_durable'] is True and receipt['remote_durable'] is False
    assert receipt['boundary'] == 'local_sqlite_FULL_commit_readback'
    assert len(store.admissions) == 1
    assert store.admissions[0]['tenant_id'] == TENANT
    assert store.admissions[0]['scope'] == {'type': 'agent', 'key': 'barbara'}
    assert posts == []


def test_fast_path_flush_completes_without_network(tmp_path, monkeypatch):
    outbox, p, binding = setup(tmp_path)
    factory = _provider(tmp_path, monkeypatch, outbox, blocked=False)
    posts = []
    p._request_json = lambda *a, **k: posts.append(a) or {}
    _submit(p, 1)
    assert p.flush_pending(timeout=1) is True
    assert p.last_outbox_receipt['local_durable'] is True
    assert posts == [], 'provider outbox path must not perform a network POST'


def test_concurrent_flush_waiters_all_observe_completion(tmp_path, monkeypatch):
    outbox, p, binding = setup(tmp_path)
    factory = _provider(tmp_path, monkeypatch, outbox, blocked=True)
    _submit(p, 1)
    store = _store(factory)
    assert store.entered.wait(10)
    results = []
    threads = [threading.Thread(target=lambda: results.append(p.flush_pending(timeout=5)))
               for _ in range(3)]
    for thread in threads:
        thread.start()
    time.sleep(0.05)
    store.release.set()
    for thread in threads:
        thread.join(6)
    assert results == [True, True, True], results
    assert p.last_outbox_receipt['local_durable'] is True


def test_fifo_order_preserved_across_blocked_then_released_commits(tmp_path, monkeypatch):
    outbox, p, binding = setup(tmp_path)
    factory = _provider(tmp_path, monkeypatch, outbox, blocked=True)
    for index in range(3):
        _submit(p, index)
    store = _store(factory)
    assert store.entered.wait(10)
    store.release.set()
    assert p.flush_pending(timeout=5) is True
    bodies = [row['body'] for row in store.admissions]
    assert len(bodies) == 3
    assert [b.index(f'u{i}') >= 0 for i, b in enumerate(bodies)] == [True, True, True]
    assert all(bodies[i].index(f'u{i}') < len(bodies[i]) for i in range(3))
    assert [row['metadata']['session_id'] for row in store.admissions] == [p._session_id] * 3


def test_cancellation_closes_admission_while_commit_blocked_then_completes(tmp_path, monkeypatch):
    outbox, p, binding = setup(tmp_path)
    factory = _provider(tmp_path, monkeypatch, outbox, blocked=True)
    _submit(p, 1)
    store = _store(factory)
    assert store.entered.wait(10)
    # Shutdown must close admission without waiting forever on the blocked commit.
    p.shutdown(timeout=0.05)
    with pytest.raises(RuntimeError):
        p.sync_turn('after close', 'after close')
    # The already-accepted write is still owned by the worker and completes.
    store.release.set()
    assert p.flush_pending(timeout=2) is True
    assert p.last_outbox_receipt['local_durable'] is True
    state = p.shutdown_drain_state
    assert state['completed_writes'] == 1 and state['active_tasks'] == 0


@pytest.mark.parametrize('iteration', range(5))
def test_slow_commit_barrier_is_deterministic(tmp_path, monkeypatch, iteration):
    outbox, p, binding = setup(tmp_path)
    factory = _provider(tmp_path, monkeypatch, outbox, blocked=True)
    _submit(p, 1)
    store = _store(factory)
    assert store.entered.wait(10)
    assert p.flush_pending(timeout=0.05) is False
    assert p.last_outbox_receipt is None
    store.release.set()
    assert p.flush_pending(timeout=2) is True
    assert p.last_outbox_receipt['local_durable'] is True


def test_real_sqlite_durability_completes_via_observable_wait(tmp_path):
    """Real FULL-sync SQLite commit, integrity and receipt. No wall-clock budget.

    Completion is awaited through ``drain`` (observable worker synchronization with
    a bounded integration ceiling); the provider's own flush budget is untouched.
    """
    outbox, p, binding = setup(tmp_path)
    posts = []
    p._request_json = lambda *a, **k: posts.append(a) or {}
    _submit(p, 1)
    assert drain(p)
    receipt = p.last_outbox_receipt
    assert receipt['local_durable'] is True and receipt['remote_durable'] is False
    assert receipt['boundary'] == 'local_sqlite_FULL_commit_readback'
    store = p._outbox_store
    rows = store.records()
    assert len(rows) == 1 and rows[0]['state'] == 'pending'
    assert rows[0]['payload']['tenant_id'] == TENANT
    with sqlite3.connect(str(store.path)) as con:
        assert con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert con.execute('PRAGMA journal_mode').fetchone()[0].lower() == 'delete'
    assert posts == []
