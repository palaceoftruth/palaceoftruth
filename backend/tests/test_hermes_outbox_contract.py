"""Additional negative contract probes; fixture state only."""
import copy
import json
import os
import subprocess
import sys
import time

import pytest
from tests.test_hermes_outbox import setup, admit, Backend, ROOT


def test_recovery_stops_at_first_unproven_predecessor(tmp_path):
    m,p,binding=setup(tmp_path);store=admit(p)
    p.sync_turn('second','second')
    assert p.flush_pending(timeout=1)
    backend=Backend(binding);backend.queue_only=True
    result=m.RecoveryWorker(store,backend).run(max_jobs=2,timeout=1)
    assert result['failed']==1 and result['remote_durable']==0
    assert len(backend.posts)==1 and all(r['state']=='pending' for r in store.records())


def test_row_cap_is_finite_and_done_rows_are_not_forgotten(tmp_path,monkeypatch):
    m,p,binding=setup(tmp_path);store=admit(p)
    monkeypatch.setattr(m,'MAX_ROWS',1)
    assert m.RecoveryWorker(store,Backend(binding)).run(max_jobs=1,timeout=1)['remote_durable']==1
    p.sync_turn('not silently evicted','second')
    assert p.flush_pending(timeout=1) and p.last_outbox_receipt is None
    assert p.shutdown_drain_state['failed_writes']==1
    assert len(store.records())==1 and store.records()[0]['state']=='delivered'


@pytest.mark.parametrize('origin',['http://fixture.invalid','https://user:password@fixture.invalid','https://fixture.invalid/path','https://fixture.invalid?token=bad'])
def test_unsafe_origin_is_not_accepted(tmp_path,origin):
    m,p,binding=setup(tmp_path);binding['origin']=origin
    with pytest.raises(m.OutboxError):m.OutboxStore(tmp_path/'outbox',binding)
    assert not (tmp_path/'outbox').exists()


def test_context_is_used_at_admission_not_serialized_as_executable_state(tmp_path):
    m,p,binding=setup(tmp_path)
    import contextvars
    var=contextvars.ContextVar('opaque-fixture',default=None);opaque=object();token=var.set(opaque)
    captured=[]
    original=p._build_entry_payload
    def build(*a,**kw):captured.append(var.get());return original(*a,**kw)
    # Own plugin builder observation only; no Hermes function is replaced.
    p._build_entry_payload=build
    try:store=admit(p)
    finally:var.reset(token)
    assert captured==[opaque]
    row=store.records()[0]
    assert row['payload']['metadata']['turn_author']['id']=='alice'
    assert 'opaque-fixture' not in store.path.read_text(errors='ignore')
    assert 'pickle' not in store.path.read_text(errors='ignore')


@pytest.mark.skipif(not os.environ.get('SAR1406_NORMAL_LOADER'),reason='requires official host')
def test_crash_while_host_submission_unobserved_produces_no_recoverable_record(tmp_path):
    setup(tmp_path)
    script=r'''
import os,sys,threading,json
from plugins.memory import load_memory_provider
from agent.memory_manager import MemoryManager
base=type(load_memory_provider('palaceoftruth',register_skills=False))
entered=threading.Event()
class Barrier(base):
 def queue_prefetch(self,query,*,session_id=''):
  entered.set();threading.Event().wait(10)
p=Barrier();p._base_url='https://fixture.invalid';p._api_key='fixture';p._session_id='old';p._scope_type='agent';p._scope_key='barbara'
p._config={'outbox_prototype_enabled':True,'outbox_directory':sys.argv[1],'outbox_binding':json.loads(sys.argv[2])}
manager=MemoryManager();manager.add_provider(p)
manager.queue_prefetch_all('fixture blocker',session_id='old');assert entered.wait(2)
manager.sync_all('unobserved','unobserved',session_id='old')
assert p._outbox_store is None and p.flush_pending(timeout=.01) is True
os._exit(76)
'''
    m,p,binding=setup(tmp_path)
    env={k:v for k,v in os.environ.items() if not k.startswith(('PALACEOFTRUTH_','SECONDBRAIN_'))}
    result=subprocess.run([sys.executable,'-c',script,str(tmp_path/'unobserved'),json.dumps(binding)],env=env,timeout=5)
    assert result.returncode==76 and not (tmp_path/'unobserved').exists()


def test_precommit_native_queue_gap_and_checkpoint_deadline_remain_honest(tmp_path,monkeypatch):
    m,p,binding=setup(tmp_path);store=admit(p)
    import threading
    from tests.test_hermes_checkpoint_repair import wire,event
    entered,release=threading.Event(),threading.Event();original=store.admit
    def stalled(*args,**kwargs):
        entered.set();assert release.wait(2)
        return original(*args,**kwargs)
    monkeypatch.setattr(store,'admit',stalled)
    monkeypatch.setattr(type(p),'CHECKPOINT_TOTAL_SECONDS',.03)
    calls,_=wire(p)
    try:
        start=time.perf_counter();p.sync_turn('waiting for disk','answer')
        assert time.perf_counter()-start<.15 and entered.wait(1)
        assert p.last_outbox_receipt is None
        assert len(store.records())==1  # Queued/active task is not committed.
        start=time.perf_counter()
        with pytest.raises(RuntimeError):p.on_pre_compress([event()],require_checkpoint=True)
        assert time.perf_counter()-start<.15 and not calls
        assert p.flush_pending(timeout=.01) is False
    finally:release.set()
    assert p.flush_pending(timeout=1)
    assert len(store.records())==2 and not calls
    state=p.shutdown_drain_state
    assert state['accepted_writes']==3 and state['completed_writes']==2 and state['failed_writes']==1
    assert not p._session_quota_jobs and state['active_tasks']==state['queued_writes']==0


def test_delivered_state_without_valid_receipt_is_rejected(tmp_path):
    import sqlite3
    m,p,binding=setup(tmp_path);store=admit(p)
    with sqlite3.connect(store.path) as con:con.execute("UPDATE records SET state='delivered'")
    with pytest.raises(m.OutboxError):store.records()


def test_external_transport_binding_and_receipt_extra_secrets_are_contained(tmp_path):
    m,p,binding=setup(tmp_path);store=admit(p)
    changed={**binding,'origin':'https://other.invalid'}
    with pytest.raises(m.OutboxError):m.RecoveryWorker(store,m.RestTransport(changed,'fixture'))
    backend=Backend(binding);backend.receipt_change={'authorization':'DO-NOT-RETAIN'}
    assert m.RecoveryWorker(store,backend).run(max_jobs=1,timeout=1)['remote_durable']==1
    assert b'DO-NOT-RETAIN' not in store.path.read_bytes()
    exposed=store.binding;exposed['origin']='https://mutated.invalid'
    assert store.binding==binding


def test_profile_home_and_activated_mode_cannot_silently_change(tmp_path):
    m,p,binding=setup(tmp_path);store=admit(p)
    with pytest.raises(m.OutboxError):p.initialize('other',hermes_home=str(tmp_path/'other-profile'))
    p._config['outbox_prototype_enabled']=False
    with pytest.raises(m.OutboxError):p.sync_turn('not legacy fallback','answer')
    assert p.flush_pending(timeout=1) and p.last_outbox_receipt is None
    assert p.shutdown_drain_state['failed_writes']==0  # Rejected before native admission.
    assert len(store.records())==1


def test_outside_profile_storage_is_rejected_before_writing(tmp_path):
    m,p,binding=setup(tmp_path)
    p._config['outbox_directory']=str(tmp_path.parent/'outside-profile')
    with pytest.raises(m.OutboxError):p.sync_turn('outside','outside')
    assert p.flush_pending(timeout=1) and p._outbox_store is None
    assert p.shutdown_drain_state['failed_writes']==0


def test_real_initialize_contract_keeps_cron_explicit_tools_only(tmp_path,monkeypatch):
    import sys
    m,p,binding=setup(tmp_path)
    module=sys.modules[type(p).__module__]
    config={**p._config,'base_url':binding['origin'],'api_key':'fixture-only','scope_type':'agent','scope_key':'barbara'}
    monkeypatch.setattr(module,'_load_config',lambda *_:config)
    p.initialize('cron_fixture',hermes_home=str(tmp_path),agent_context='cron',platform='cron')
    p._tenant_id=binding['tenant_id'];posts=[]
    p._request_json=lambda *a,**kw:posts.append(a) or {'job_id':'22222222-2222-4222-8222-222222222222'}
    assert json.loads(p.handle_tool_call('palace_remember',{'content':'explicit fixture','target':'memory'}))['ok'] is True
    p.sync_turn('automatic','automatic');p.on_memory_write('add','memory','automatic');p.on_session_switch('later',reset=True)
    assert p.flush_pending(timeout=1) and len(posts)==1 and p._outbox_store is None


def test_primary_public_initialize_reads_isolated_plugin_config(tmp_path):
    m,p,binding=setup(tmp_path)
    config={**p._config,'base_url':binding['origin'],'api_key':'fixture-only','scope_type':'agent','scope_key':'barbara'}
    (tmp_path/'palaceoftruth.json').write_text(json.dumps(config))
    p.initialize('session-fixture',hermes_home=str(tmp_path),agent_identity='barbara',platform='cli')
    store=admit(p)
    assert store.directory.parent==tmp_path and store.binding==binding
