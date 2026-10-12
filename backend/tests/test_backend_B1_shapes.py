import pytest,uuid
from fastapi import FastAPI,Request
from fastapi.testclient import TestClient
from app import auth
from app.api import memory,mcp_oauth
from app.database import get_db
from app.schemas.memory import MemoryQueueHint
from tests.test_backend_checkpoint_auth import AuthDatabase,CommittedStore,API_SECRET,OAUTH_SECRET,CLIENT_KEY,STAMP
@pytest.mark.parametrize('mode',['api_key','mcp_oauth'])
@pytest.mark.parametrize('shape',['pointer_int','pointer_float','pointer_bool','pointer_null','pointer_list','pointer_object','pointer_empty','pointer_compact','pointer_braces','payload_int','payload_null','payload_list','payload_string','payload_bool','payload_float','payload_object','payload_empty','scope_list','scope_object','scope_bool','scope_int','scope_null','scope_string'])
@pytest.mark.parametrize('deny',[False,True])
def test_all_json_shapes_and_auth_precedence(monkeypatch,mode,shape,deny):
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
  from app.models.job import Job
  job=store.committed[(Job,uuid.UUID(accepted.json()['job_id']))]
  values={'int':7,'float':1.2,'bool':True,'null':None,'list':[],'object':{},'empty':'','compact':str(uuid.uuid4()).replace('-',''),'braces':'{'+str(uuid.uuid4())+'}','string':'text'}
  family,kind=shape.split('_',1)
  if family=='pointer':job.payload['memory_entry_id']=values[kind]
  elif family=='scope':job.payload['scope_type']=values[kind]
  else:job.payload=values[kind]
  if deny:
   if mode=='api_key':adb.api_row['scopes']=['write']
   else:adb.rows[0]['allowed_scopes']=['write']
  result=client.get(path,headers=headers)
  assert result.status_code== (403 if deny else 409),result.text
  assert result.json().get('durable') is not True
  assert len(store.committed)==3
