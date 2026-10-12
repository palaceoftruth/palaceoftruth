"""Additional mandatory artifact-only entrypoints; never part of legacy exact70."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from tests.test_hermes_outbox_auth_routes import actual_backend, prepared, OAUTH_SECRET, CLIENT_KEY


@pytest.mark.parametrize('entrypoint', ['standalone', 'plugin'])
@pytest.mark.parametrize('fault', [None, 'expired', 'grant'])
def test_extracted_cli_actual_oauth_routes(tmp_path, monkeypatch, entrypoint, fault):
    artifact = Path(os.environ['SAR1402_EXTRACTED_PACKAGE'])
    host = Path(os.environ['SAR1406_HOST_SOURCE'])
    assert (artifact / 'palaceoftruth/outbox.py').is_file()
    assert not (artifact / 'third_party_plugins').exists()
    with actual_backend(tmp_path, monkeypatch, fault=fault) as (origin, cert, authdb, domain, calls):
        m, provider, binding, store = prepared(tmp_path, origin, 'mcp_oauth')
        shutil.copytree(artifact / 'palaceoftruth', tmp_path / 'plugins/palaceoftruth')
        (tmp_path / 'config.yaml').write_text('memory:\n  provider: palaceoftruth\n')
        binding_file = tmp_path / 'binding.json'
        binding_file.write_text(json.dumps(binding))
        args = ['--directory', str(store.directory), '--binding-file', str(binding_file),
                '--ca-file', str(cert), '--max-jobs', '2', '--timeout', '5']
        env = {'PATH': os.environ['PATH'], 'HOME': str(tmp_path), 'HERMES_HOME': str(tmp_path),
               'PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET': OAUTH_SECRET,
               'PALACEOFTRUTH_MCP_CLIENT_KEY': CLIENT_KEY}
        if entrypoint == 'standalone':
            command = [sys.executable, '-I', str(artifact / 'scripts/recover_hermes_outbox.py'), *args]
        else:
            # Only official host source on child import path; no repository backend/plugin paths.
            env['PYTHONPATH'] = str(host)
            script = '''
import argparse,sys
from plugins.memory import discover_plugin_cli_commands
commands=discover_plugin_cli_commands()
assert len(commands)==1 and commands[0]['name']=='palaceoftruth'
parser=argparse.ArgumentParser();commands[0]['setup_fn'](parser)
args=parser.parse_args(sys.argv[1:]);raise SystemExit(args.func(args))
'''
            command = [sys.executable, '-c', script, 'recover-outbox', *args]
        result = subprocess.run(command, env=env, cwd=tmp_path, capture_output=True, text=True, timeout=10)
        assert OAUTH_SECRET not in result.stdout + result.stderr
        assert result.returncode == (2 if fault else 0), result.stdout + result.stderr
        if fault:
            assert all(r['state'] == 'pending' for r in store.records())
            assert not domain.committed
            assert not [c for c in calls if c[0] == 'POST' and c[1].endswith('/entries')]
        else:
            assert json.loads(result.stdout)['remote_durable'] == 2
            records = store.records()
            assert all(r['state'] == 'delivered' for r in records)
            from collections import Counter
            assert Counter(model.__name__ for model, key in domain.committed) == {'Job': 2, 'Item': 2, 'MemoryEntry': 2}
            assert len([c for c in calls if c[0] == 'POST' and c[1].endswith('/entries')]) == 2
            assert len([c for c in calls if c[0] == 'GET' and 'checkpoint-receipt' in c[1]]) == 2
        provider.shutdown()


def test_incomplete_extracted_modules_fail_closed(tmp_path):
    artifact = Path(os.environ['SAR1402_EXTRACTED_PACKAGE'])
    shutil.copytree(artifact, tmp_path / 'broken')
    (tmp_path / 'broken/palaceoftruth/outbox.py').unlink()
    result = subprocess.run([sys.executable, '-I', str(tmp_path / 'broken/scripts/recover_hermes_outbox.py'),
        '--directory', str(tmp_path / 'ledger'), '--binding-file', str(tmp_path / 'binding.json')],
        env={'PATH': os.environ['PATH']}, cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert json.loads(result.stdout)['error'] == 'recovery_not_proven'
    assert not result.stderr and not (tmp_path / 'ledger').exists()
