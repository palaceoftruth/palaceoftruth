"""Session-keyed quota regression, including actual official resume boundary."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest
from tests.test_hermes_supported_lifecycle import provider


def test_official_resume_quota_fixed_host_drain_still_red(tmp_path):
    host = os.environ.get('HERMES_CONTRACT_ROOT')
    if not host:
        pytest.skip('Requires explicit official host checkout')
    script = Path(__file__).resolve().parents[2] / 'scripts/check_hermes_public_lifecycle.py'
    env = {k:v for k,v in os.environ.items() if not k.startswith(('PALACE','SECONDBRAIN','HERMES_'))}
    env.update(HOME=str(tmp_path), HERMES_HOME=str(tmp_path))
    receipt = tmp_path / 'receipt.json'
    result = subprocess.run([sys.executable, str(script), '--host', host, '--receipt', str(receipt)], env=env, capture_output=True, text=True, timeout=20)
    assert receipt.exists(), result.stdout + result.stderr
    data = json.loads(receipt.read_text())
    assert data['host_submission_snapshot'] == 'passed'
    assert data['stale_host_submission_quota_isolation'] == 'passed'
    assert data['new_session_quota_before_old_dispatch'] == data['new_session_quota_after_old_dispatch'] == 0
    assert data['host_global_drain'] == 'unsupported'
    assert result.returncode == 2, 'Host gate must remain red'


def test_shared_session_cap_survives_resume_and_rewind(provider, monkeypatch):
    provider._max_writes_per_session = 1
    writes = []
    monkeypatch.setattr(provider, '_request_json', lambda *args: writes.append(args[-1]) or {'ok': True})
    provider.on_memory_write('add', 'memory', 'first')
    assert provider.flush_pending()
    provider.on_session_switch('new')
    provider.on_session_switch('old', rewound=True)
    provider.on_memory_write('add', 'memory', 'second')
    assert provider.flush_pending()
    facts = [payload for payload in writes if 'session_audit' not in payload['metadata']]
    assert len(facts) == 1
    assert provider._session_write_count == 1
    assert provider._session_quotas['new'].owner.writes == [1]  # Rewind audit owns its previous session.
    assert provider.shutdown_drain_state['failed_writes'] == 1


def test_retention_pins_active_owner_and_prunes_after_drain(provider, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def transport(*args):
        entered.set()
        assert release.wait(5)
        return {'ok': True}
    monkeypatch.setattr(provider, '_request_json', transport)
    try:
        provider.on_memory_write('add', 'memory', 'pinned')
        assert entered.wait(3)
        owner = provider._session_quotas['old'].owner
        for i in range(provider._session_quota_retention_limit + 20):
            provider.on_session_switch(f'session-{i}')
        assert provider._session_quotas['old'].owner is owner
        assert len(provider._session_quotas) <= provider._session_quota_retention_limit + 1
        release.set()
        assert provider.flush_pending()
        assert len(provider._session_quotas) <= provider._session_quota_retention_limit
        assert owner.writes == [1]
    finally:
        release.set()


def test_old_explicit_turn_does_not_rotate_new_turn_bucket(provider, monkeypatch):
    provider._max_writes_per_turn = 1
    monkeypatch.setattr(provider, '_request_json', lambda *args: {'ok': True})
    provider.on_session_switch('new')
    provider._reserve_write_quota(is_bulk=False)
    provider.sync_turn('delayed old', 'answer', session_id='old')
    assert provider.flush_pending()
    assert provider._turn_write_count == provider._session_write_count == 1
    provider.on_memory_write('add', 'memory', 'new over cap')
    assert provider.flush_pending()
    assert provider.shutdown_drain_state['failed_writes'] == 1


def test_explicit_mirror_session_uses_old_immutable_owner(provider, monkeypatch):
    writes = []
    monkeypatch.setattr(provider, '_request_json', lambda *args: writes.append(args[-1]) or {'ok': True})
    provider.on_session_switch('new')
    provider.on_memory_write('add', 'memory', 'old mirror', metadata={'session_id': 'old'})
    assert provider.flush_pending()
    assert provider._session_write_count == 0
    owner = provider._session_quotas['old'].owner
    assert owner.writes == [1]
    assert writes[0]['metadata']['session_id'] == 'old'
    with pytest.raises(AttributeError):
        owner.session_id = 'new'


def test_queued_concurrent_jobs_share_session_cap_across_switch(provider, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    writes = []
    provider._max_writes_per_turn = 20
    provider._max_writes_per_session = 3
    def transport(*args):
        entered.set()
        assert release.wait(5)
        writes.append(args[-1])
        return {'ok': True}
    monkeypatch.setattr(provider, '_request_json', transport)
    try:
        provider.on_memory_write('add', 'memory', 'first')
        assert entered.wait(3)
        threads = [threading.Thread(target=lambda i=i: provider.on_memory_write('add', 'memory', f'queued-{i}')) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(2)
            assert not t.is_alive()
        provider.on_session_switch('new')
        provider.on_session_switch('old', rewound=True)
        release.set()
        assert provider.flush_pending()
        facts = [payload for payload in writes if 'session_audit' not in payload['metadata']]
        assert len(facts) == provider._session_write_count == 3
        assert provider._session_quotas['new'].owner.writes == [1]
        assert provider.shutdown_drain_state['failed_writes'] == 3
    finally:
        release.set()


def test_all_active_overflow_is_retained_until_completion(provider, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    provider._session_quota_retention_limit = 3
    def transport(*args):
        entered.set()
        assert release.wait(5)
        return {'ok': True}
    monkeypatch.setattr(provider, '_request_json', transport)
    try:
        provider.on_memory_write('add', 'memory', 'block')
        assert entered.wait(3)
        for i in range(6):
            provider.sync_turn(f'pending-{i}', 'answer', session_id=f'pending-{i}')
        owners = [provider._session_quotas[f'pending-{i}'].owner for i in range(6)]
        provider.on_session_switch('current')
        assert len(provider._session_quotas) > 3
        assert all(provider._session_quota_jobs[f'pending-{i}'] == 1 for i in range(6))
        release.set()
        assert provider.flush_pending()
        assert len(provider._session_quotas) <= 3
        assert not provider._session_quota_jobs
        assert all(owner.writes == [1] for owner in owners)
    finally:
        release.set()
