"""Required backend-fast entrypoint: pinned upstream, packaged normal-loader recovery.

No workflow change, production initialization, runtime dependency install or skip.
Network/source/dependency failures fail this required test, not fall back to stubs.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import xml.etree.ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOSTS = ('1f2b9e08644c6de4a5b5c50d0e4676a19c5577c5', 'c57331677d7fc46298a7159cf3a650853f0966c5')


@pytest.mark.parametrize('revision', HOSTS)
def test_packaged_normal_loader_recovery_required(tmp_path, revision):
    host = tmp_path / 'official-host'
    subprocess.run(['git', 'init', '-q', str(host)], check=True, timeout=10)
    subprocess.run(['git', '-C', str(host), 'fetch', '--depth=1',
                    'https://github.com/NousResearch/hermes-agent.git', revision], check=True, capture_output=True, timeout=90)
    subprocess.run(['git', '-C', str(host), 'checkout', '-q', '--detach', 'FETCH_HEAD'], check=True, timeout=10)
    assert subprocess.check_output(['git', '-C', str(host), 'rev-parse', 'HEAD'], text=True).strip() == revision
    spec = importlib.util.spec_from_file_location('recovery_packager', ROOT / 'scripts/package_hermes_recovery.py')
    assert spec is not None and spec.loader is not None
    packager = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(packager)
    first, second = tmp_path / 'first.tar', tmp_path / 'second.tar'
    inventory = packager.build(first)
    assert packager.build(second) == inventory and first.read_bytes() == second.read_bytes()
    assert inventory['version'] == '1.0.41'
    home = tmp_path / 'home'
    plugins = home / 'plugins'
    plugins.mkdir(parents=True)
    with tarfile.open(first) as archive:
        archive.extractall(tmp_path / 'package', filter='data')
    import shutil
    shutil.copytree(tmp_path / 'package/palaceoftruth', plugins / 'palaceoftruth')
    for name, digest in inventory['files'].items():
        assert hashlib.sha256((tmp_path / 'package' / name).read_bytes()).hexdigest() == digest
    env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(home), 'HERMES_HOME': str(home),
           'TMPDIR': str(tmp_path), 'PYTHONPATH': str(ROOT / 'backend') + os.pathsep + str(host),
           'SAR1406_NORMAL_LOADER': '1', 'SAR1406_HOST_SOURCE': str(host),
           'HERMES_CONTRACT_ROOT': str(host), 'HERMES_PROVENANCE_HOST_ROOT': str(host)}
    receipt = tmp_path / 'recovery.xml'
    files = sorted((ROOT / 'backend/tests').glob('test_hermes_outbox*.py'))
    result = subprocess.run([sys.executable, '-m', 'pytest', *map(str, files), '-q', '-o', 'addopts=',
                             '--junitxml=' + str(receipt)], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    suites = list(ET.parse(receipt).getroot().iter('testsuite'))
    counts = {key: sum(int(s.get(key, 0)) for s in suites) for key in ('tests', 'failures', 'errors', 'skipped')}
    assert counts == {'tests': 70, 'failures': 0, 'errors': 0, 'skipped': 0}, counts
    env['SAR1402_EXTRACTED_PACKAGE'] = str(tmp_path / 'package')
    artifact_receipt = tmp_path / 'artifact-cli.xml'
    artifact_result = subprocess.run([sys.executable, '-m', 'pytest',
        str(ROOT / 'backend/tests/artifact_cli_acceptance.py'), '-q', '-o', 'addopts=',
        '--junitxml=' + str(artifact_receipt)], env=env, cwd=tmp_path,
        capture_output=True, text=True, timeout=90)
    assert artifact_result.returncode == 0, artifact_result.stdout + artifact_result.stderr
    artifact_suites = list(ET.parse(artifact_receipt).getroot().iter('testsuite'))
    artifact_counts = {key: sum(int(s.get(key, 0)) for s in artifact_suites) for key in ('tests', 'failures', 'errors', 'skipped')}
    assert artifact_counts == {'tests': 7, 'failures': 0, 'errors': 0, 'skipped': 0}, artifact_counts
    assert not subprocess.check_output(['git', '-C', str(host), 'status', '--porcelain'], text=True).strip()
    # Visible under the unchanged hosted pytest -q lane, no secret-bearing output.
    import warnings
    warnings.warn('REVISED_RECOVERY_RECEIPT ' + json.dumps({'host': revision, 'package': inventory['archive_sha256'], **counts}, sort_keys=True), UserWarning)
    warnings.warn('ARTIFACT_ONLY_CLI_RECEIPT ' + json.dumps({'host': revision, 'package': inventory['archive_sha256'], **artifact_counts}, sort_keys=True), UserWarning)
