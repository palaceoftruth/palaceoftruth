"""Real host/provider admission regressions; opt in with HERMES_COMPAT_TEST_ROOT.

Accepted boundaries intentionally remain red until paired session/quota admission
is implemented. Do not xfail or weaken these assertions to approve a release.
Each case runs in a separate process, without credentials or network access.
"""
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROBE = r'''
import importlib.util
import json
from pathlib import Path
import socket
import sys
import threading

host, plugin, mode = sys.argv[1:]
def deny(*args, **kwargs):
    raise AssertionError("Network is forbidden in paired admission tests")
socket.socket.connect = deny
socket.create_connection = deny
sys.path.insert(0, host)
from agent.memory_manager import MemoryManager
path = Path(plugin)
spec = importlib.util.spec_from_file_location(
    'palace_paired_admission', path, submodule_search_locations=[str(path.parent)])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
p = module.PalaceOfTruthMemoryProvider()
p._session_id = 'old'
p._scope_type = 'session'
p._has_api_auth = lambda: True
p._resolve_tenant_id = lambda: 'offline-tenant'
p._write_quotas_enabled = True
p._max_writes_per_turn = 20
p._max_writes_per_session = 20
observed, quotas, extractions, execution_order = [], [], [], []
p._build_memory_write_payload = lambda **kwargs: kwargs
p._build_entry_payload = lambda user, assistant, session, **kwargs: {
    'content': 'sync', 'snapshot': kwargs['snapshot']}
if mode == 'sync-caps':
    # The ending turn has one mirror plus its capture. The next turn must
    # have its own allowance, not inherit this full two-write bucket.
    p._max_writes_per_turn = 2

def record(route, payload):
    quota = p._active_write_quota()
    p._reserve_write_quota(is_bulk=False)
    quotas.append(quota)
    observed.append((payload['content'], payload['snapshot']['session_id']))
    execution_order.append('mirror')

p._post_memory_entries = record
def extract(messages):
    extractions.append(p._session_id)
    execution_order.append('extract')
p.on_session_end = extract
m = MemoryManager()
m._providers = [p]
entered, release = threading.Event(), threading.Event()
def block():
    entered.set()
    assert release.wait(5)
assert m._submit_background(block)
orphans = []
try:
    assert entered.wait(2)
    if mode == 'concurrent':
        prepared, resume_prepare = threading.Event(), threading.Event()
        boundary_started, boundary_finished = threading.Event(), threading.Event()
        prepare = p.prepare_memory_write
        results = []
        def paused_prepare(*args, **kwargs):
            completion = prepare(*args, **kwargs)
            prepared.set()
            assert resume_prepare.wait(3)
            return completion
        p.prepare_memory_write = paused_prepare
        mirror = threading.Thread(target=lambda: m.on_memory_write('add', 'memory', 'before'))
        def boundary():
            boundary_started.set()
            results.append(m.commit_session_boundary_async([], new_session_id='new'))
            boundary_finished.set()
        switch = threading.Thread(target=boundary)
        mirror.start()
        try:
            assert prepared.wait(2)
            switch.start()
            assert boundary_started.wait(2)
            # Atomic admission blocks the boundary until mirror enqueue.
            # A broken host can finish here; always release and join both.
            boundary_finished.wait(0.2)
        finally:
            resume_prepare.set()
            mirror.join(3)
            if switch.ident is not None:
                switch.join(3)
        assert not mirror.is_alive() and not switch.is_alive()
        assert results == [True]
    else:
        m.on_memory_write('add', 'memory', 'before')
    if mode in ('reject', 'orphan'):
        submit = m._sync_executor.submit
        def reject(fn):
            if mode == 'orphan':
                orphans.append(submit(fn))
            raise RuntimeError('offline rejection')
        m._sync_executor.submit = reject
        try:
            assert m.commit_session_boundary_async([], new_session_id='rejected') is False
        finally:
            m._sync_executor.submit = submit
        m.on_memory_write('add', 'memory', 'after rejection')
    elif mode in ('sync-quotas', 'sync-caps', 'sync-control'):
        m.sync_all('ending old turn', 'reply', session_id='old')
        if mode == 'sync-control':
            # Positive control: once sync executes, the same real quota code
            # correctly gives the next mirror a new turn bucket.
            release.set()
            assert m.flush_pending(timeout=3)
        m.on_memory_write('add', 'memory', 'next turn')
    elif mode != 'concurrent':
        assert m.commit_session_boundary_async([], new_session_id='new') is True
        m.on_memory_write('add', 'memory', 'after new')
        assert m.commit_session_boundary_async([], new_session_id='newer') is True
        m.on_memory_write('add', 'memory', 'after newer')
    assert p._session_id == 'old'
    assert extractions == []
finally:
    release.set()
    drained = m.flush_pending(timeout=3)
    for future in orphans:
        future.result(timeout=2)
    m.shutdown_all()

result = dict(drained=drained, writes=observed, extractions=extractions,
              execution_order=execution_order,
              distinct_buckets=len({id(q.session_writes) for q in quotas}),
              distinct_turn_buckets=len({id(q) for q in quotas}),
              turn_counts=[q.turn_writes for q in quotas],
              session_counts=[q.session_writes[0] for q in quotas])
print(json.dumps(result, sort_keys=True))
assert drained
if mode == 'concurrent':
    assert observed == [('before', 'old')]
    assert extractions == ['old']
    assert execution_order == ['mirror', 'extract'], 'Boundary overtook prepared mirror'
elif mode in ('reject', 'orphan'):
    assert extractions == []
    assert observed == [('before', 'old'), ('after rejection', 'old')]
    assert result['distinct_buckets'] == 1
    assert result['session_counts'] == [2, 2]
elif mode in ('sync-quotas', 'sync-caps', 'sync-control'):
    assert extractions == []
    assert observed == [('before', 'old'), ('sync', 'old'), ('next turn', 'old')], (
        'Next-turn mirror was lost against the ending turn quota')
    assert result['distinct_buckets'] == 1, 'Turn rotation must retain session total'
    assert result['session_counts'] == [3, 3, 3]
    assert result['distinct_turn_buckets'] == 2, 'Queued sync did not publish next-turn quota'
    assert quotas[0] is quotas[1] and quotas[1] is not quotas[2]
    assert result['turn_counts'] == [2, 2, 1]
else:
    assert extractions == ['old', 'new']
    assert len(quotas) == 3
    if mode == 'sessions':
        assert observed == [('before', 'old'), ('after new', 'new'), ('after newer', 'newer')]
    else:
        assert result['distinct_buckets'] == 3, 'Accepted resets reused old-session quota bucket'
        assert result['session_counts'] == [1, 1, 1]
'''


@pytest.mark.parametrize('mode', [
    'sessions', 'quotas', 'reject', 'orphan', 'concurrent',
    'sync-quotas', 'sync-caps', 'sync-control',
])
def test_real_paired_boundary_admission(tmp_path, mode):
    host = os.environ.get('HERMES_COMPAT_TEST_ROOT')
    if not host:
        pytest.skip('Set HERMES_COMPAT_TEST_ROOT to a real, trusted Hermes checkout')
    assert (Path(host) / 'agent/memory_manager.py').is_file()
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('PALACE', 'HERMES_'))
           and not any(word in key for word in ('TOKEN', 'SECRET', 'API_KEY', 'PASSWORD'))}
    env.update(HERMES_HOME=str(tmp_path), PYTHONPATH=host)
    plugin = ROOT / 'third_party_plugins/hermes/memory/palaceoftruth/__init__.py'
    result = subprocess.run([sys.executable, '-c', PROBE, host, str(plugin), mode],
                            env=env, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, (result.stdout, result.stderr)
