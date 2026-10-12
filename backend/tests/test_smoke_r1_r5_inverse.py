import sys,copy,json,os,sqlite3
from pathlib import Path
from datetime import timedelta
from types import SimpleNamespace
import pytest
sys.path.insert(0,str(Path(__file__).parent))
from test_smoke_control import doc_for,approval,observed,logs,Transport,gates,NOW,JOB,ENTRY
from scripts import smoke_control as c,smoke_preflight as p,check_memory_rollout_smoke as s

@pytest.mark.parametrize('which',['journal.sqlite3','anchor.json','fence'])
def test_DIAGNOSTIC_hardlinks_accepted(tmp_path,which):
 root=tmp_path/'store';d=doc_for(root);a=approval(d);j=c.Journal(root,a,host_id='fixture-host')
 if which=='fence':j.reserve(a);f=root/'reservations'/(a.slot+'.json')
 else:f=root/which
 os.link(f,tmp_path/'alias');assert f.stat().st_nlink==2
 with pytest.raises(c.ControlError):c.Journal(root,a,host_id='fixture-host')

def test_DIAGNOSTIC_erased_fence_and_row_replenishes_invocation(tmp_path):
 root=tmp_path/'store';d=doc_for(root);a=approval(d);j=c.Journal(root,a,host_id='fixture-host');t=Transport(d)
 c.run_reserved(a,j,t,gates,gates,now=NOW)
 (root/'reservations'/(a.slot+'.json')).unlink()
 with sqlite3.connect(root/'journal.sqlite3') as db:db.execute('DELETE FROM slots')
 with pytest.raises(c.ControlError):c.run_reserved(a,c.Journal(root,a,host_id='fixture-host'),t,gates,gates,now=NOW)
 assert len(t.posts)==1

@pytest.mark.parametrize('kind',['undated','future','counter_without_termination'])
def test_DIAGNOSTIC_missing_temporal_evidence_accepted(tmp_path,kind):
 d=doc_for(tmp_path/'store')
 if kind in ['undated','future']:
  d['baseline']['workers'][0]['last_terminated']={'reason':'Error','exitCode':1}
  if kind=='future':d['baseline']['workers'][0]['last_terminated']['finishedAt']=(NOW+timedelta(hours=1)).isoformat()
 d['baseline_sha256']=c.sha256_json(d['baseline']);w=observed(d)
 if kind=='counter_without_termination':w[0]['restart_count']+=1
 with pytest.raises(c.ControlError):c.temporal_gate(approval(d),w,logs(d),now=NOW)

class Kube:
 namespace='fixture'
 def __init__(self,d):
  self.d=d;self.calls=[]
  self.pods=[{'metadata':{'name':'worker-a','uid':'worker-uid','labels':{'app':'worker'}},'status':{'phase':'Running','containerStatuses':[{'name':'worker','containerID':'containerd://fixed','restartCount':108,'ready':True,'lastState':{'terminated':d['baseline']['workers'][0]['last_terminated']},'state':{'running':{}}}]}}]
  for name in ['backend','mcp']:
   for i in range(2):self.pods.append({'metadata':{'name':name+str(i),'uid':name+str(i)},'status':{'containerStatuses':[{'name':name,'ready':True,'imageID':'fixture@'+d['target_digest']}]}})
 def get(self,path,**kw):
  self.calls.append(path);d=self.d
  if path.endswith('/pods'):return {'items':self.pods}
  if path.endswith('/kube-system'):return {'metadata':{'uid':d['cluster_uid']}}
  if path.endswith('/fixture'):return {'metadata':{'uid':d['namespace_uid']}}
  if '/helmreleases/' in path:return {'metadata':{'uid':d['release_uid']},'spec':{'suspend':True,'values':{'image':{'backendDigest':d['target_digest']},'highAvailability':{'replicas':{'backend':2,'mcp':2,'valkeySentinels':3}}}},'status':{'history':[{'ociDigest':d['chart_digest']}]}}
  if '/kustomizations/' in path:return {'metadata':{'generation':9},'status':{'lastAppliedRevision':d['hold_git_revision'],'conditions':[{'type':'Ready','status':'True','observedGeneration':1}]}}
  if '/gitrepositories/' in path:return {'metadata':{'generation':9},'status':{'artifact':{'revision':d['hold_git_revision']},'conditions':[{'type':'Ready','status':'False','observedGeneration':9}]}}
  raise AssertionError(path)
 def get_text(self,path,**kw):return ''

def fixture(tmp_path):
 d=doc_for(tmp_path/'store');d['writer_digest']=d['target_digest'];d['hold_git_revision']='main@sha1:'+'a'*40;a=approval(d)
 args=s.build_parser().parse_args(['--mode','bounded','--namespace','fixture','--scope-key','rollout-smoke','--expected-sentinel-pods','3']);k=Kube(d)
 report={'checks':[],'alerts':[],'identity':{'tenant_id':'default','auth_mode':'mcp_oauth','credential_id':JOB,'mcp_client_key':'fixture-client','allowed_scopes':['read','write','write:workspace']}}
 t=Transport(d);return d,a,args,k,report,t

def test_DIAGNOSTIC_empty_logs_claim_full_coverage(tmp_path,monkeypatch):
 d,a,args,k,r,t=fixture(tmp_path);monkeypatch.setattr(s,'utc_now',lambda:NOW)
 with pytest.raises(c.ControlError):p.temporal_observations(s,args,k,a)

def test_DIAGNOSTIC_stale_conditions_false_git_ready_still_prove_hold(tmp_path):
 d,a,args,k,r,t=fixture(tmp_path)
 with pytest.raises(c.ControlError):p.runtime_binding(s,t,r,args,k,a)
 assert not t.posts
 assert not any('artifact/' in x or 'contents/' in x for x in k.calls)

@pytest.mark.parametrize('field',['tenant_id','auth_mode','credential_id','allowed_scopes'])
def test_identity_and_grant_mismatch_refused(tmp_path,field):
 d,a,args,k,r,t=fixture(tmp_path);r['identity'][field]=[] if field=='allowed_scopes' else 'wrong'
 with pytest.raises(c.ControlError):p.runtime_binding(s,t,r,args,k,a)
 assert not t.posts

def test_DIAGNOSTIC_extra_principal_secret_persisted(tmp_path):
 root=tmp_path/'store';d=doc_for(root);d['principal']['client_secret']='FIXTURE_SECRET_NOT_REAL'
 with pytest.raises(c.ControlError):approval(d)

def test_DIAGNOSTIC_readonly_receipt_hardlink_accepted(tmp_path):
 d=doc_for(tmp_path/'store');a=approval(d);t=Transport(d);t.record=True;envelope=c.recover_receipt(a,t)
 file=tmp_path/'receipt';file.write_bytes(c.canonical(envelope));file.chmod(0o600);os.link(file,tmp_path/'alias')
 with pytest.raises(c.ControlError):p.readonly_receipt(s,t,SimpleNamespace(receipt_file=str(file)),a)

@pytest.mark.parametrize('field',['tenant_id','scope','body_sha256','durable','job_id'])
def test_malformed_receipts_fail_closed(tmp_path,field):
 d=doc_for(tmp_path/'store');a=approval(d);t=Transport(d);t.record=True;t.corrupt=field
 with pytest.raises(c.ControlError):c.recover_receipt(a,t)
 assert not t.posts

def test_same_campaign_changed_tenant_cannot_replenish(tmp_path):
 root=tmp_path/'store';d=doc_for(root);a=approval(d);j=c.Journal(root,a,host_id='fixture-host');j.reserve(a)
 d['tenant_id']=d['payload']['tenant_id']='other';b=approval(d)
 with pytest.raises(c.ControlError):j.reserve(b)

def test_environment_cannot_enable_automatic_bounded_mode(monkeypatch):
 monkeypatch.setenv('PALACE_ROLLOUT_SMOKE_MODE','bounded');assert s.build_parser().parse_args([]).mode=='read-only'

def test_DIAGNOSTIC_mcp_404_is_passed(monkeypatch):
 import urllib.error
 def missing(*a,**kw):raise urllib.error.HTTPError('http://fixture',404,'missing',{},None)
 monkeypatch.setattr(s.urllib.request,'urlopen',missing);r={'target':'fixture','checks':[],'alerts':[]};s.check_mcp_reachable('http://fixture',r,timeout=1)
 assert not s._check_passed(r,'mcp_health') and r['alerts']

def test_api_safe_transport_denies_cross_origin_redirect():
 import threading,urllib.error
 from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
 calls=[]
 class Sink(BaseHTTPRequestHandler):
  def do_GET(self):calls.append('sink');self.send_response(200);self.end_headers()
  def log_message(self,*a):pass
 sink=ThreadingHTTPServer(('127.0.0.1',0),Sink)
 class Origin(BaseHTTPRequestHandler):
  def do_GET(self):calls.append('origin');self.send_response(302);self.send_header('Location','http://127.0.0.1:'+str(sink.server_port)+'/');self.end_headers()
  def log_message(self,*a):pass
 origin=ThreadingHTTPServer(('127.0.0.1',0),Origin)
 ts=[threading.Thread(target=x.serve_forever,daemon=True) for x in [origin,sink]]
 for t in ts:t.start()
 try:
  client=s.HttpClient(base_url='http://127.0.0.1:'+str(origin.server_port),api_key='fixture-only',bearer_token=None,oauth_client_secret=None,oauth_token_url=None,oauth_resource=None,client_key='fixture',client_scopes=[],timeout=1);client.safe_transport=True
  with pytest.raises(urllib.error.URLError):client.request('GET','/health')
  assert calls==['origin']
 finally:
  for server in [origin,sink]:server.shutdown();server.server_close()
  for t in ts:t.join()

@pytest.mark.parametrize('method',['get','get_text'])
def test_DIAGNOSTIC_kube_redirect_forwards_fixture_bearer(method):
 import threading
 from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
 captured=[]
 class Sink(BaseHTTPRequestHandler):
  def do_GET(self):captured.append(self.headers.get('Authorization'));self.send_response(200);self.end_headers();self.wfile.write(b'{"items":[]}')
  def log_message(self,*a):pass
 sink=ThreadingHTTPServer(('127.0.0.1',0),Sink)
 class Origin(BaseHTTPRequestHandler):
  def do_GET(self):self.send_response(302);self.send_header('Location','http://127.0.0.1:'+str(sink.server_port)+'/');self.end_headers()
  def log_message(self,*a):pass
 origin=ThreadingHTTPServer(('127.0.0.1',0),Origin)
 ts=[threading.Thread(target=x.serve_forever,daemon=True) for x in [origin,sink]]
 for t in ts:t.start()
 try:
  k=s.KubernetesClient.__new__(s.KubernetesClient);k.base_url='http://127.0.0.1:'+str(origin.server_port);k.token='FIXTURE_ONLY';k.timeout=1;k.context=None
  with pytest.raises(Exception):getattr(k,method)('/resource')
  assert captured==[]
 finally:
  for server in [origin,sink]:server.shutdown();server.server_close()
  for t in ts:t.join()

def test_DIAGNOSTIC_readonly_report_retains_private_http_body(tmp_path,monkeypatch):
 d=doc_for(tmp_path/'store');a=approval(d)
 monkeypatch.setattr(c.Approval,'load',lambda *args,**kw:a)
 class BadHTTP:
  def request(self,*args,**kw):return s.HttpResult(503,{'client_secret':'FIXTURE_PRIVATE_RESPONSE'})
 def pre(*args,**kw):
  report={'target':'fixture','checks':[],'alerts':[]};s.check_api_health(BadHTTP(),report);return report
 monkeypatch.setattr(p,'preflight',pre)
 result=s.build_report(s.build_parser().parse_args([]),kube=object())
 assert result['status']=='failed' and 'FIXTURE_PRIVATE_RESPONSE' not in json.dumps(result)
