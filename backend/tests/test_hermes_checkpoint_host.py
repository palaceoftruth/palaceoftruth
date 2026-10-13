"""Explicit upstream-host opt-in; subprocess prevents stub-module contamination."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(not os.environ.get("SAR1406_HOST_SOURCE"), reason="requires an explicit read-only upstream host checkout")
def test_real_host_required_gate_withholds_unproven_v2(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    host = Path(os.environ["SAR1406_HOST_SOURCE"])
    script = '''
import shutil
from pathlib import Path
import sys
from agent.memory_manager import MemoryManager
import os
shutil.copytree(Path(sys.argv[1]).parent, Path(os.environ['HERMES_HOME']) / 'plugins' / 'palaceoftruth')
from plugins.memory import load_memory_provider
p = load_memory_provider('palaceoftruth', register_skills=False)
assert p is not None and hasattr(p, '_write_queue')
assert type(p).__module__.startswith('_hermes_user_memory.')
print('normal supported plugin loader:', type(p).__module__)
manager = MemoryManager()
manager.add_provider(p)
assert not manager.supports_pre_compress_checkpoint()
assert manager.on_pre_compress([{"role": "user", "content": "direct"}]) == ""
try:
    manager.on_pre_compress([{"role": "user", "content": "direct"}], require_checkpoint=True)
except RuntimeError as exc:
    assert "No active memory provider completed" in str(exc)
else:
    raise AssertionError("unproven candidate must not pass required host gate")
print("official host: legacy optional preserved; unadvertised candidate blocks required compression")
import hashlib
p._config["precompression_checkpoint_enabled"] = True
p._session_id = "fixture"
p._tenant_id = "fixture-tenant"
p._scope_type = "session"
p._api_key = "fixture-only"
p._base_url = "https://fixture.invalid"
last = {}
def request(method, path, payload=None, **kwargs):
    if method == "POST":
        last.update(payload)
        return {"job_id": "00000000-0000-4000-8000-000000000001", "source_item_id": "00000000-0000-4000-8000-000000000002"}
    return {"durable": True, "job_id": "00000000-0000-4000-8000-000000000001", "source_item_id": "00000000-0000-4000-8000-000000000002", "tenant_id": "fixture-tenant", "scope": last["scope"], "idempotency_key": last["idempotency_key"], "body_sha256": hashlib.sha256(last["body"].encode()).hexdigest()}
p._checkpoint_request_json = request
assert manager.supports_pre_compress_checkpoint()
from agent.message_metadata import stamp_message_uid, stamp_message_timestamp
from agent.conversation_compression import _direct_messages_for_pre_compress_memory
import json
source = {"role": "user", "content": "direct evidence"}
# Source event metadata precedes checkpoint calls; retry never stamps anything.
stamp_message_timestamp(source, timestamp=1791633600.0)
stamp_message_uid(source)
original = [source, {"role": "tool", "content": "raw tool sentinel"}]
normalized = _direct_messages_for_pre_compress_memory(original)
assert normalized[0]["message_uid"] == source["message_uid"]
assert normalized[0]["timestamp"] == source["timestamp"]
result = manager.on_pre_compress(original, evidence_messages=normalized, require_checkpoint=True)
assert "durable" in result and "raw tool sentinel" not in last["body"]
first_body, first_key = last["body"], last["idempotency_key"]
restored = json.loads(json.dumps(original))
assert manager.on_pre_compress(restored, evidence_messages=_direct_messages_for_pre_compress_memory(restored), require_checkpoint=True)
assert (last["body"], last["idempotency_key"]) == (first_body, first_key)
p._checkpoint_request_json = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline"))
try:
    manager.on_pre_compress([], evidence_messages=normalized, require_checkpoint=True)
except RuntimeError:
    pass
else:
    raise AssertionError("host must propagate required provider failure")
assert manager.on_pre_compress([], evidence_messages=normalized) == ""
print("official host: opted-in normalized evidence, synchronous receipt success, required failure and optional best-effort proven")
'''
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "HERMES_HOME": str(tmp_path), "PYTHONPATH": str(host)}
    result = subprocess.run([sys.executable, "-c", script, str(repo / "third_party_plugins/hermes/memory/palaceoftruth/__init__.py")], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "unadvertised candidate blocks required compression" in result.stdout


@pytest.mark.skipif(not os.environ.get('SAR1406_HOST_SOURCE'), reason='requires official host source')
def test_real_normal_loader_native_acceptance(tmp_path):
    import shutil
    repo = Path(__file__).resolve().parents[2]
    host = Path(os.environ['SAR1406_HOST_SOURCE'])
    shutil.copytree(repo / 'third_party_plugins/hermes/memory/palaceoftruth',
                    tmp_path / 'plugins/palaceoftruth')
    env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(tmp_path),
           'HERMES_HOME': str(tmp_path), 'TMPDIR': os.environ.get('TMPDIR', str(tmp_path)),
           'PYTHONPATH': str(host) + os.pathsep + str(repo / 'backend'),
           'SAR1406_NORMAL_LOADER': '1'}
    files = ['native_export', 'independent_acceptance', 'repair', 'composed', 'session_quota']
    result = subprocess.run([sys.executable, '-m', 'pytest',
        *[str(repo / f'backend/tests/test_hermes_checkpoint_{name}.py') for name in files],
        '-q', '-o', 'addopts=', '--junitxml=' + str(tmp_path / 'normal-loader.xml')],
        env=env, cwd=tmp_path, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
