"""Actual adapters with fresh, non-sensitive fixtures; no production initialization."""
import copy
import hashlib
import io
import json
import os
import threading
import urllib.error
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
import pytest
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent))
from test_smoke_control import doc_for, approval, NOW, Transport, JOB
from scripts import smoke_control as c, smoke_preflight as p, smoke_transport as t, check_memory_rollout_smoke as s


def fixture(tmp_path):
    d=doc_for(tmp_path/'store')
    d['writer_digest']=d['target_digest'];d['hold_git_revision']='main@sha1:'+'a'*40
    blob=json.dumps({'kind':'HelmRelease','metadata':{'name':d['release_name'],'namespace':d['namespace']},'spec':{'suspend':True,'values':{'image':{'backendDigest':d['target_digest']}}}})
    d['canonical_hold']={'revision':d['hold_git_revision'],'path':'apps/fixture/release.yaml','sha256':hashlib.sha256(blob.encode()).hexdigest(),'blob':blob,'parent_verified':True}
    d['workloads']={k:{'replicas':2,'replicaset_uids':[k+'-owner']} for k in ['backend','mcp']}
    args=s.build_parser().parse_args(['--mode','bounded','--namespace','fixture','--scope-key','rollout-smoke','--expected-sentinel-pods','3','--pod-label-selector','app.kubernetes.io/instance=fixture-release'])
    args.reader_budget=t.Budget()
    report={'checks':[],'alerts':[],'identity':{'tenant_id':'default','auth_mode':'mcp_oauth','credential_id':JOB,'mcp_client_key':'fixture-client','allowed_scopes':['read','write','write:workspace']}}
    resource={'metadata':{'generation':9},'status':{'observedGeneration':9,'conditions':[{'type':'Ready','status':'True','observedGeneration':9}]}}
    ks=copy.deepcopy(resource);ks['status']['lastAppliedRevision']=d['hold_git_revision']
    git=copy.deepcopy(resource);git['status']['artifact']={'revision':d['hold_git_revision']}
    hr={'metadata':{'uid':d['release_uid']},'spec':{'suspend':True,'values':{'image':{'backendDigest':d['target_digest']},'highAvailability':{'replicas':{'backend':2,'mcp':2,'valkeySentinels':3}}}},'status':{'history':[{'ociDigest':d['chart_digest']}]}}
    pods=[{'metadata':{'uid':k+str(i),'labels':{'app.kubernetes.io/instance':d['release_name']},'ownerReferences':[{'kind':'ReplicaSet','controller':True,'uid':k+'-owner'}]},'status':{'containerStatuses':[{'name':k,'ready':True,'imageID':'fixture@'+d['target_digest']}]}} for k in ['backend','mcp'] for i in range(2)]
    worker={'metadata':{'name':'worker-a','uid':'worker-uid','labels':{'app':'worker'}},'status':{'phase':'Running','containerStatuses':[{'name':'worker','containerID':'containerd://fixed','restartCount':108,'ready':True,'lastState':{'terminated':d['baseline']['workers'][0]['last_terminated']},'state':{'running':{'startedAt':'2026-10-12T02:00:00Z'}}}]}}
    class Kube:
        text='2026-10-12T11:01:30Z heartbeat\n2026-10-12T12:01:59Z heartbeat\n'
        def get(self,path,**kw):
            if path.endswith('/pods'):return {'items':pods+[worker]}
            if path.endswith('/kube-system'):return {'metadata':{'uid':d['cluster_uid']}}
            if path.endswith('/fixture'):return {'metadata':{'uid':d['namespace_uid']}}
            if '/helmreleases/' in path:return hr
            if '/kustomizations/' in path:return ks
            if '/gitrepositories/' in path:return git
            raise AssertionError(path)
        def get_text(self,path,**kw):
            assert kw['query']['timestamps']=='true'
            return self.text
    return d,args,report,Kube(),Transport(d),ks,git,hr,pods,worker


def test_actual_runtime_binding_and_temporal_pass(tmp_path,monkeypatch):
    d,args,r,k,client,*_=fixture(tmp_path);monkeypatch.setattr(s,'utc_now',lambda:NOW)
    p.runtime_binding(s,client,r,args,k,approval(d));assert s._check_passed(r,'artifact_binding')
    result=p.temporal_observations(s,args,k,approval(d));assert result['workers'][0]['restart_delta']==0 and result['historical_oom']


@pytest.mark.parametrize('bad',['stale_ks','stale_git','git_notready','ks_notready','git_revision','ks_revision','desired_false','live_false','missing_blob','wrong_owner','other_release','wrong_uid','selector','replicas'])
def test_actual_hold_and_owners_refuse(tmp_path,bad):
    d,args,r,k,client,ks,git,hr,pods,worker=fixture(tmp_path)
    if bad=='stale_ks':ks['status']['conditions'][0]['observedGeneration']=1
    if bad=='stale_git':git['status']['observedGeneration']=1
    if bad=='git_notready':git['status']['conditions'][0]['status']='False'
    if bad=='ks_notready':ks['status']['conditions'][0]['status']='False'
    if bad=='git_revision':git['status']['artifact']['revision']='wrong'
    if bad=='ks_revision':ks['status']['lastAppliedRevision']='wrong'
    if bad=='desired_false':
        b=json.loads(d['canonical_hold']['blob']);b['spec']['suspend']=False;d['canonical_hold']['blob']=json.dumps(b);d['canonical_hold']['sha256']=hashlib.sha256(d['canonical_hold']['blob'].encode()).hexdigest()
    if bad=='live_false':hr['spec']['suspend']=False
    if bad=='missing_blob':d.pop('canonical_hold')
    if bad=='wrong_owner':pods[0]['metadata']['ownerReferences'][0]['uid']='other'
    if bad=='other_release':pods[0]['metadata']['labels']['app.kubernetes.io/instance']='other'
    if bad=='wrong_uid':hr['metadata']['uid']='other'
    if bad=='selector':args.pod_label_selector='app=anything'
    if bad=='replicas':d['workloads']['backend']['replicas']=3
    with pytest.raises(c.ControlError):p.runtime_binding(s,client,r,args,k,approval(d))
    assert not client.posts


@pytest.mark.parametrize('bad',['empty','rotated','untimestamped','future','stale','reversed','missing_start','future_nonoom','undated_nonoom','incoherent','counter'])
def test_actual_temporal_adapter_refuses(tmp_path,monkeypatch,bad):
    d,args,r,k,client,ks,git,hr,pods,w=fixture(tmp_path);monkeypatch.setattr(s,'utc_now',lambda:NOW)
    status=w['status']['containerStatuses'][0]
    if bad=='empty':k.text=''
    if bad=='rotated':k.text='2026-10-12T12:01:59Z heartbeat\n'
    if bad=='untimestamped':k.text='heartbeat\n'
    if bad=='future':k.text+='2026-10-12T12:10:00Z heartbeat\n'
    if bad=='stale':k.text='2026-10-12T11:01:30Z heartbeat\n'
    if bad=='reversed':k.text='2026-10-12T12:01:59Z heartbeat\n2026-10-12T11:01:30Z heartbeat\n'
    if bad=='missing_start':status['state']['running']={}
    if bad in ['future_nonoom','undated_nonoom','incoherent']:
        term={'reason':'Error','exitCode':1,'finishedAt':'2026-10-12T01:00:00Z'}
        if bad=='future_nonoom':term['finishedAt']='2026-10-12T13:00:00Z'
        if bad=='undated_nonoom':term.pop('finishedAt')
        if bad=='incoherent':term['startedAt']='2026-10-12T02:00:00Z'
        status['lastState']['terminated']=term;d['baseline']['workers'][0]['last_terminated']=term;d['baseline_sha256']=c.sha256_json(d['baseline'])
    if bad=='counter':status['restartCount']=109
    with pytest.raises(c.ControlError):p.temporal_observations(s,args,k,approval(d))
    assert not client.posts


@pytest.mark.parametrize('place',['principal','scope','store_binding','worker','payload','metadata'])
def test_nested_fields_and_actual_payload_invalid(tmp_path,place):
    d=doc_for(tmp_path/'store')
    if place in ['principal','scope','store_binding']:d[place]['unused']='fixture'
    if place=='worker':d['baseline']['workers'][0]['unused']='fixture';d['baseline_sha256']=c.sha256_json(d['baseline'])
    if place=='payload':d['payload']['title']=None
    if place=='metadata':d['payload']['metadata']['token']='FIXTURE_ONLY'
    with pytest.raises(c.ControlError):approval(d)


def test_manifest_hardlink_and_swap_race(tmp_path,monkeypatch):
    d=doc_for(tmp_path/'store');path=tmp_path/'manifest';path.write_bytes(c.canonical(d));path.chmod(0o600);os.link(path,tmp_path/'alias')
    with pytest.raises(c.ControlError):c.Approval.load(path,c.sha256_json(d),now=NOW)
    (tmp_path/'alias').unlink();original=c.os.open
    def raced(target,*args,**kwargs):
        if str(target)==str(path):
            path.rename(tmp_path/'old');path.write_bytes(c.canonical(d));path.chmod(0o600)
        return original(target,*args,**kwargs)
    monkeypatch.setattr(c.os,'open',raced)
    with pytest.raises(c.ControlError):c.Approval.load(path,c.sha256_json(d),now=NOW)


@pytest.mark.parametrize('method',['get','get_text','mcp'])
@pytest.mark.parametrize('destination',['same','cross','downgrade'])
def test_all_required_readers_never_forward_redirects(method,destination,tmp_path,monkeypatch):
    captures=[]
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            captures.append((self.path,self.headers.get('Authorization')))
            if self.path=='/sink':self.send_response(200);self.end_headers();self.wfile.write(b'{}');return
            self.send_response(302);self.send_header('Location',location);self.end_headers()
        def log_message(self,format,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);other=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    base='http://127.0.0.1:'+str(server.server_port)
    location=base+'/sink' if destination=='same' else 'http://127.0.0.1:'+str(other.server_port)+'/sink'
    context=None
    if destination=='downgrade':
        import ssl, subprocess
        cert=tmp_path/'fixture-cert.pem';key=tmp_path/'fixture-key.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1'],check=True,capture_output=True)
        tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.load_cert_chain(cert,key)
        server.socket=tls.wrap_socket(server.socket,server_side=True)
        base='https://127.0.0.1:'+str(server.server_port)
        context=ssl.create_default_context(cafile=str(cert))
        if method=='mcp':
            original=t.strict_open
            monkeypatch.setattr(t,'strict_open',lambda request,**kwargs:original(request,context=context,**kwargs))
    threads=[threading.Thread(target=x.serve_forever,daemon=True) for x in [server,other]]
    for thread in threads:thread.start()
    try:
        if method=='mcp':
            report={'target':'fixture','checks':[],'alerts':[]};s.check_mcp_reachable(base+'/mcp',report,timeout=1);assert not s._check_passed(report,'mcp_health')
        else:
            k=s.KubernetesClient.__new__(s.KubernetesClient);k.base_url=base;k.token='FIXTURE_ONLY';k.timeout=1;k.context=context;k.budget=t.Budget()
            with pytest.raises(urllib.error.URLError):getattr(k,method)('/origin')
        assert len(captures)==1 and captures[0][0]!='/sink'
    finally:
        for x in [server,other]:x.shutdown();x.server_close()
        for thread in threads:thread.join()


@pytest.mark.parametrize('status,passed',[(200,True),(401,True),(405,True),(404,False),(500,False)])
def test_explicit_mcp_contract(status,passed,monkeypatch):
    class Response:
        status=200
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,n):return b''
    response=Response();response.status=status
    monkeypatch.setattr(t,'strict_open',lambda *args,**kwargs:response)
    r={'target':'fixture','checks':[],'alerts':[]};s.check_mcp_reachable('https://fixture.invalid/mcp',r,timeout=1)
    assert s._check_passed(r,'mcp_health') is passed


def test_response_budget_and_tls_context(monkeypatch):
    with pytest.raises(c.ControlError):t.Budget(max_bytes=3).read(io.BytesIO(b'1234'))
    clock=[0.];monkeypatch.setattr(t.time,'monotonic',lambda:clock[0]);budget=t.Budget(seconds=1)
    class Slow:
        def read1(self,n):clock[0]+=2;return b'a'
    with pytest.raises(c.ControlError):budget.read(Slow())
    contexts=[]
    original=t.urllib.request.HTTPSHandler
    monkeypatch.setattr(t.urllib.request,'HTTPSHandler',lambda **kwargs:contexts.append(kwargs['context']) or original(**kwargs))
    sentinel=__import__('ssl').create_default_context()
    class Opener:
        def open(self,*args,**kwargs):return None
    monkeypatch.setattr(t.urllib.request,'build_opener',lambda *args:Opener())
    t.strict_open('https://fixture.invalid',timeout=1,context=sentinel)
    assert contexts==[sentinel]
