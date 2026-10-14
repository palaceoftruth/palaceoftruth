"""Actual token/auth/whoami/POST/receipt routes behind fixture TLS.

Only database/queue persistence is mocked. No auth dependency/identity/route
response is overridden and no production source or credential is initialized.
"""
import copy
import json
import os
import ssl
import subprocess
import sys
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

import pytest
from fastapi import FastAPI,Request
from fastapi.testclient import TestClient
from app import auth
from app.api import memory,mcp_oauth
from app.database import get_db
from app.schemas.memory import MemoryQueueHint
from tests.test_mcp_oauth import FakeSession,_Result,_client_row
from tests.test_hermes_checkpoint_repair import CommittedStore
from tests.test_hermes_outbox import setup,admit,drain,ROOT,TENANT,ITEM
from tests.test_hermes_outbox_auth import KEY_ID,CLIENT_ID,CLIENT_KEY

API_SECRET='fixture-api-secret'
OAUTH_SECRET='fixture-oauth-secret'


class AuthDatabase(FakeSession):
    def __init__(self,*,mode='mcp_oauth',fault=None,stage='identity'):
        self.mode,self.fault,self.stage=mode,fault,stage
        self.current_stage='identity';self.denied=False
        self.api_row={'id':uuid.UUID(KEY_ID),'tenant_id':TENANT,'key_hash':auth.hash_secret(API_SECRET),
                      'scopes':['read','write','write:agent'],'revoked_at':None,
                      'expires_at':datetime.now(timezone.utc)+timedelta(hours=1)}
        row=_client_row(id=uuid.UUID(CLIENT_ID),tenant_id=TENANT,client_key=CLIENT_KEY,
            allowed_scopes=['read','write','write:agent'],oauth_client_secret_hash=auth.hash_secret(OAUTH_SECRET),
            agent_scope_key='barbara',containment_mode='hermes_agent',
            allow_all_agent_scope_reads=False,allow_tenant_shared_reads=False,allow_workspace_scope_reads=False)
        super().__init__(row=row)
        if fault=='tenant':self.api_row['tenant_id']=ITEM;row['tenant_id']=ITEM
        if fault=='principal':self.api_row['id']=uuid.UUID(ITEM);row['id']=uuid.UUID(ITEM)
        if fault=='scope':row['agent_scope_key']='sibling'
        if fault=='grant':self.api_row['scopes']=['read'];row['allowed_scopes']=['read']

    async def execute(self,statement,params=None):
        sql=str(statement).lower();params=params or {}
        deny=self.current_stage==self.stage
        if 'from api_keys' in sql:
            row=self.api_row
            if self.fault=='expired' and deny:row['expires_at']=datetime.now(timezone.utc)-timedelta(seconds=1)
            if self.fault=='revoked' and deny:row['revoked_at']=datetime.now(timezone.utc)
            assert 'revoked_at is null' in sql and 'expires_at > current_timestamp' in sql
            if row['revoked_at'] is not None or row['expires_at']<=datetime.now(timezone.utc):return _Result([])
            if row['key_hash'] not in {params.get('hash'),params.get('legacy_hash')}:return _Result([])
            return _Result([row])
        if 'from mcp_oauth_access_tokens t' in sql:
            token=next((p for p in self.tokens if p['token_hash'] in {params.get('token_hash'),params.get('legacy_token_hash')}),None)
            if not token:return _Result([])
            c=self.rows[0]
            if token['client_id']!=c['id'] or token['tenant_id']!=c['tenant_id']:return _Result([])
            return _Result([{'token_id':uuid.uuid4(),'stored_token_hash':token['token_hash'],
                'tenant_id':token['tenant_id'],'token_scopes':json.loads(token['scopes']) if isinstance(token['scopes'],str) else token['scopes'],'token_resource':token['resource'],
                'expires_at':datetime.now(timezone.utc)-timedelta(seconds=1) if self.fault=='expired' and deny else token['expires_at'],
                'token_revoked_at':datetime.now(timezone.utc) if self.fault=='revoked' and deny else None,
                'client_revoked_at':c['oauth_revoked_at'],'delegated_grant_id':None,'grant_revoked_at':None,
                'delegated_agent_scope_keys':[],'delegated_workspace_scope_keys':[],
                'client_id':c['id'],'client_key':c['client_key'],'display_name':c['display_name'],
                'allowed_scopes':c['allowed_scopes'],'agent_scope_key':c['agent_scope_key'],
                'containment_mode':c['containment_mode'],'allow_all_agent_scope_reads':False,
                'allow_tenant_shared_reads':False,'allow_workspace_scope_reads':False}])
        if sql.startswith('update api_keys') or (sql.startswith('update mcp_clients') and 'last_seen_at' in sql):return _Result([])
        if 'insert into mcp_request_audit_events' in sql:
            self.audit_events.append({'error_class':params.get('error_class')})
            return _Result([])
        return await super().execute(statement,params)


@contextmanager
def actual_backend(tmp_path,monkeypatch,*,mode='mcp_oauth',fault=None,stage='identity',tenant=TENANT):
    cert,key=tmp_path/'actual-cert.pem',tmp_path/'actual-key.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),
        '-days','1','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost'],
        check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    db=AuthDatabase(mode=mode,fault=fault,stage=stage);committed=CommittedStore();calls=[]
    if fault not in {'tenant'}:
        db.api_row['tenant_id']=tenant;db.rows[0]['tenant_id']=tenant
    app=FastAPI();app.include_router(memory.router,prefix='/api/v1');app.include_router(mcp_oauth.router,prefix='/api/v1')
    async def domain_db():yield committed
    app.dependency_overrides[get_db]=domain_db
    monkeypatch.setattr(auth,'system_async_session',lambda:db)
    monkeypatch.setattr(mcp_oauth,'async_session',lambda:db)
    async def queue_hint(request):return MemoryQueueHint(state='unknown')
    async def enqueue(*a,**kw):return None
    monkeypatch.setattr(memory,'_memory_queue_contract_hint',queue_hint)
    monkeypatch.setattr(memory,'_enqueue_memory_job_or_raise',enqueue)
    # Exactly real auth dependencies and response routes; bridge only TLS/ASGI.
    client_holder={}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def dispatch(self):
            phase='post' if self.command=='POST' and self.path.endswith('/entries') else 'receipt' if self.path.endswith('/checkpoint-receipt') else 'identity'
            db.current_stage=phase
            size=int(self.headers.get('Content-Length','0'));body=self.rfile.read(size) if size else None
            calls.append((self.command,self.path))
            response=client_holder['client'].request(self.command,self.path,content=body,headers=dict(self.headers))
            detail=response.json().get('detail') if response.status_code>=400 else None
            calls[-1]=(self.command,self.path,response.status_code,detail if isinstance(detail,str) else None)
            self.send_response(response.status_code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(response.content)));self.end_headers()
            self.wfile.write(response.content)
        do_GET=dispatch;do_POST=dispatch
    http=ThreadingHTTPServer(('127.0.0.1',0),Handler);origin='https://localhost:'+str(http.server_port)
    ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain(cert,key);http.socket=ctx.wrap_socket(http.socket,server_side=True)
    with TestClient(app,base_url=origin) as client:
        client_holder['client']=client;thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        try:yield origin,cert,db,committed,calls
        finally:http.shutdown();http.server_close();thread.join(2)


def prepared(tmp_path,origin,mode,tenant=TENANT):
    m,p,b=setup(tmp_path);b['origin']=origin;p._base_url=origin
    b['tenant_id']=tenant;p._tenant_id=tenant
    if mode=='mcp_oauth':
        b['credential']={'mode':'mcp_oauth','id':CLIENT_ID,'client_key':CLIENT_KEY}
        p._api_key='';p._oauth_client_secret=OAUTH_SECRET;p._oauth_client_key=CLIENT_KEY
    else:p._api_key=API_SECRET
    p._config['outbox_binding']=b
    store=admit(p);p.sync_turn('successor','successor');assert drain(p)
    return m,p,b,store


def real_cli(store,b,cert,binding_file,mode):
    binding_file.write_text(json.dumps(b));env={'PATH':os.environ['PATH']}
    if mode=='mcp_oauth':env.update(PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET=OAUTH_SECRET,PALACEOFTRUTH_MCP_CLIENT_KEY=CLIENT_KEY)
    else:env['PALACE_OUTBOX_API_KEY']=API_SECRET
    result=subprocess.run([sys.executable,str(ROOT/'scripts/recover_hermes_outbox.py'),'--directory',str(store.directory),
        '--binding-file',str(binding_file),'--ca-file',str(cert),'--max-jobs','2','--timeout','5'],
        env=env,text=True,capture_output=True,timeout=10)
    assert API_SECRET not in result.stdout+result.stderr and OAUTH_SECRET not in result.stdout+result.stderr
    assert API_SECRET.encode() not in store.path.read_bytes() and OAUTH_SECRET.encode() not in store.path.read_bytes()
    return result


@pytest.mark.parametrize('mode',['api_key','mcp_oauth'])
def test_actual_auth_token_whoami_acceptance_and_canonical_readback(tmp_path,monkeypatch,mode):
    with actual_backend(tmp_path,monkeypatch,mode=mode) as (origin,cert,authdb,domain,calls):
        m,p,b,store=prepared(tmp_path,origin,mode)
        result=real_cli(store,b,cert,tmp_path/'binding.json',mode)
        assert result.returncode==0,(result.stdout+result.stderr,calls,authdb.audit_events)
        assert json.loads(result.stdout)['remote_durable']==2
        assert all(r['state']=='delivered' for r in store.records())
        assert len([c for c in calls if c[0]=='POST' and c[1].endswith('/entries')])==2
        if mode=='mcp_oauth':assert authdb.tokens and all(t['client_id']==uuid.UUID(CLIENT_ID) for t in authdb.tokens)


@pytest.mark.parametrize('mode',['api_key','mcp_oauth'])
@pytest.mark.parametrize('fault',['expired','revoked','tenant','principal','grant'])
def test_real_auth_denials_leave_fifo_pending_before_delivery(tmp_path,monkeypatch,mode,fault):
    with actual_backend(tmp_path,monkeypatch,mode=mode,fault=fault) as (origin,cert,authdb,domain,calls):
        m,p,b,store=prepared(tmp_path,origin,mode)
        result=real_cli(store,b,cert,tmp_path/'binding.json',mode)
        assert result.returncode==2
        assert all(r['state']=='pending' for r in store.records())
        assert not [c for c in calls if c[0]=='POST' and c[1].endswith('/entries')]
        assert not domain.committed


@pytest.mark.parametrize('mode',['api_key','mcp_oauth'])
@pytest.mark.parametrize('stage',['post','receipt'])
def test_actual_revocation_at_post_or_receipt_preserves_pending_and_stops_successor(tmp_path,monkeypatch,mode,stage):
    with actual_backend(tmp_path,monkeypatch,mode=mode,fault='revoked',stage=stage) as (origin,cert,authdb,domain,calls):
        m,p,b,store=prepared(tmp_path,origin,mode)
        result=real_cli(store,b,cert,tmp_path/'binding.json',mode)
        assert result.returncode==2 and all(r['state']=='pending' for r in store.records())
        assert len([c for c in calls if c[0]=='POST' and c[1].endswith('/entries')])==1


def test_actual_oauth_agent_scope_is_server_owned_and_denied_before_post(tmp_path,monkeypatch):
    with actual_backend(tmp_path,monkeypatch,mode='mcp_oauth',fault='scope') as (origin,cert,authdb,domain,calls):
        m,p,b,store=prepared(tmp_path,origin,'mcp_oauth');result=real_cli(store,b,cert,tmp_path/'binding.json','mcp_oauth')
        assert result.returncode==2 and not domain.committed
        assert all(r['state']=='pending' for r in store.records())
        assert not [c for c in calls if c[0]=='POST' and c[1].endswith('/entries')]


def test_actual_client_credentials_refresh_is_ephemeral_same_client_not_api_key(tmp_path,monkeypatch):
    with actual_backend(tmp_path,monkeypatch) as (origin,cert,authdb,domain,calls):
        m,p,b,store=prepared(tmp_path,origin,'mcp_oauth')
        import importlib,time
        c=importlib.import_module(m.__package__+'.credentials')
        credentials=c.OAuthCredentials(b,client_secret=OAUTH_SECRET,ca_file=str(cert))
        transport=m.RestTransport(b,credentials=credentials,ca_file=str(cert))
        first=transport.call('GET','/api/v1/memory/whoami',deadline=time.perf_counter()+5)
        credentials._expires=time.perf_counter()-1
        second=transport.call('GET','/api/v1/memory/whoami',deadline=time.perf_counter()+5)
        assert first['credential_id']==second['credential_id']==CLIENT_ID
        assert len(authdb.tokens)==2 and authdb.tokens[0]['token_hash']!=authdb.tokens[1]['token_hash']
        assert all('Authorization' in credentials.headers(time.perf_counter()+1) for _ in [1])
        assert 'X-API-Key' not in credentials.headers(time.perf_counter()+1)
        assert OAUTH_SECRET.encode() not in store.path.read_bytes()


def test_refresh_revalidates_changed_server_principal_before_post(tmp_path,monkeypatch):
    with actual_backend(tmp_path,monkeypatch) as (origin,cert,authdb,domain,calls):
        m,p,b,store=prepared(tmp_path,origin,'mcp_oauth')
        import importlib,time
        c=importlib.import_module(m.__package__+'.credentials')
        credential=c.OAuthCredentials(b,client_secret=OAUTH_SECRET,ca_file=str(cert))
        transport=m.RestTransport(b,credentials=credential,ca_file=str(cert))
        assert transport.call('GET','/api/v1/memory/whoami',deadline=time.perf_counter()+5)['credential_id']==CLIENT_ID
        authdb.rows[0]['id']=uuid.UUID(ITEM);credential._expires=time.perf_counter()-1
        with pytest.raises(m.OutboxError):
            transport.call('POST','/api/v1/memory/entries',store.records()[0]['payload'],deadline=time.perf_counter()+5)
        assert not domain.committed
        assert not [r for r in calls if r[0]=='POST' and r[1].endswith('/entries')]
        assert all(r['state']=='pending' for r in store.records())


def test_actual_revoked_oauth_client_cannot_mint_replacement(tmp_path,monkeypatch):
    with actual_backend(tmp_path,monkeypatch) as (origin,cert,authdb,domain,calls):
        m,p,b,store=prepared(tmp_path,origin,'mcp_oauth')
        authdb.rows[0]['oauth_revoked_at']=datetime.now(timezone.utc)
        result=real_cli(store,b,cert,tmp_path/'binding.json','mcp_oauth')
        assert result.returncode==2 and not domain.committed
        assert not [r for r in calls if r[0]=='POST' and r[1].endswith('/entries')]
        assert all(r['state']=='pending' for r in store.records())


@pytest.mark.skipif(not os.environ.get('SAR1406_NORMAL_LOADER'),reason='official CLI discovery contract')
def test_documented_plugin_cli_uses_real_profile_secret_reader_and_auth_routes(tmp_path,monkeypatch):
    with actual_backend(tmp_path,monkeypatch) as (origin,cert,authdb,domain,calls):
        m,p,b,store=prepared(tmp_path,origin,'mcp_oauth')
        import shutil
        shutil.copytree(ROOT/'third_party_plugins/hermes/memory/palaceoftruth',tmp_path/'plugins/palaceoftruth')
        (tmp_path/'config.yaml').write_text('memory:\n  provider: palaceoftruth\n')
        binding_file=tmp_path/'binding.json';binding_file.write_text(json.dumps(b))
        script=r'''
import argparse,sys
from plugins.memory import discover_plugin_cli_commands
commands=discover_plugin_cli_commands();assert len(commands)==1 and commands[0]['name']=='palaceoftruth'
parser=argparse.ArgumentParser();commands[0]['setup_fn'](parser)
args=parser.parse_args(sys.argv[1:]);raise SystemExit(args.func(args))
'''
        env={'PATH':os.environ['PATH'],'HOME':str(tmp_path),'HERMES_HOME':str(tmp_path),
             'PYTHONPATH':os.environ['PYTHONPATH'],'PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET':OAUTH_SECRET,
             'PALACEOFTRUTH_MCP_CLIENT_KEY':CLIENT_KEY}
        result=subprocess.run([sys.executable,'-c',script,'recover-outbox','--directory',str(store.directory),
            '--binding-file',str(binding_file),'--ca-file',str(cert),'--max-jobs','2','--timeout','5'],
            env=env,capture_output=True,text=True,timeout=10)
        assert result.returncode==0,result.stdout+result.stderr
        assert OAUTH_SECRET not in result.stdout+result.stderr
        assert json.loads(result.stdout)['remote_durable']==2
        assert all(r['state']=='delivered' for r in store.records())
