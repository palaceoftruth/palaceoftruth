"""Residual R4b/R5b fixtures only: loopback exchanges and private local stores."""
import json
import os
import sqlite3
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import pytest
sys.path.insert(0,str(Path(__file__).parent))
from test_smoke_control import doc_for, approval, gates, NOW, Transport
from scripts import smoke_control as c, smoke_transport as t, check_memory_rollout_smoke as s


@pytest.mark.parametrize('field',['api_key','api-key','apiKey','APIKey','private_key','private-key','privateKey','PrivateKey','credential_value','credential-value','credentialValue','CredentialValue'])
@pytest.mark.parametrize('nesting',['direct','dict','list'])
def test_credential_spellings_refuse_before_reservation(tmp_path,field,nesting):
    root=tmp_path/'store';d=doc_for(root);marker='FIXTURE_RESIDUAL_NOT_REAL'
    obj={field:marker}
    if nesting=='dict':obj={'outer':obj}
    if nesting=='list':obj={'outer':[{'inner':obj}]}
    d['payload']['metadata'].update(obj)
    with pytest.raises(c.ControlError):approval(d)
    with sqlite3.connect(root/'journal.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM slots').fetchone()[0]==0
    assert not any((root/'reservations').iterdir())
    assert marker.encode() not in (root/'journal.sqlite3').read_bytes()
    report=s.safe_final_report({'status':'failed','checks':[],'alerts':[{'code':'control_approval_failed','response':obj}]})
    assert marker not in json.dumps(report)


def test_noncredential_literal_values_are_not_normalized_or_prose_scanned(tmp_path):
    d=doc_for(tmp_path/'store');body='Literal api_key in approved non-sensitive prose is not a structured field.'
    d['payload']['body']=body
    d['payload']['metadata']={'campaign':'literal-CaSe-ID_1'}
    a=approval(d)
    assert a.payload['body']==body and a.payload['metadata']==d['payload']['metadata']


def _counting_server(handler,counters):
    class Server(ThreadingHTTPServer):
        def get_request(self):
            connection,address=super().get_request()
            counters['connections']+=1
            return connection,address
    return Server(('127.0.0.1',0),handler)


@pytest.mark.parametrize('phase',['headers','body'])
@pytest.mark.parametrize('reader',['api','kube_json','kube_text','mcp'])
def test_exchange_drip_cancelled_and_child_reaped(phase,reader,monkeypatch,record_property):
    from scripts.smoke_transport import ControlError
    pids=[];original=t.os.fork
    def tracked_fork():
        pid=original()
        if pid:pids.append(pid)
        return pid
    monkeypatch.setattr(t.os,'fork',tracked_fork)
    counters={'connections':0,'requests':0}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            counters['requests']+=1
            try:
                if phase=='headers':data=b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n'
                else:
                    self.connection.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n');data=b'x'*100
                for byte in data:self.connection.sendall(bytes([byte]));time.sleep(.015)
            except (BrokenPipeError,ConnectionResetError):pass
        def log_message(self,format,*args):pass
    server=_counting_server(Handler,counters);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base='http://127.0.0.1:'+str(server.server_port);budget=t.Budget(seconds=.12)
    try:
        start=time.monotonic()
        if reader=='mcp':
            report={'target':'fixture','checks':[],'alerts':[]};s.check_mcp_reachable(base+'/mcp',report,timeout=1,budget=budget)
            assert not s._check_passed(report,'mcp_health')
        elif reader=='api':
            client=s.HttpClient(base_url=base,api_key='FIXTURE_ONLY',bearer_token=None,oauth_client_secret=None,oauth_token_url=None,oauth_resource=None,client_key='fixture',client_scopes=[],timeout=1)
            client.safe_transport=True;client.budget=budget
            with pytest.raises(ControlError):client.request('GET','/health')
        else:
            k=s.KubernetesClient.__new__(s.KubernetesClient);k.base_url=base;k.token='FIXTURE_ONLY';k.timeout=1;k.context=None;k.budget=budget
            method=k.get if reader=='kube_json' else k.get_text
            with pytest.raises(ControlError):method('/resource')
        elapsed=time.monotonic()-start
        record_property('configured_deadline_seconds',.12);record_property('elapsed_seconds',elapsed)
        record_property('forked_children',len(pids))
        assert elapsed<=.12+.20
        if pids:
            # A supervised child was established and MUST be reaped.
            assert len(pids)==1
            with pytest.raises(ChildProcessError):os.waitpid(pids[0],os.WNOHANG)
        else:
            # Zero children is the fail-closed pre-flight refusal: the total deadline
            # had already elapsed at attempt time and no network I/O ran (no connect,
            # no request). This is the bounded refusal, not a forked child.
            assert budget.deadline<=time.monotonic()
            assert counters['connections']==0 and counters['requests']==0
    finally:server.shutdown();server.server_close();thread.join()


@pytest.mark.parametrize('reader',['api','kube_json'])
def test_exchange_expired_deadline_refuses_without_network_io(reader,monkeypatch,tmp_path,record_property):
    # Deterministic counterpart to the scheduling jitter in the drip test: the shared
    # total deadline has already elapsed when the exchange is attempted. The runtime
    # guard must fail closed BEFORE any fork or socket: zero children, zero
    # connections, zero requests, and the child-side opener is never reached.
    from scripts.smoke_transport import ControlError
    sentinel=tmp_path/'opener-open-reached'
    pids=[];original=t.os.fork
    def tracked_fork():
        pid=original()
        if pid:pids.append(pid)
        return pid
    monkeypatch.setattr(t.os,'fork',tracked_fork)
    real_open=urllib.request.OpenerDirector.open
    def spied_open(self,*args,**kwargs):
        sentinel.write_text('reached')
        return real_open(self,*args,**kwargs)
    monkeypatch.setattr(urllib.request.OpenerDirector,'open',spied_open)
    counters={'connections':0,'requests':0}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            counters['requests']+=1
            self.send_response(200);self.send_header('Content-Length','0');self.end_headers()
        def log_message(self,format,*args):pass
    server=_counting_server(Handler,counters);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base='http://127.0.0.1:'+str(server.server_port)
    try:
        budget=t.Budget(seconds=.12);budget.deadline=time.monotonic()-1
        if reader=='api':
            client=s.HttpClient(base_url=base,api_key='FIXTURE_ONLY',bearer_token=None,oauth_client_secret=None,oauth_token_url=None,oauth_resource=None,client_key='fixture',client_scopes=[],timeout=1)
            client.safe_transport=True;client.budget=budget
            with pytest.raises(ControlError):client.request('GET','/health')
        else:
            k=s.KubernetesClient.__new__(s.KubernetesClient);k.base_url=base;k.token='FIXTURE_ONLY';k.timeout=1;k.context=None;k.budget=budget
            with pytest.raises(ControlError):k.get('/resource')
        record_property('forked_children',len(pids))
        record_property('server_connections',counters['connections'])
        record_property('server_requests',counters['requests'])
        assert len(pids)==0
        assert counters['connections']==0 and counters['requests']==0
        assert not sentinel.exists()
    finally:server.shutdown();server.server_close();thread.join()


def test_exchange_live_deadline_forks_and_delivers_fixture_post(monkeypatch,record_property):
    # Positive control: with a live total deadline the cancellable exchange really
    # forks exactly one supervised child, delivers the request over IPC, and the
    # loopback peer receives the fixture-only body. Proves the expiry detector is not
    # vacuous and that no real credential ever crosses the wire.
    pids=[];original=t.os.fork
    def tracked_fork():
        pid=original()
        if pid:pids.append(pid)
        return pid
    monkeypatch.setattr(t.os,'fork',tracked_fork)
    posts=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            posts.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200);self.send_header('Content-Length','2');self.end_headers();self.wfile.write(b'{}')
        def log_message(self,format,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base='http://127.0.0.1:'+str(server.server_port)
    try:
        budget=t.Budget(seconds=5)
        client=s.HttpClient(base_url=base,api_key='FIXTURE_ONLY',bearer_token=None,oauth_client_secret=None,oauth_token_url=None,oauth_resource=None,client_key='fixture',client_scopes=[],timeout=5)
        client.safe_transport=True;client.budget=budget
        body={'fixture_marker':'FIXTURE_ONLY','scopes':['read']}
        result=client.request('POST','/entries',body=body)
        record_property('forked_children',len(pids))
        assert result.status==200
        assert len(pids)==1
        assert posts==[body]
        rendered=json.dumps(posts[0])
        assert 'FIXTURE_ONLY' in rendered
        for secret in ('api_key','private_key','credentialValue','password'):assert secret not in rendered
        with pytest.raises(ChildProcessError):os.waitpid(pids[0],os.WNOHANG)
    finally:server.shutdown();server.server_close();thread.join()


def test_interrupted_post_remains_consumed_and_recovery_is_readonly(tmp_path):
    d=doc_for(tmp_path/'store');a=approval(d);journal=c.Journal(tmp_path/'store',a,host_id='fixture-host');posts=[];reads=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            posts.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            try:
                for byte in b'HTTP/1.1 202 Accepted\r\nContent-Length: 0\r\n\r\n':self.connection.sendall(bytes([byte]));time.sleep(.015)
            except (BrokenPipeError,ConnectionResetError):pass
        def log_message(self,format,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    class Client(Transport):
        def request(self,method,path,**kwargs):
            if method=='POST':
                request=urllib.request.Request('http://127.0.0.1:'+str(server.server_port)+'/entries',data=c.canonical(kwargs['body']),method='POST')
                with t.strict_open(request,timeout=1,budget=t.Budget(seconds=.12)) as response:return s.HttpResult(response.status,{})
            reads.append(path);self.record=bool(posts)
            return super().request(method,path,**kwargs)
    client=Client(d)
    try:
        with pytest.raises(c.ControlError):c.run_reserved(a,journal,client,gates,gates,now=NOW)
        assert len(posts)==1 and posts[0]==a.payload
        assert journal.inspect(a.slot)['state']=='reserved'
        result=c.run_reserved(a,journal,client,gates,gates,now=NOW)
        assert result['receipt']['durable'] is True and len(posts)==1
        assert reads and all(p.startswith('/memory/') for p in reads)
    finally:server.shutdown();server.server_close();thread.join()


def test_process_buffering_preserves_aggregate_byte_accounting():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200);self.send_header('Content-Length','4');self.end_headers();self.wfile.write(b'1234')
        def log_message(self,format,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    request=urllib.request.Request('http://127.0.0.1:'+str(server.server_port)+'/')
    try:
        budget=t.Budget(seconds=1,max_bytes=10)
        with t.strict_open(request,timeout=1,budget=budget) as response:
            assert budget.remaining==6
            assert budget.read(response)==b'1234' and budget.remaining==6
        with t.strict_open(request,timeout=1,budget=budget) as response:
            assert budget.read(response)==b'1234' and budget.remaining==2
        with pytest.raises(Exception):t.strict_open(request,timeout=1,budget=budget)
        assert budget.remaining<=0
    finally:server.shutdown();server.server_close();thread.join()


def test_alert_codes_survive_sanitization():
    """A failing rollout must name its cause, not one opaque preflight code."""
    report = s.safe_final_report(
        {
            "status": "failed",
            "checks": [],
            "alerts": [
                {"code": "worker_restart_spike"},
                {"code": "worker_pod_not_running"},
                {"code": "master_not_found"},
                {"code": "../../etc/passwd"},
            ],
        }
    )

    codes = [alert["code"] for alert in report["alerts"]]
    assert codes == [
        "worker_restart_spike",
        "worker_pod_not_running",
        "master_not_found",
        "required_preflight_exception",
    ]
