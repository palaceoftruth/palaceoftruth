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
observed, quotas, extractions = [], [], []
p._build_memory_write_payload = lambda **kwargs: kwargs

def record(route, payload):
    quota = p._active_write_quota()
    p._reserve_write_quota(is_bulk=False)
    quotas.append(quota)
    observed.append((payload['content'], payload['snapshot']['session_id']))

p._post_memory_entries = record
p.on_session_end = lambda messages: extractions.append(p._session_id)
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
    else:
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
              distinct_buckets=len({id(q.session_writes) for q in quotas}),
              session_counts=[q.session_writes[0] for q in quotas])
print(json.dumps(result, sort_keys=True))
assert drained
if mode in ('reject', 'orphan'):
    assert extractions == []
    assert observed == [('before', 'old'), ('after rejection', 'old')]
    assert result['distinct_buckets'] == 1
    assert result['session_counts'] == [2, 2]
else:
    assert extractions == ['old', 'new']
    assert len(quotas) == 3
    if mode == 'sessions':
        assert observed == [('before', 'old'), ('after new', 'new'), ('after newer', 'newer')]
    else:
        assert result['distinct_buckets'] == 3, 'Accepted resets reused old-session quota bucket'
        assert result['session_counts'] == [1, 1, 1]
'''


@pytest.mark.parametrize('mode', ['sessions', 'quotas', 'reject', 'orphan'])
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
