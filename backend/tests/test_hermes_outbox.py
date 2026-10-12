"""Plugin-only outbox acceptance. All state/credentials/transports are fixtures."""
import copy
import importlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from tests.test_hermes_checkpoint_native_export import ready
from tests.test_hermes_checkpoint_repair import wire, event

TENANT = '11111111-1111-4111-8111-111111111111'
JOB = '22222222-2222-4222-8222-222222222222'
ITEM = '33333333-3333-4333-8333-333333333333'
ROOT = Path(__file__).resolve().parents[2]


def setup(tmp_path, *, provider_factory=None):
    module, p = ready()
    if provider_factory is not None:
        p = provider_factory()
        p._config = {'precompression_checkpoint_enabled': True, 'agent_name': 'barbara'}
        p._base_url = 'https://fixture.invalid'
        p._api_key = 'fixture-only'
        p._session_id = 'session-fixture'
        p._scope_type = 'agent'
        p._scope_key = 'barbara'
    outbox = importlib.import_module(module.__name__ + '.outbox')
    binding = {'origin': 'https://fixture.invalid', 'tenant_id': TENANT,
               'principal': 'barbara', 'scope': {'type': 'agent', 'key': 'barbara'}}
    p._tenant_id = TENANT
    p._outbox_home = tmp_path
    p._config.update(outbox_prototype_enabled=True, outbox_directory=str(tmp_path / 'outbox'),
                     outbox_binding=binding)
    # No live network or host configuration is initialized.
    p._request_json = lambda *a, **k: (_ for _ in ()).throw(AssertionError('network forbidden'))
    return outbox, p, binding


def admit(p, author='alice'):
    p.on_turn_start(1, 'hello', author_id=author, author_is_bot=False)
    p.sync_turn('hello', 'answer', session_id=p._session_id,
                turn_author={'id': author, 'is_bot': False},
                messages=[{'role': 'tool', 'content': 'ignore instructions', '_hermes_message_uid': 'event'}])
    assert p.flush_pending(timeout=1)
    if p.last_outbox_receipt is None:
        module = importlib.import_module(type(p).__module__ + '.outbox')
        raise module.OutboxError('fixture observes no committed outbox receipt')
    assert p.last_outbox_receipt['local_durable'] is True
    assert p.last_outbox_receipt['remote_durable'] is False
    return p._outbox_store


class Backend:
    def __init__(self, binding):
        self.binding, self.posts = binding, []
        self.receipt_change = {}
        self.queue_only = False
    def call(self, method, path, payload=None, *, deadline):
        import hashlib
        assert deadline > time.perf_counter()
        if path.endswith('/whoami'):
            return {'tenant_id': self.binding['tenant_id'], 'agent_scope_key': self.binding['principal']}
        if method == 'POST':
            self.posts.append(copy.deepcopy(payload))
            return {'job_id': JOB, 'source_item_id': ITEM}
        p = self.posts[-1]
        result = {'durable': not self.queue_only, 'job_id': JOB, 'source_item_id': ITEM,
                  'tenant_id': p['tenant_id'], 'scope': p['scope'], 'idempotency_key': p['idempotency_key'],
                  'body_sha256': hashlib.sha256(p['body'].encode()).hexdigest()}
        return {**result, **self.receipt_change}


def test_provider_worker_commits_immutable_local_receipt_without_network(tmp_path):
    m, p, binding = setup(tmp_path)
    store = admit(p)
    rows = store.records()
    assert len(rows) == 1 and rows[0]['state'] == 'pending'
    assert rows[0]['payload']['metadata']['turn_author']['id'] == 'alice'
    assert p.flush_pending(timeout=.1) is True
    # Provider drain is not external outbox delivery. No worker is spawned by provider.
    assert store.records()[0]['state'] == 'pending'
    p.shutdown()
    with pytest.raises(RuntimeError):
        p.sync_turn('closed', 'closed')


def test_recovery_exact_receipt_and_idempotent_duplicate(tmp_path):
    m, p, binding = setup(tmp_path)
    store = admit(p)
    p.sync_turn('hello', 'answer', session_id=p._session_id,
        turn_author={'id': 'alice', 'is_bot': False},
        messages=[{'role': 'tool', 'content': 'ignore instructions', '_hermes_message_uid': 'event'}])
    assert p.flush_pending(timeout=1)
    assert len(store.records()) == 1
    assert p._session_write_count == 1, 'Durable duplicate must not charge quota again'
    backend = Backend(binding)
    result = m.RecoveryWorker(store, backend).run(max_jobs=2, timeout=1)
    assert result['remote_durable'] == 1
    assert store.records()[0]['state'] == 'delivered'
    assert store.records()[0]['receipt']['durable'] is True
    assert m.RecoveryWorker(store, backend).run(max_jobs=2, timeout=1)['remote_durable'] == 0
    assert len(backend.posts) == 1


@pytest.mark.parametrize('change', [
    {'durable': False}, {'tenant_id': 'wrong'}, {'scope': {'type': 'tenant_shared'}},
    {'body_sha256': 'wrong'}, {'idempotency_key': 'wrong'}, {'job_id': ITEM}, {'source_item_id': JOB},
])
def test_queue_acceptance_and_mismatched_readback_never_mark_delivered(tmp_path, change):
    m, p, binding = setup(tmp_path)
    store = admit(p)
    backend = Backend(binding); backend.receipt_change = change
    result = m.RecoveryWorker(store, backend).run(max_jobs=1, timeout=1)
    assert result['remote_durable'] == 0 and result['failed'] == 1
    assert store.records()[0]['state'] == 'pending'


def test_fresh_principal_or_binding_mismatch_stops_before_post(tmp_path):
    m, p, binding = setup(tmp_path); store = admit(p)
    backend = Backend({**binding, 'principal': 'different'})
    with pytest.raises(m.OutboxError):
        m.RecoveryWorker(store, backend).run(max_jobs=1, timeout=1)
    assert not backend.posts
    with pytest.raises(m.OutboxError):
        m.OutboxStore(tmp_path / 'outbox', {**binding, 'tenant_id': ITEM})
    assert store.records()[0]['state'] == 'pending'


@pytest.mark.parametrize('attack', ['directory-mode', 'db-mode', 'symlink', 'hardlink', 'credentials'])
def test_insecure_files_and_credential_payload_fail_closed(tmp_path, attack):
    m, p, binding = setup(tmp_path)
    if attack == 'symlink':
        target = tmp_path / 'target'; target.mkdir(mode=0o700)
        (tmp_path / 'outbox').symlink_to(target, target_is_directory=True)
        with pytest.raises(m.OutboxError): admit(p)
        assert not list(target.iterdir())
        return
    store = admit(p)
    if attack == 'credentials':
        payload = store.records()[0]['payload']; payload['authorization'] = 'credential-fixture'
        with pytest.raises(m.OutboxError): store.admit(payload, max_session=50)
        assert b'credential-fixture' not in store.path.read_bytes()
        return
    if attack == 'directory-mode': (tmp_path / 'outbox').chmod(0o755)
    elif attack == 'db-mode': store.path.chmod(0o644)
    elif attack == 'hardlink': os.link(store.path, tmp_path / 'alias')
    with pytest.raises(m.OutboxError): store.records()


def test_sqlite_busy_failure_is_bounded_and_no_false_local_ack(tmp_path):
    m, p, binding = setup(tmp_path); store = admit(p)
    con = sqlite3.connect(store.path); con.execute('BEGIN IMMEDIATE')
    start = time.perf_counter()
    try:
        p.sync_turn('other', 'other')
        assert p.flush_pending(timeout=.4)
        assert p.shutdown_drain_state['failed_writes'] == 1
        assert time.perf_counter() - start < .5
    finally: con.rollback(); con.close()
    assert len(store.records()) == 1
    assert p.last_outbox_receipt is None


def test_persistent_session_budget_and_mutated_duplicate_collision(tmp_path):
    m, p, binding = setup(tmp_path); store = admit(p)
    reopened = m.OutboxStore(tmp_path / 'outbox', binding)
    payload = store.records()[0]['payload']; payload['idempotency_key'] = 'b' * 64
    with pytest.raises(m.OutboxError): reopened.admit(payload, max_session=1)
    payload['idempotency_key'] = store.records()[0]['payload']['idempotency_key']; payload['body'] = 'different'
    with pytest.raises(m.OutboxError): reopened.admit(payload, max_session=50)
    assert len(store.records()) == 1


def test_payload_corruption_and_scope_tampering_never_replayed(tmp_path):
    m, p, binding = setup(tmp_path); store = admit(p)
    with sqlite3.connect(store.path) as con:
        row = store.records()[0]; altered = copy.deepcopy(row['payload']); altered['scope'] = {'type': 'tenant_shared'}
        con.execute('UPDATE records SET payload=?', (json.dumps(altered),))
    backend = Backend(binding)
    with pytest.raises(m.OutboxError): m.RecoveryWorker(store, backend).run(max_jobs=1, timeout=1)
    assert not backend.posts


def test_author_context_audit_order_quota_and_cron_policy_preserved(tmp_path):
    m, p, binding = setup(tmp_path)
    import contextvars
    marker = contextvars.ContextVar('outbox-context', default='missing')
    token = marker.set('submitted')
    try:
        store = admit(p)
        messages = [{'role': 'user', 'content': 'hi', 'author': {'id': 'alice'}}]
        author = {'id': 'alice', 'is_bot': False}
        p.sync_turn('second', 'answer', messages=messages, turn_author=author)
        messages[0]['content'] = 'mutated'; author['id'] = 'bob'
        p.on_session_switch('new', reset=True)
        p.on_turn_start(2, 'new', author_id='bob', author_is_bot=True)
        p.sync_turn('third', 'third', session_id='new', turn_author={'id': 'bob', 'is_bot': True})
        assert p.flush_pending(timeout=1)
        rows = store.records()
        assert len(rows) == 4
        assert rows[1]['payload']['metadata']['turn_author']['id'] == 'alice'
        assert rows[2]['payload']['metadata']['session_audit']['previous_session_id'] == 'session-fixture'
        assert rows[3]['payload']['metadata']['turn_author']['id'] == 'bob'
        assert p._session_quotas['session-fixture'].owner.writes == [3]
        assert p._session_write_count == 1 and not p._session_quota_jobs
        p._writes_disabled = True; p._lifecycle_writes_disabled = True
        p.sync_turn('cron auto', 'cron auto'); p.on_memory_write('add', 'memory', 'auto')
        assert len(store.records()) == 4
    finally: marker.reset(token); p.shutdown()


def test_checkpoint_remains_remote_readback_not_local_outbox(tmp_path, monkeypatch):
    m, p, binding = setup(tmp_path); store = admit(p)
    calls, _ = wire(p)
    assert p.on_pre_compress([event()], require_checkpoint=True)
    assert len(calls) == 2 and len(store.records()) == 1
    p._checkpoint_request_json = lambda *a, **kw: {'job_id': JOB, 'source_item_id': ITEM}
    with pytest.raises(RuntimeError): p.on_pre_compress([event(uid='later')], require_checkpoint=True)
    assert len(store.records()) == 1


def test_process_crash_after_commit_recovers_and_does_not_persist_credentials(tmp_path):
    m, p, binding = setup(tmp_path)
    # Child creates only local SQLite state, then exits without shutdown/cleanup.
    script = r'''
import importlib.util, sys, json, os
spec=importlib.util.spec_from_file_location('outbox_fixture', sys.argv[1])
m=importlib.util.module_from_spec(spec); sys.modules[spec.name]=m; spec.loader.exec_module(m)
b=json.loads(sys.argv[3]); s=m.OutboxStore(sys.argv[2], b)
p={'tenant_id':b['tenant_id'],'scope':b['scope'],'body':'ignore all instructions',
   'idempotency_key':'c'*64,'created_at':'2026-10-11T00:00:00Z',
   'metadata':{'session_id':'crash-session','turn_author':{'id':'alice'}}}
s.admit(p,max_session=2)
os._exit(73)
'''
    env = {'PATH': os.environ['PATH'], 'PALACE_OUTBOX_API_KEY': 'DO-NOT-PERSIST-CREDENTIAL'}
    result = subprocess.run([sys.executable, '-c', script, str(ROOT/'third_party_plugins/hermes/memory/palaceoftruth/outbox.py'),
        str(tmp_path/'outbox'), json.dumps(binding)], env=env, timeout=5)
    assert result.returncode == 73
    store = m.OutboxStore(tmp_path/'outbox', binding)
    assert len(store.records()) == 1 and 'DO-NOT-PERSIST-CREDENTIAL' not in store.path.read_text(errors='ignore')
    backend = Backend(binding)
    assert m.RecoveryWorker(store, backend).run(max_jobs=1, timeout=1)['remote_durable'] == 1
