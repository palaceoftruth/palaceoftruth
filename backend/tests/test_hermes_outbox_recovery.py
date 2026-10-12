"""Process-crash and real TLS transport tests; no production services or host patches."""
import copy
import hashlib
import json
import os
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from tests.test_hermes_outbox import setup, admit, Backend, TENANT, JOB, ITEM, ROOT, fixture_identity


def test_crash_before_sqlite_commit_does_not_ack_or_charge(tmp_path):
    m, p, binding = setup(tmp_path); store = admit(p)
    payload = copy.deepcopy(store.records()[0]['payload']); payload['idempotency_key'] = 'e' * 64
    script = '''import sqlite3,sys,os
con=sqlite3.connect(sys.argv[1]);con.execute('BEGIN IMMEDIATE')
con.execute("INSERT INTO records(key,payload,sha,semantic_sha,session) VALUES (?,?,?,?,?)",('e'*64,sys.argv[2],'bad','bad','session-fixture'))
os._exit(74)
'''
    result = subprocess.run([sys.executable, '-c', script, str(store.path), json.dumps(payload)], timeout=5)
    assert result.returncode == 74
    reopened = m.OutboxStore(tmp_path/'outbox', binding)
    assert len(reopened.records()) == 1
    assert reopened.admit(payload, max_session=2)['local_durable'] is True
    assert len(reopened.records()) == 2


def test_crash_after_remote_acceptance_replays_same_key_exactly_once_effect(tmp_path):
    m, p, binding = setup(tmp_path); store = admit(p)
    backend_marker = tmp_path / 'mock-backend.json'
    script = r'''
import sys,importlib.util,json,os
spec=importlib.util.spec_from_file_location('outbox_fixture',sys.argv[1]);m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)
b=json.loads(sys.argv[3]);s=m.OutboxStore(sys.argv[2],b)
class Backend:
 def call(self,method,path,payload=None,*,deadline):
  if path.endswith('/whoami'):return {'tenant_id':b['tenant_id'],'auth_mode':'api_key','credential_id':b['credential']['id'],'agent_scope_key':None,'allowed_scopes':['read','write','write:agent']}
  if method=='POST':
   with open(sys.argv[4],'w') as f:json.dump({'keys':[payload['idempotency_key']],'calls':1},f);f.flush();os.fsync(f.fileno())
   os._exit(75)
m.RecoveryWorker(s,Backend()).run(max_jobs=1,timeout=1)
'''
    result = subprocess.run([sys.executable, '-c', script, str(ROOT/'third_party_plugins/hermes/memory/palaceoftruth/outbox.py'),
        str(tmp_path/'outbox'), json.dumps(binding), str(backend_marker)], timeout=5)
    assert result.returncode == 75 and store.records()[0]['state'] == 'pending'
    class ReplayedBackend(Backend):
        def call(self, method, path, payload=None, *, deadline):
            if method == 'POST':
                data = json.loads(backend_marker.read_text()); data['calls'] += 1
                if payload['idempotency_key'] not in data['keys']: data['keys'].append(payload['idempotency_key'])
                backend_marker.write_text(json.dumps(data))
            return super().call(method, path, payload, deadline=deadline)
    assert m.RecoveryWorker(store, ReplayedBackend(binding)).run(max_jobs=1, timeout=1)['remote_durable'] == 1
    data = json.loads(backend_marker.read_text())
    assert data['calls'] == 2 and len(data['keys']) == 1
    assert p._session_write_count == 1


def test_worker_lock_is_exclusive_and_crash_releases_it(tmp_path):
    m, p, binding = setup(tmp_path); store = admit(p)
    script = r'''
import sys,importlib.util,json,os,time
spec=importlib.util.spec_from_file_location('outbox_fixture',sys.argv[1]);m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)
s=m.OutboxStore(sys.argv[2],json.loads(sys.argv[3]))
with s.worker_lock():
 open(sys.argv[4],'w').write('locked')
 time.sleep(5)
'''
    marker = tmp_path/'locked'
    proc = subprocess.Popen([sys.executable,'-c',script,str(ROOT/'third_party_plugins/hermes/memory/palaceoftruth/outbox.py'),str(tmp_path/'outbox'),json.dumps(binding),str(marker)])
    try:
        end = time.perf_counter()+2
        while not marker.exists() and time.perf_counter()<end: time.sleep(.005)
        assert marker.exists()
        with pytest.raises(m.OutboxError): m.RecoveryWorker(store, Backend(binding)).run(max_jobs=1, timeout=1)
    finally: proc.kill(); proc.wait(timeout=2)
    assert m.RecoveryWorker(store, Backend(binding)).run(max_jobs=1, timeout=1)['remote_durable'] == 1


@contextmanager
def server(tmp_path, *, redirect=None, slow=False):
    cert, key = tmp_path/'cert.pem', tmp_path/'key.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),
                    '-days','1','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost'],
                   check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    calls, payloads = [], []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def respond(self, value):
            data=json.dumps(value).encode();self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers()
            try:self.wfile.write(data)
            except (BrokenPipeError,ssl.SSLError):pass
        def do_GET(self):
            calls.append((self.command,self.path,self.headers.get('X-API-Key'),self.headers.get('X-Palace-Agent-Scope-Key')))
            if redirect:
                self.send_response(302);self.send_header('Location',redirect);self.end_headers();return
            if slow: time.sleep(.3)
            if self.path.endswith('/whoami'):
                return self.respond(fixture_identity({'tenant_id':TENANT,'credential':{'mode':'api_key','id':'44444444-4444-4444-8444-444444444444'},'scope':{'type':'agent','key':'barbara'}}))
            p=payloads[-1]
            self.respond({'durable':True,'tenant_id':TENANT,'scope':p['scope'],'job_id':JOB,'source_item_id':ITEM,
                          'idempotency_key':p['idempotency_key'],'body_sha256':hashlib.sha256(p['body'].encode()).hexdigest()})
        def do_POST(self):
            calls.append((self.command,self.path,self.headers.get('X-API-Key'),self.headers.get('X-Palace-Agent-Scope-Key')))
            payloads.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.respond({'job_id':JOB,'source_item_id':ITEM})
    http=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain(cert,key);http.socket=ctx.wrap_socket(http.socket,server_side=True)
    thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
    try:yield 'https://localhost:'+str(http.server_port),cert,calls,payloads
    finally:http.shutdown();http.server_close();thread.join(2)


def cli(store, binding, binding_file, cert, *, timeout='1', credential='external-fixture-secret'):
    binding_file.write_text(json.dumps(binding))
    env={'PATH':os.environ['PATH'],'PALACE_OUTBOX_API_KEY':credential}
    return subprocess.run([sys.executable,str(ROOT/'scripts/recover_hermes_outbox.py'),'--directory',str(store.directory),
        '--binding-file',str(binding_file),'--ca-file',str(cert),'--max-jobs','1','--timeout',timeout],
        env=env,text=True,capture_output=True,timeout=5)


def test_external_cli_real_tls_committed_readback_no_secrets_at_rest(tmp_path):
    with server(tmp_path) as (origin,cert,calls,payloads):
        m,p,binding=setup(tmp_path);binding['origin']=origin;p._base_url=origin
        p._config['outbox_binding']=binding
        store=admit(p)
        result=cli(store,binding,tmp_path/'binding.json',cert)
        assert result.returncode==0,result.stdout+result.stderr
        assert json.loads(result.stdout)['remote_durable']==1
        assert len(payloads)==1 and len(calls)==3
        assert all(c[2]=='external-fixture-secret' and c[3]=='barbara' for c in calls)
        assert store.records()[0]['state']=='delivered'
        assert b'external-fixture-secret' not in store.path.read_bytes()
        assert 'external-fixture-secret' not in result.stdout+result.stderr
        again=cli(store,binding,tmp_path/'binding.json',cert)
        assert again.returncode==0 and len(calls)==3


def test_redirect_refused_before_credential_forwarding_and_pending_survives(tmp_path):
    hits=[]
    class Target(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_GET(self):hits.append(self.path);self.send_response(200);self.end_headers()
    target=ThreadingHTTPServer(('127.0.0.1',0),Target);thread=threading.Thread(target=target.serve_forever,daemon=True);thread.start()
    try:
        with server(tmp_path,redirect='http://127.0.0.1:'+str(target.server_port)) as (origin,cert,calls,payloads):
            m,p,binding=setup(tmp_path);binding['origin']=origin;p._base_url=origin;p._config['outbox_binding']=binding
            store=admit(p);result=cli(store,binding,tmp_path/'binding.json',cert)
            assert result.returncode==2 and not hits and not payloads
            assert store.records()[0]['state']=='pending'
    finally:target.shutdown();target.server_close();thread.join(2)


def test_external_network_deadline_is_bounded_pending_not_discarded(tmp_path):
    with server(tmp_path,slow=True) as (origin,cert,calls,payloads):
        m,p,binding=setup(tmp_path);binding['origin']=origin;p._base_url=origin;p._config['outbox_binding']=binding
        store=admit(p);start=time.perf_counter();result=cli(store,binding,tmp_path/'binding.json',cert,timeout='.04')
        assert result.returncode==2 and time.perf_counter()-start<1
        assert store.records()[0]['state']=='pending' and not payloads


@pytest.mark.skipif(not os.environ.get('SAR1406_NORMAL_LOADER'),reason='requires genuine official normal loader')
def test_real_official_host_submission_gap_is_observed_not_repaired(tmp_path):
    m,p,binding=setup(tmp_path)
    from agent.memory_manager import MemoryManager
    entered,release=threading.Event(),threading.Event()
    class Barrier(type(p)):
        def queue_prefetch(self,query,*,session_id=''):
            entered.set();assert release.wait(2)
    m,p,binding=setup(tmp_path,provider_factory=Barrier)
    manager=MemoryManager();manager.add_provider(p)
    try:
        manager.queue_prefetch_all('offline gate',session_id=p._session_id);assert entered.wait(1)
        manager.sync_all('queued before provider','answer',session_id=p._session_id,turn_author={'id':'alice','is_bot':False})
        assert p._outbox_store is None and not (tmp_path/'outbox').exists()
        assert p.flush_pending(timeout=.02) is True  # No visibility into host executor queue.
        assert manager.flush_pending(timeout=.02) is False
        release.set();assert manager.flush_pending(timeout=2)
        assert p.flush_pending(timeout=1)
        assert p.last_outbox_receipt['local_durable'] is True
        assert p._outbox_store.records()[0]['state']=='pending'
    finally:release.set();manager.shutdown_all()
