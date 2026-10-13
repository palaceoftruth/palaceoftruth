import copy,json,sqlite3,time,threading,sys
from pathlib import Path
from datetime import timedelta
import pytest
sys.path.insert(0,str(Path(__file__).parent))
from test_smoke_control import doc_for,approval,Transport,gates,NOW
from test_smoke_r1_r5_adapters import fixture
from scripts import smoke_control as c,smoke_preflight as p,smoke_transport as t,check_memory_rollout_smoke as s

@pytest.mark.parametrize('lost',['row','inventory','fence','pair','replace_inventory'])
def test_partial_history_loss_never_replenishes(tmp_path,lost):
 root=tmp_path/'store';d=doc_for(root);a=approval(d);j=c.Journal(root,a,host_id='fixture-host');client=Transport(d);c.run_reserved(a,j,client,gates,gates,now=NOW)
 if lost in ['row','pair']:
  with sqlite3.connect(root/'journal.sqlite3') as db:db.execute('DELETE FROM slots')
 if lost in ['fence','pair']:(root/'reservations'/(a.slot+'.json')).unlink()
 if lost=='inventory':(root/'consumed.json').write_text('{}')
 if lost=='replace_inventory':
  (root/'consumed.json').rename(root/'old-inventory');(root/'consumed.json').write_text('{}');(root/'consumed.json').chmod(0o600)
 with pytest.raises(c.ControlError):c.run_reserved(a,c.Journal(root,a,host_id='fixture-host'),client,gates,gates,now=NOW)
 assert len(client.posts)==1

@pytest.mark.parametrize('bad',['wrong_owner_stale','wrong_blob_hash','external_marker_missing','reader_exception'])
def test_combined_control_failures_never_pass(tmp_path,bad):
 d,args,r,k,client,ks,git,hr,pods,w=fixture(tmp_path)
 if bad=='wrong_owner_stale':pods[0]['metadata']['ownerReferences'][0]['uid']='wrong';ks['status']['observedGeneration']=0
 if bad=='wrong_blob_hash':d['canonical_hold']['sha256']='f'*64
 if bad=='external_marker_missing':d['canonical_hold']['parent_verified']=False
 if bad=='reader_exception':
  def denied(*a,**kw):raise PermissionError('FIXTURE_PRIVATE_ERROR')
  k.get=denied
 with pytest.raises((c.ControlError,PermissionError)):p.runtime_binding(s,client,r,args,k,approval(d))
 assert not client.posts

def test_recent_oom_with_real_log_interval_refuses(tmp_path,monkeypatch):
 d,args,r,k,client,ks,git,hr,pods,w=fixture(tmp_path);monkeypatch.setattr(s,'utc_now',lambda:NOW)
 w['status']['containerStatuses'][0]['lastState']['terminated']['finishedAt']=(NOW-timedelta(seconds=5)).isoformat()
 with pytest.raises(c.ControlError):p.temporal_observations(s,args,k,approval(d))
 assert not client.posts

def test_safe_report_excludes_nested_private_reader_errors():
 report={'status':'failed','checks':[{'name':'api_health','status':'failed','response':{'secret':'FIXTURE_PRIVATE'}}],'alerts':[{'code':'arbitrary','message':'FIXTURE_PRIVATE','detail':{'token':'FIXTURE_PRIVATE'}}]}
 result=s.safe_final_report(report);assert 'FIXTURE_PRIVATE' not in json.dumps(result)

def test_observed_source_generation_race_refuses(tmp_path):
 d,args,r,k,client,ks,git,hr,pods,w=fixture(tmp_path)
 original=k.get
 def raced(path,**kw):
  if '/gitrepositories/' in path:
   git['metadata']['generation']+=1
  return original(path,**kw)
 k.get=raced
 with pytest.raises(c.ControlError):p.runtime_binding(s,client,r,args,k,approval(d))
 assert not client.posts

# Diagnostic: socket inactivity timeout is not a total response-header deadline.
def test_DIAGNOSTIC_response_headers_exceed_total_deadline(record_property):
 from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
 import urllib.request
 class Handler(BaseHTTPRequestHandler):
  def do_GET(self):
   try:
    # Every byte arrives before the socket inactivity timeout, but headers take > deadline.
    for b in b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n':
     self.connection.sendall(bytes([b]));time.sleep(.015)
   except (BrokenPipeError,ConnectionResetError):pass
  def log_message(self,*a):pass
 server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
 try:
  budget=t.Budget(seconds=.12);start=time.monotonic()
  report={'target':'fixture','checks':[],'alerts':[]}
  s.check_mcp_reachable('http://127.0.0.1:'+str(server.server_port)+'/mcp',report,timeout=1,budget=budget)
  elapsed=time.monotonic()-start
  assert not s._check_passed(report,'mcp_health')
  record_property('configured_deadline_seconds',.12)
  record_property('observed_elapsed_seconds',elapsed)
  assert elapsed <= .12 + .20
 finally:server.shutdown();server.server_close();thread.join()

@pytest.mark.parametrize('field',['api_key','private_key','credentialValue'])
def test_DIAGNOSTIC_unused_credential_metadata_persists(tmp_path,field):
 root=tmp_path/'store';d=doc_for(root);d['payload']['metadata'][field]='FIXTURE_ONLY_CREDENTIAL'
 with pytest.raises(c.ControlError):approval(d)
 with sqlite3.connect(root/'journal.sqlite3') as db:assert db.execute('SELECT count(*) FROM slots').fetchone()[0]==0
 assert not any((root/'reservations').iterdir())
 assert 'FIXTURE_ONLY_CREDENTIAL' not in (root/'journal.sqlite3').read_bytes().decode('latin1')
