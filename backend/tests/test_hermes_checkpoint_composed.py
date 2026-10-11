"""R3 real child/HTTP/TLS regressions; R4 composes immutable exact PR550 source."""
import hashlib
import importlib.machinery
import importlib.util
import json
import ssl
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.test_hermes_checkpoint_repair import ready, wire, event

FIXTURES = Path(__file__).parent / 'fixtures/checkpoint_review'
PR550_SHA = 'a368363dedf547da667c7b8eab32f3ecdd1f73ea'
PR550_SOURCE_HASH = '4788e87107f0d16fda519f94f2cbbe3a1641e01e980d52fcad6a608627ffc9cd'


@pytest.fixture(scope='module')
def tls_files(tmp_path_factory):
    path = tmp_path_factory.mktemp('checkpoint-tls')
    cert, key = path / 'cert.pem', path / 'key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                    '-keyout', str(key), '-out', str(cert), '-days', '1',
                    '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1'],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return cert, key


def start_server(handler, tls=None):
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    server.daemon_threads = True
    if tls:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(tls[0]), str(tls[1]))
        server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    return server, thread


def stop_server(server, thread):
    server.shutdown(); server.server_close(); thread.join(timeout=2)
    assert not thread.is_alive()


@pytest.mark.parametrize('code', [301, 302, 303, 307, 308])
@pytest.mark.parametrize('method', ['GET', 'POST'])
@pytest.mark.parametrize('oauth', [False, True])
@pytest.mark.parametrize('downgrade', [False, True])
def test_R3_child_never_redirects_auth_to_cross_origin_or_http_downgrade(code, method, oauth, downgrade, tls_files):
    _, p = ready()
    target_hits, origin_hits = [], []
    class Target(BaseHTTPRequestHandler):
        def do_GET(self):
            target_hits.append(dict(self.headers)); self.send_response(200); self.end_headers()
            self.wfile.write(b'{}')
        do_POST = do_GET
        def log_message(self, format, *args): pass
    target, target_thread = start_server(Target)
    class Origin(BaseHTTPRequestHandler):
        def do_GET(self):
            origin_hits.append((self.command, dict(self.headers)))
            if self.command == 'POST': self.rfile.read(int(self.headers.get('Content-Length', 0)))
            self.send_response(code)
            self.send_header('Location', f'http://127.0.0.1:{target.server_port}/redirect-target')
            self.end_headers()
        do_POST = do_GET
        def log_message(self, format, *args): pass
    origin, origin_thread = start_server(Origin, tls_files if downgrade else None)
    try:
        p._base_url = f'{"https" if downgrade else "http"}://127.0.0.1:{origin.server_port}'
        if downgrade: p._config['checkpoint_ca_file'] = str(tls_files[0])
        if oauth:
            p._oauth_client_secret = 'fixture OAuth binding'
            p._server_identity_loaded = True
            p._server_agent_scope_key = 'barbara'
            p._server_containment_mode = 'hermes_agent'
            p._bearer_token = 'fixture-bearer'
            p._bearer_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        deadline = time.perf_counter() + 2
        snapshot = p._checkpoint_snapshot(deadline)
        with pytest.raises(RuntimeError):
            p._checkpoint_request_json(method, '/request', {'fixture': True} if method == 'POST' else None,
                                       snapshot=snapshot, deadline=deadline)
        # This proves HTTPS handshake/trust worked: rejection was at redirect,
        # not a certificate failure which would hide the security test.
        assert len(origin_hits) == 1
        credential_header = 'Authorization' if oauth else 'X-Api-Key'
        assert any(key.lower() == credential_header.lower() for key in origin_hits[0][1])
        assert target_hits == []
    finally:
        stop_server(origin, origin_thread); stop_server(target, target_thread)


def test_R2_real_slow_drip_child_uses_remaining_total_budget_and_is_reaped(monkeypatch):
    module, p = ready()
    checkpoint_module = sys.modules[module.CheckpointMixin.__module__]
    children = []
    original = subprocess.Popen
    def tracked(*args, **kwargs):
        child = original(*args, **kwargs); children.append(child); return child
    monkeypatch.setattr(checkpoint_module.subprocess, 'Popen', tracked)
    class Slow(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers()
            try:
                for _ in range(100):
                    self.wfile.write(b' '); self.wfile.flush(); time.sleep(.02)
            except (BrokenPipeError, ConnectionResetError): pass
        def log_message(self, format, *args): pass
    server, thread = start_server(Slow)
    try:
        p._base_url = f'http://127.0.0.1:{server.server_port}'
        deadline = time.perf_counter() + .15
        snapshot = p._checkpoint_snapshot(deadline)
        start = time.perf_counter()
        with pytest.raises(RuntimeError):
            p._checkpoint_request_json('GET', '/slow', snapshot=snapshot, deadline=deadline)
        assert time.perf_counter() - start < .4
        assert children and all(child.poll() is not None for child in children)
    finally: stop_server(server, thread)


def composed(*, reviewed=False):
    module, baseline = ready()
    source = FIXTURES / ('pr550-provider.py.txt' if reviewed else 'pr550-session-quota.py.txt')
    expected = PR550_SOURCE_HASH if reviewed else '72c39468b6e05738d1a046359bb8ed39bf247b39e9fdae0b190ddc27351ccc74'
    assert hashlib.sha256(source.read_bytes()).hexdigest() == expected
    name = 'exact_pr550_lifecycle_fixture'
    spec = importlib.util.spec_from_loader(name, importlib.machinery.SourceFileLoader(name, str(source)))
    assert spec is not None and spec.loader is not None
    native = importlib.util.module_from_spec(spec)
    sys.modules[name] = native
    spec.loader.exec_module(native)
    class Composed(module.CheckpointMixin, native.PalaceOfTruthMemoryProvider):
        # Minimal integration: one base and one initialization call. Do not
        # override sibling policy, queue, flush, shutdown or snapshot helpers.
        def __init__(self):
            super().__init__(); self._init_checkpoint()
    p = Composed()
    for field in ('_config', '_base_url', '_api_key', '_session_id', '_tenant_id', '_scope_type', '_scope_key'):
        setattr(p, field, getattr(baseline, field))
    return p


def test_R4_exact_sibling_cron_explicit_allowed_checkpoint_auto_blocked():
    p = composed(); calls, _ = wire(p)
    p._platform = 'cron'; p._session_id = 'cron_abc'
    p._writes_disabled = False; p._lifecycle_writes_disabled = True
    explicit = []
    p._request_json = lambda *args, **kwargs: explicit.append(args) or {'job_id': 'fixture'}
    result = json.loads(p.handle_tool_call('palace_remember', {'content': 'operator-requested memory', 'target': 'memory'}))
    assert result['ok'] is True
    assert len(explicit) == 1
    with pytest.raises(RuntimeError): p.on_pre_compress([event()], require_checkpoint=True)
    assert not calls
    p.shutdown(timeout=1)
    assert p.shutdown_drain_state['status'] == 'drained'


def test_R4_exact_sibling_active_checkpoint_flush_and_shutdown_are_accounted():
    p = composed(); wire(p)
    original = p._checkpoint_request_json
    entered, release = threading.Event(), threading.Event()
    def blocked(method, path, payload=None, **kwargs):
        if method == 'GET':
            entered.set(); assert release.wait(2)
        return original(method, path, payload, **kwargs)
    p._checkpoint_request_json = blocked
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(p.on_pre_compress, [event()], require_checkpoint=True)
        try:
            assert entered.wait(1)
            assert p._active_writes == 1
            assert p.flush_pending(timeout=.01) is False
            p.shutdown(timeout=.01)
            assert p.shutdown_drain_state['status'] == 'timed_out'
            assert p._active_writes == 1
        finally: release.set()
        assert 'durable' in future.result(timeout=2)
    assert p.flush_pending(timeout=1) is True
    assert p._completed_writes == 1 and p._failed_writes == 0
    p.shutdown(timeout=1)
    assert p.shutdown_drain_state['status'] == 'drained'
    with pytest.raises(RuntimeError): p.on_pre_compress([event('later')], require_checkpoint=True)


def test_R4_exact_sibling_checkpoint_failure_is_a_native_failed_write():
    p = composed()
    p._checkpoint_request_json = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('fixture offline'))
    with pytest.raises(RuntimeError): p.on_pre_compress([event()], require_checkpoint=True)
    assert p.flush_pending(timeout=1) is True  # Barrier drained, not remote success.
    assert p._failed_writes == 1 and p._completed_writes == 0
    assert p.shutdown_drain_state['status'] == 'failed'


def test_R4_exact_sibling_expired_pending_checkpoint_never_writes_later(monkeypatch):
    p = composed(); calls, _ = wire(p)
    blocker_entered, blocker_release = threading.Event(), threading.Event()
    def blocker():
        blocker_entered.set(); assert blocker_release.wait(2)
    with p._lifecycle_lock:
        p._queue_write(blocker, p._write_quota)
    assert blocker_entered.wait(1)
    monkeypatch.setattr(type(p), 'CHECKPOINT_TOTAL_SECONDS', .05)
    try:
        with pytest.raises(RuntimeError): p.on_pre_compress([event()], require_checkpoint=True)
        assert calls == []
    finally: blocker_release.set()
    p.flush_pending(timeout=1)
    assert calls == []


def test_R2_ready_identity_lock_contended_is_bounded(monkeypatch):
    _, p = ready(); calls, _ = wire(p)
    monkeypatch.setattr(type(p), 'CHECKPOINT_TOTAL_SECONDS', .05)
    p._server_identity_lock.acquire()
    try:
        start = time.perf_counter()
        with pytest.raises(RuntimeError): p.on_pre_compress([event()], require_checkpoint=True)
        assert time.perf_counter() - start < .3
    finally: p._server_identity_lock.release()
    assert not calls
