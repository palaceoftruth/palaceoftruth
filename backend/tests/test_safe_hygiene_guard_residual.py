"""Synthetic Git CLI and exception safety; no production initialization."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest

SCRIPT = Path(__file__).parent.parent / 'scripts/safe_hygiene_guard.py'
spec = importlib.util.spec_from_file_location('residual_guard', SCRIPT)
assert spec is not None and spec.loader is not None
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)
MARK = 'ZXQ9_SYNTHETIC_ONLY_LONG_MARKER_8VJK'


def safe_output(text):
    for forbidden in (MARK, MARK[:4], MARK[-4:], hashlib.sha256(MARK.encode()).hexdigest()):
        assert forbidden not in text
    assert 'Traceback' not in text


def test_actual_git_cli_enumerates_publication_paths(tmp_path):
    env = {'PATH': os.defpath, 'HOME': str(tmp_path), 'GIT_CONFIG_NOSYSTEM': '1'}
    def git(*args):
        return subprocess.run(['git', *args], cwd=tmp_path, env=env, capture_output=True, check=True)
    git('init')
    paths = ['.env', '.env.example', 'build/report.json', 'receipts/test_config.xml']
    contents = ['api_key='+MARK, 'TOKEN='+MARK, json.dumps({'client_secret': MARK}),
                '<failure>E assert Config(api_key="'+MARK+'") == fixture</failure>']
    for path, content in zip(paths, contents):
        p = tmp_path / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    git('add', '-f', '.')
    git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'fixture')
    p = subprocess.run([sys.executable, str(SCRIPT), 'HEAD'], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert p.returncode == 0 and p.stderr == ''
    safe_output(p.stdout+p.stderr)
    payload = json.loads(p.stdout)
    assert payload['scanned'] == 4
    assert {r['path'] for r in payload['findings']} == set(paths)
    for row in payload['findings']:
        assert set(row) == {'path', 'line', 'rule'}


@pytest.mark.parametrize('stage', ['tree', 'blob'])
@pytest.mark.parametrize('kind', ['process', 'os', 'unicode'])
def test_exception_output_is_static(stage, kind, monkeypatch, capsys):
    calls = []
    def run(*args, **kwargs):
        calls.append(args)
        if stage == 'blob' and len(calls) == 1:
            return subprocess.CompletedProcess([], 0, 'build/report.json\n', '')
        if kind == 'process':
            raise subprocess.CalledProcessError(1, [MARK], output=MARK, stderr=MARK)
        if kind == 'os':
            raise OSError(MARK)
        raise UnicodeError(MARK)
    monkeypatch.setattr(g.subprocess, 'run', run)
    assert g.main(['guard', MARK]) == 2
    out = capsys.readouterr()
    assert out.err == ''
    safe_output(out.out+out.err)
    assert json.loads(out.out) == {'error': stage+'-read-failed'}


@pytest.mark.parametrize('blob', [
    '{"api_key":"'+MARK+'", "client_secret":"'+MARK+'"}',
    '<failure>E assert Config(api_key=&quot;'+MARK+'&quot;) == fixture</failure>',
    'E assert Config(api_key="'+MARK+'", token="'+MARK+'") == fixture',
])
def test_formats_strict_three_field_output(blob):
    rows = g.scan_blob(Path('receipts/assertion.xml'), blob)
    assert rows
    safe_output(json.dumps(rows))
    assert all(set(row) == {'path', 'line', 'rule'} for row in rows)


def test_invalid_ref_cli_is_static(tmp_path):
    p = subprocess.run([sys.executable, str(SCRIPT), MARK], cwd=tmp_path,
                       env={'PATH': os.defpath, 'HOME': str(tmp_path)}, capture_output=True, text=True)
    assert p.returncode == 2 and p.stderr == ''
    safe_output(p.stdout+p.stderr)
    assert json.loads(p.stdout) == {'error': 'tree-read-failed'}
