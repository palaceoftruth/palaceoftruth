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
manager.shutdown_all()
assert len(writes) == 1, writes
assert writes[0][1]['metadata']['turn_author'] == author
assert writes[0][1]['metadata']['messages']['authors'] == [author]
manager = MemoryManager()
manager.add_provider(p)
manager.on_memory_write('remove', 'memory', '', metadata={'write_origin': 'assistant_tool',
    'previous_content': 'old fact', 'old_text': 'old', 'tool_call_id': 'call'})
manager.shutdown_all()
assert writes[-1][0] == '/api/v1/memory/entries'
assert writes[-1][1]['metadata']['write_provenance']['write_origin'] == 'assistant_tool'
assert writes[-1][1]['metadata']['write_provenance']['previous_content_sha256']
assert writes[-1][1]['relationship_policy'] == 'skip'
manager.on_session_switch('shared', rewound=True)
manager.shutdown_all()
assert writes[-1][1]['metadata']['session_audit']['event'] == 'rewind'
print('real upstream MemoryManager author/messages/remove metadata: passed')
'''
    env = {k: v for k, v in os.environ.items() if not k.startswith(('PALACEOFTRUTH_', 'SECONDBRAIN_'))}
    env['HERMES_HOME'] = str(tmp_path)
    result = subprocess.run([sys.executable, '-c', code, HOST,
        str(ROOT / 'third_party_plugins/hermes/memory/palaceoftruth/__init__.py'),
        'true' if is_bot else 'false'], env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
