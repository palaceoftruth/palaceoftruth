import hashlib,json,uuid
from datetime import datetime,timedelta,timezone
from fastapi import FastAPI,Request
from fastapi.testclient import TestClient
import pytest
from app import auth
from app.api import memory,mcp_oauth
from app.database import get_db
from app.models.item import Item
from app.models.job import Job
from app.models.palace import MemoryEntry
from app.schemas.memory import MemoryQueueHint
from tests.test_mcp_oauth import FakeSession,_Result,_client_row
TENANT='default'
KEY_ID='44444444-4444-4444-8444-444444444444'
CLIENT_ID='55555555-5555-4555-8555-555555555555'
CLIENT_KEY='hermes-barbara'
ITEM='33333333-3333-4333-8333-333333333333'
API_SECRET='fixture-api-secret'
OAUTH_SECRET='fixture-oauth-secret'
STAMP='2026-10-10T12:00:00+00:00'

class CommittedStore:
    """Mock persistence only; run the real SQLAlchemy models and acceptance algorithm.

    Receipts open a different read handle and cannot see pending uncommitted rows.
    SQLAlchemy expressions are used to locate the real idempotency replay record.
    """
    def __init__(self):
        self.committed = {}
        self.pending = []
        self.commits = 0
    def add(self, row):
        self.pending.append(row)
    async def flush(self):
        import uuid
        for row in self.pending:
            if row.id is None:
                row.id = uuid.uuid4()
            if isinstance(row, Job) and row.created_at is None:
                row.created_at = datetime.fromisoformat(STAMP)
    async def commit(self):
        await self.flush()
        for row in self.pending:
            self.committed[type(row), row.id] = row
        self.pending.clear()
        self.commits += 1
    async def scalar(self, statement):
        params = statement.compile().params
        for (kind, _), row in self.committed.items():
            if kind is Job and row.payload.get('idempotency_key') in params.values():
                return row
        return None
    async def get(self, model, ident, **kwargs):
        return self.committed.get((model, ident))
    async def refresh(self, row):
        assert (type(row), row.id) in self.committed
    async def execute(self, statement):
        class Result:
            def scalars(self): return self
            def first(self): return None
        return Result()

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

@pytest.mark.parametrize('tenant',['default','11111111-1111-4111-8111-111111111111'])
@pytest.mark.parametrize('mode',['api_key','mcp_oauth'])
@pytest.mark.parametrize('fault',[None,'grant','scope','tenant','missing_pointer'])
def test_actual_auth_receipt_route(monkeypatch,tenant,mode,fault):
    adb=AuthDatabase(mode=mode);adb.api_row['tenant_id']=tenant;adb.rows[0]['tenant_id']=tenant
    store=CommittedStore();app=FastAPI()
    app.include_router(memory.router,prefix='/api/v1');app.include_router(mcp_oauth.router,prefix='/api/v1')
    monkeypatch.setattr(auth,'system_async_session',lambda:adb)
    monkeypatch.setattr(mcp_oauth,'async_session',lambda:adb)
    class FreshReader:
        async def get(self,model,ident):return store.committed.get((model,ident))
    async def db(request:Request):yield store if request.method=='POST' else FreshReader()
    app.dependency_overrides[get_db]=db
    async def hint(request):return MemoryQueueHint(state='unknown')
    async def enqueue(*a,**k):return None
    monkeypatch.setattr(memory,'_memory_queue_contract_hint',hint);monkeypatch.setattr(memory,'_enqueue_memory_job_or_raise',enqueue)
    with TestClient(app) as client:
        if mode=='api_key':headers={'X-API-Key':API_SECRET,'X-MCP-Scopes':'read,write,write:agent'}
        else:
            minted=client.post('/api/v1/memory/mcp/oauth/token',data={'grant_type':'client_credentials','client_id':CLIENT_KEY,'client_secret':OAUTH_SECRET,'scope':'read write write:agent','resource':'https://testserver/api/v1'})
            assert minted.status_code==200
            headers={'Authorization':'Bearer '+minted.json()['access_token']}
        who=client.get('/api/v1/memory/whoami',headers={**headers,'X-Palace-Agent-Scope-Key':'forged'})
        assert who.status_code==200
        assert who.json()['tenant_id']==tenant and who.json()['credential_id']==(KEY_ID if mode=='api_key' else CLIENT_ID)
        missing=client.get('/api/v1/memory/jobs/'+ITEM+'/checkpoint-receipt',headers=headers)
        assert missing.status_code==404 and missing.json()['detail']=='Checkpoint not found'
        body={'tenant_id':tenant,'scope':{'type':'agent','key':'barbara'},'title':'fixture','body':'exact synthetic body','source':'fixture','idempotency_key':'a'*64,'relationship_policy':'skip','enable_ai_enrichment':False}
        body['created_at']=STAMP
        accepted=client.post('/api/v1/memory/entries',json=body,headers=headers)
        assert accepted.status_code==202,accepted.json()
        data=accepted.json();job=store.committed[(Job,uuid.UUID(data['job_id']))]
        if fault=='tenant':job.tenant_id='unrelated'
        if fault=='scope':job.payload['scope_key']='sibling'
        if fault=='missing_pointer':job.payload.pop('memory_entry_id')
        if fault=='grant':
            if mode=='api_key':headers['X-MCP-Scopes']='write'
            else:adb.rows[0]['allowed_scopes']=['write']
        response=client.get('/api/v1/memory/jobs/'+data['job_id']+'/checkpoint-receipt',headers=headers)
        if fault:
            assert response.status_code in {403,404,409},response.json()
        else:
            assert response.status_code==200,response.json()
            r=response.json();assert r['durable'] is True and r['tenant_id']==tenant
            assert r['body_sha256']==hashlib.sha256(body['body'].encode()).hexdigest()
            assert r['idempotency_key']==body['idempotency_key'] and r['source_item_id']==data['source_item_id']
            replay=client.post('/api/v1/memory/entries',json=body,headers=headers)
            assert replay.json()['job_id']==data['job_id'] and len(store.committed)==3
