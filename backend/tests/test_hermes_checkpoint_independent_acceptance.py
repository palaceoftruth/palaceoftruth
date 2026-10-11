"""Independent final-product probes. No provider initialization or external API."""
import hashlib, json, threading, time, sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from tests.test_hermes_checkpoint_repair import ready, wire, event, CommittedStore
from tests.test_hermes_checkpoint_composed import start_server, stop_server, tls_files
from app.api import memory as api
from app.auth import verify_memory_auth
from app.database import get_db
from app.models.item import Item
from app.models.job import Job
from app.schemas.memory import MemoryQueueHint, MemoryEntryRequest


def test_real_product_route_restart_replay_and_honest_latest_timestamp(monkeypatch):
    store=CommittedStore(); app=FastAPI(); app.include_router(api.router,prefix='/api/v1')
    @app.middleware('http')
    async def principal(request, next):
        request.state.tenant_id='tenant-fixture'; request.state.auth_mode='api_key'; request.state.mcp_allowed_scopes=['read','write','write:agent']
        return await next(request)
    async def auth(request:Request):return 'fixture'
    async def db(request:Request):yield store
    app.dependency_overrides[verify_memory_auth]=auth; app.dependency_overrides[get_db]=db
    async def hint(request):return MemoryQueueHint(state='unknown')
    async def enqueue(*args,**kwargs):return None
    monkeypatch.setattr(api,'_memory_queue_contract_hint',hint); monkeypatch.setattr(api,'_enqueue_memory_job_or_raise',enqueue)
    rows=[event('a','same',timestamp='2026-10-10T08:00:00-04:00'),event('b','same',timestamp=1791637200.0),event('c','reply',role='assistant',timestamp='2026-10-10T13:30:00Z')]
    captured=[]; responses=[]
    with TestClient(app) as client:
        def transport(method,path,payload=None,**kwargs):
            if payload:captured.append(payload); MemoryEntryRequest.model_validate(payload)
            r=client.request(method,path,json=payload,headers=dict(kwargs['snapshot'].headers)); responses.append(r)
            assert r.status_code<300,r.text
            return r.json()
        _,p=ready(); p._checkpoint_request_json=transport
        first=p.on_pre_compress(rows,require_checkpoint=True)
        _,q=ready(); q._checkpoint_request_json=transport
        second=q.on_pre_compress(json.loads(json.dumps(rows)),require_checkpoint=True)
    assert first==second and captured[0]==captured[1]
    assert captured[0]['created_at']=='2026-10-10T13:30:00+00:00'
    assert [r.status_code for r in responses]==[202,200,202,200]
    assert responses[2].json()['replayed'] is True
    assert len([1 for kind,_ in store.committed if kind is Item])==1
    assert len([1 for kind,_ in store.committed if kind is Job])==1
    evidence=json.loads(captured[0]['body'])['source_evidence']
    assert [r['source_event_id'] for r in evidence]==['a','b','c']
    assert [r['position'] for r in evidence]==[0,1,2]
    assert evidence[0]['text']==evidence[1]['text']=='same'


@pytest.mark.parametrize('lock_name',['_checkpoint_lock','_lifecycle_lock','_server_identity_lock','_tenant_id_lock','_bearer_lock','_write_quota_lock'])
def test_every_admission_and_quota_lock_obeys_budget(lock_name,monkeypatch):
    _,p=ready(); calls,_=wire(p); monkeypatch.setattr(type(p),'CHECKPOINT_TOTAL_SECONDS',.04)
    held=getattr(p,lock_name); acquired=threading.Event(); release=threading.Event()
    def owner():
        with held:acquired.set(); release.wait(1)
    thread=threading.Thread(target=owner); thread.start(); assert acquired.wait(1)
    start=time.perf_counter()
    try:
        with pytest.raises(RuntimeError):p.on_pre_compress([event()],require_checkpoint=True)
        assert time.perf_counter()-start<.2
        assert not calls
    finally:release.set(); thread.join(1)


# F1/F2 inverse expectations execute the normal exported provider.
from tests.test_hermes_checkpoint_native_export import (
    test_export_contains_native_lifecycle_and_checkpoint,
    test_closed_capture_rejected_no_context_lost_and_old_quota_not_reset,
    test_checkpoint_cleanup_caller_deadline_and_eventual_accounting,
)


@pytest.mark.parametrize('rows',[
 [event('a'),event('b',timestamp=None)],
 [event('a'),event('b',json.dumps({'checkpoint_handoff':2,'remaining_work':[123]}))],
 [event(str(i)) for i in range(17)],
 [event('a'),event('b','x'*24000)],
 [event('a'),event('a')]
])
def test_adapted_partial_archive_defect_now_rejects_before_post(rows):
    _,p=ready(); calls,payloads=wire(p)
    with pytest.raises(RuntimeError):p.on_pre_compress(rows,require_checkpoint=True)
    assert not calls and not payloads


def test_adapted_order_overlap_restart_defect_now_keeps_occurrences():
    _,p=ready(); _,payloads=wire(p)
    a,b,c=event('a','same'),event('b','same'),event('c','same',role='assistant')
    p.on_pre_compress([a,b,c],require_checkpoint=True)
    p.on_pre_compress([b,c],require_checkpoint=True)
    p.on_pre_compress([c,b,a],require_checkpoint=True)
    _,q=ready(); _,restarted=wire(q); q.on_pre_compress([a,b,c],require_checkpoint=True)
    assert payloads[0]==restarted[0] and payloads[0]['body']!=payloads[2]['body']
    manifests=[json.loads(x['body']) for x in payloads]
    assert [r['source_event_id'] for r in manifests[0]['source_evidence']]==['a','b','c']
    assert manifests[0]['source_evidence'][1]['evidence_id']==manifests[1]['source_evidence'][0]['evidence_id']


def test_source_absorption_overlap_is_not_fully_validated():
    _,p=ready(); _,payloads=wire(p)
    p.on_pre_compress([event('a',_absorbed_message_uids=['b']),event('b')],require_checkpoint=True)
    evidence=json.loads(payloads[0]['body'])['source_evidence']
    assert evidence[0]['absorbed_source_event_ids']==['b'] and evidence[1]['source_event_id']=='b'


def test_optional_author_metadata_is_not_preserved_or_bound():
    _,p=ready(); _,payloads=wire(p)
    p.on_pre_compress([event('a','same',turn_author={'id':'alice','is_bot':False})],require_checkpoint=True)
    p.on_pre_compress([event('a','same',turn_author={'id':'bob','is_bot':True})],require_checkpoint=True)
    assert payloads[0]==payloads[1]
    assert 'turn_author' not in payloads[0]['body']


# The original host-global-gap probe is preserved verbatim as evidence, not
# inverted into a false release assertion. Host-global exit acceptance is blocked.

@pytest.mark.parametrize('code',[301,307,308])
@pytest.mark.parametrize('method',['GET','POST'])
@pytest.mark.parametrize('target_kind',['cross','same','downgrade'])
def test_independent_real_child_redirect_rejected(code,method,target_kind,tls_files):
    from http.server import BaseHTTPRequestHandler
    hits=[]; destinations=[]
    class Target(BaseHTTPRequestHandler):
        def do_GET(self):destinations.append(self.path); self.send_response(200); self.end_headers(); self.wfile.write(b'{}')
        do_POST=do_GET
        def log_message(self,*a):pass
    target,tt=start_server(Target)
    class Origin(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append((self.path,dict(self.headers)))
            if self.command=='POST':self.rfile.read(int(self.headers.get('Content-Length',0)))
            self.send_response(code)
            self.send_header('Location','/same-target' if target_kind=='same' else f'http://127.0.0.1:{target.server_port}/other')
            self.end_headers()
        do_POST=do_GET
        def log_message(self,*a):pass
    origin,ot=start_server(Origin,tls_files if target_kind=='downgrade' else None)
    try:
        _,p=ready(); p._base_url=f"{'https' if target_kind=='downgrade' else 'http'}://127.0.0.1:{origin.server_port}"
        p._oauth_client_secret='fixture'; p._server_identity_loaded=True; p._server_agent_scope_key='barbara'; p._bearer_token='fixture-bearer'; p._bearer_expires_at=datetime.now(timezone.utc)+timedelta(hours=1)
        if target_kind=='downgrade':p._config['checkpoint_ca_file']=str(tls_files[0])
        deadline=time.perf_counter()+1
        snapshot=p._checkpoint_snapshot(deadline)
        with pytest.raises(RuntimeError):p._checkpoint_request_json(method,'/initial',{'fixture':True} if method=='POST' else None,snapshot=snapshot,deadline=deadline)
        assert len(hits)==1 and hits[0][1]['Authorization']=='Bearer fixture-bearer'
        assert not destinations
    finally:stop_server(origin,ot); stop_server(target,tt)
