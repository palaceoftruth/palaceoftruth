"""Opt-in real upstream ABI/MemoryManager test; no Hermes core substitutions."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOST = os.environ.get('HERMES_PROVENANCE_HOST_ROOT')


@pytest.mark.skipif(not HOST, reason='set HERMES_PROVENANCE_HOST_ROOT to a pinned upstream checkout')
@pytest.mark.parametrize('is_bot', [False, True])
def test_real_upstream_manager_preserves_author_messages_and_write_metadata(is_bot, tmp_path):
    assert HOST is not None
    code = r'''
import importlib.util, inspect, json, sys
sys.path.insert(0, sys.argv[1])
from agent.memory_provider import MemoryProvider
from agent.memory_manager import MemoryManager
spec = importlib.util.spec_from_file_location('provenance_candidate', sys.argv[2])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
p = m.PalaceOfTruthMemoryProvider()
assert isinstance(p, MemoryProvider)
assert p.pre_compress_checkpoint_api_version == 1
for key in ('messages', 'turn_author'):
    host_parameter = inspect.signature(MemoryProvider.sync_turn).parameters[key]
    candidate_parameter = inspect.signature(p.sync_turn).parameters[key]
    assert host_parameter.default is None
    assert candidate_parameter.default is None
    assert candidate_parameter.kind == host_parameter.kind
assert inspect.signature(MemoryProvider.on_memory_write).parameters['metadata'].default is None
assert inspect.signature(p.on_memory_write).parameters['metadata'].default is None
p._base_url = 'https://fixture.invalid'
p._api_key = 'fixture-only'
p._session_id = 'shared'
p._agent_identity = p._scope_key = 'barbara'
p._resolve_tenant_id = lambda: 'fixture-tenant'
# Network writes are denied. Capture the provider submission, not a live server.
writes = []
p._post_memory_entries = lambda path, payload: writes.append((path, payload))
p._request_json = lambda *a, **kw: (_ for _ in ()).throw(AssertionError('network forbidden'))
author = {'id': 'actor', 'name': 'Actor', 'is_bot': json.loads(sys.argv[3])}
manager = MemoryManager()
manager.add_provider(p)
manager.on_turn_start(1, 'hello', author_id=author['id'], author_name=author['name'], author_is_bot=author['is_bot'])
manager.sync_all('hello', 'answer', session_id='shared', turn_author=author,
                 messages=[{'role': 'user', 'content': 'hello', 'author': author}])
assert manager.flush_pending(timeout=3)
assert p.flush_pending(timeout=3)
assert len(writes) == 1, writes
assert writes[0][1]['metadata']['turn_author'] == author
assert writes[0][1]['metadata']['messages']['authors'] == [author]
manager.on_memory_write('remove', 'memory', '', metadata={'write_origin': 'assistant_tool',
    'previous_content': 'old fact', 'old_text': 'old', 'tool_call_id': 'call'})
assert p.flush_pending(timeout=3)
assert writes[-1][0] == '/api/v1/memory/entries'
assert writes[-1][1]['metadata']['write_provenance']['write_origin'] == 'assistant_tool'
assert writes[-1][1]['metadata']['write_provenance']['previous_content_sha256']
assert writes[-1][1]['relationship_policy'] == 'skip'
from agent.background_review import build_memory_write_metadata
from types import SimpleNamespace
agent = SimpleNamespace(session_id='old-child', _parent_session_id='shared', platform='matrix')
metadata = build_memory_write_metadata(agent, write_origin='background_review', execution_context='background_review', task_id='task')
assert 'turn_author' not in metadata
manager.on_turn_start(2, 'later bot', author_id='later', author_is_bot=True)
manager.on_memory_write('remove', 'memory', '', metadata=metadata)
assert p.flush_pending(timeout=3)
assert writes[-1][1]['metadata']['write_provenance']['turn_author'] == {}
assert writes[-1][1]['metadata']['session_id'] == 'old-child'
# Real official host gap remains visible: provider audit accounting is correct,
# but the official manager's own executor flush is NOT a provider-wide barrier.
import threading
entered, release = threading.Event(), threading.Event()
def blocked_post(path, payload):
    entered.set()
    assert release.wait(3)
    writes.append((path, payload))
p._post_memory_entries = blocked_post
manager.on_session_switch('shared', rewound=True)
assert entered.wait(2)
assert manager.flush_pending(timeout=.05) is True
assert p.flush_pending(timeout=.01) is False
assert p.shutdown_drain_state['status'] == 'pending'
release.set()
assert p.flush_pending(timeout=3)
manager.shutdown_all()
assert writes[-1][1]['metadata']['session_audit']['event'] == 'rewind'
assert p.shutdown_drain_state['status'] == 'drained'
print('official host-global audit barrier unsupported; strict release gate remains mandatory')
print('real upstream MemoryManager author/messages/remove metadata: passed')
'''
    env = {k: v for k, v in os.environ.items() if not k.startswith(('PALACEOFTRUTH_', 'SECONDBRAIN_'))}
    env['HERMES_HOME'] = str(tmp_path)
    result = subprocess.run([sys.executable, '-c', code, HOST,
        str(ROOT / 'third_party_plugins/hermes/memory/palaceoftruth/__init__.py'),
        'true' if is_bot else 'false'], env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
