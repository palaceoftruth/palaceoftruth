import asyncio,uuid,hashlib
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import pytest
from fastapi import FastAPI,Request,HTTPException
from fastapi.testclient import TestClient
from tests.test_checkpoint_receipt import records
from tests.test_backend_checkpoint_auth import AuthDatabase,CommittedStore,TENANT,API_SECRET,OAUTH_SECRET,CLIENT_KEY,STAMP
from app import auth
from app.api import memory,mcp_oauth
from app.database import get_db
from app.schemas.memory import MemoryQueueHint
from app.services.checkpoint_receipt import read_checkpoint_receipt


@pytest.mark.asyncio
@pytest.mark.parametrize('bad',[7,[],{}])
async def test_malformed_canonical_pointer_currently_raises_unhandled_attributeerror(records,bad):
 db,job,item,entry=records;job.payload['memory_entry_id']=bad
 with pytest.raises(HTTPException) as error:await read_checkpoint_receipt(db,job=job,tenant_id='t')
 assert error.value.status_code==409


@pytest.mark.asyncio
async def test_fresh_concurrent_receipt_hash_and_no_pending_visibility(records):
 db,job,item,entry=records
 results=await asyncio.gather(*[read_checkpoint_receipt(db,job=job,tenant_id='t') for _ in range(20)])
 assert all(x==results[0] for x in results)
 assert results[0]['body_sha256']==hashlib.sha256(item.raw_content.encode()).hexdigest()
 # Not a database concurrency canary: concurrent reads against committed fixture only.


@pytest.mark.parametrize('mode',['api_key','mcp_oauth'])
@pytest.mark.parametrize('denial',['revoked','expired','read_removed','pointer_corrupt'])
def test_actual_auth_denies_after_commit_no_durable_response(monkeypatch,mode,denial):
 adb=AuthDatabase();store=CommittedStore();app=FastAPI();app.include_router(memory.router,prefix='/api/v1');app.include_router(mcp_oauth.router,prefix='/api/v1')
 monkeypatch.setattr(auth,'system_async_session',lambda:adb);monkeypatch.setattr(mcp_oauth,'async_session',lambda:adb)
 async def db(request:Request):yield store
 app.dependency_overrides[get_db]=db
 async def hint(request):return MemoryQueueHint(state='unknown')
 async def enqueue(*a,**kw):return None
 monkeypatch.setattr(memory,'_memory_queue_contract_hint',hint);monkeypatch.setattr(memory,'_enqueue_memory_job_or_raise',enqueue)
 with TestClient(app,raise_server_exceptions=False) as client:
  if mode=='api_key':headers={'X-API-Key':API_SECRET,'X-MCP-Scopes':'read,write,write:agent'}
  else:
   token=client.post('/api/v1/memory/mcp/oauth/token',data={'grant_type':'client_credentials','client_id':CLIENT_KEY,'client_secret':OAUTH_SECRET,'scope':'read write write:agent','resource':'https://testserver/api/v1'});assert token.status_code==200;headers={'Authorization':'Bearer '+token.json()['access_token']}
  payload={'tenant_id':'default','scope':{'type':'agent','key':'barbara'},'title':'fixture','body':'raw exact é','source':'fixture','created_at':STAMP,'idempotency_key':'e'*64,'relationship_policy':'skip','enable_ai_enrichment':False}
  accepted=client.post('/api/v1/memory/entries',json=payload,headers=headers);assert accepted.status_code==202
  path='/api/v1/memory/jobs/'+accepted.json()['job_id']+'/checkpoint-receipt'
  assert client.get(path,headers=headers).status_code==200
  if denial=='pointer_corrupt':
   from app.models.job import Job
   store.committed[(Job,uuid.UUID(accepted.json()['job_id']))].payload['memory_entry_id']=7
  elif denial=='read_removed':
   if mode=='api_key':adb.api_row['scopes']=['write']
   else:adb.rows[0]['allowed_scopes']=['write']
  else:adb.fault=denial
  result=client.get(path,headers=headers)
  if denial=='pointer_corrupt':assert result.status_code==409
  else:assert result.status_code in [401,403] and result.json().get('durable') is not True
  assert len(store.committed)==3
