"""Acceptance regressions for independent review at exact 9631120 (R1-R5).

Original defect-asserting script is preserved verbatim in fixtures/checkpoint_review.
These assert the repaired behavior, including actual HTTP model and backend replay.
No database, Redis, provider initialization, OAuth endpoint or production API is used.
"""
import hashlib
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api import memory as memory_api
from app.auth import verify_memory_auth
from app.database import get_db
from app.models.item import Item
from app.models.job import Job
from app.models.palace import MemoryEntry
from app.services import memory as memory_service
from app.services.checkpoint_receipt import read_checkpoint_receipt
from tests.test_hermes_memory_plugin import load_palaceoftruth_plugin

JOB = '00000000-0000-4000-8000-000000000001'
ITEM = '00000000-0000-4000-8000-000000000002'
STAMP = '2026-10-10T12:00:00+00:00'  # Fixture source event time, not a capture time.


def event(uid='event-1', text='source evidence', role='user', **extra):
    return {'role': role, 'content': text, 'message_uid': uid, 'timestamp': STAMP, **extra}


def ready():
    module = load_palaceoftruth_plugin()
    p = module.PalaceOfTruthMemoryProvider()
    p._config = {'precompression_checkpoint_enabled': True, 'agent_name': 'barbara'}
    p._base_url = 'https://fixture.invalid'
    p._api_key = 'fixture-not-a-secret'
    p._session_id = 'session-fixture'
    p._tenant_id = 'tenant-fixture'
    p._scope_type = 'agent'
    p._scope_key = 'barbara'
    return module, p


def wire(p):
    calls = []
    payloads = []
    def request(method, path, payload=None, **kwargs):
        calls.append((method, path, kwargs))
        if method == 'POST':
            payloads.append(payload)
            return {'job_id': JOB, 'source_item_id': ITEM}
        last = payloads[-1]
        return {'durable': True, 'tenant_id': last['tenant_id'], 'scope': last['scope'],
                'job_id': JOB, 'source_item_id': ITEM, 'idempotency_key': last['idempotency_key'],
                'body_sha256': hashlib.sha256(last['body'].encode()).hexdigest()}
    p._checkpoint_request_json = request
    return calls, payloads


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


def test_R1_real_fastapi_post_and_backend_replay(monkeypatch):
    _, p = ready()
    store = CommittedStore()
    app = FastAPI()
    app.include_router(memory_api.router, prefix='/api/v1')
    @app.middleware('http')
    async def principal(request, call_next):
        request.state.tenant_id = 'tenant-fixture'
        request.state.auth_mode = 'api_key'
        request.state.mcp_allowed_scopes = ['read', 'write', 'write:agent']
        return await call_next(request)
    class FreshReader:
        async def get(self, model, ident, **kwargs):
            return store.committed.get((model, ident))
    async def db(request: Request):
        yield store if request.method == 'POST' else FreshReader()
    app.dependency_overrides[get_db] = db
    async def fixture_auth(request: Request):
        return 'fixture authenticated principal'
    app.dependency_overrides[verify_memory_auth] = fixture_auth
    queue = SimpleNamespace(state='unavailable', available=False, warnings=[])
    async def queue_hint(request): return queue
    async def enqueued(*args, **kwargs): return None
    monkeypatch.setattr(memory_api, '_memory_queue_contract_hint', queue_hint)
    monkeypatch.setattr(memory_api, '_enqueue_memory_job_or_raise', enqueued)
    # Queue response formatting is outside the archive boundary. Keep its schema real.
    from app.schemas.memory import MemoryQueueHint
    queue = MemoryQueueHint(state='unknown')
    calls = []
    with TestClient(app) as client:
        def request(method, path, payload=None, **kwargs):
            response = client.request(method, path, json=payload if payload is not None else None,
                                      headers=dict(kwargs['snapshot'].headers))
            calls.append((method, response.status_code, response.json()))
            assert response.status_code < 300, response.text
            return response.json()
        p._checkpoint_request_json = request
        first = p.on_pre_compress([event()], require_checkpoint=True)
        second = p.on_pre_compress([event()], require_checkpoint=True)
    assert first == second
    assert [status for method, status, _ in calls if method == 'POST'] == [202, 202]
    assert len([row for (kind, _), row in store.committed.items() if kind is Item]) == 1
    entry = next(row for (kind, _), row in store.committed.items() if kind is MemoryEntry)
    assert entry.created_at == datetime.fromisoformat(STAMP)
    assert calls[0][2]['job_id'] == calls[2][2]['job_id']


@pytest.mark.parametrize('bad', [None, '', 'invalid', '2026-10-10T12:00:00'])
def test_R1_missing_or_invalid_source_time_is_rejected_before_post(bad):
    _, p = ready(); calls, _ = wire(p)
    with pytest.raises(RuntimeError):
        p.on_pre_compress([event(timestamp=bad)], require_checkpoint=True)
    assert not calls


def test_R2_never_enters_legacy_identity_scope_or_headers():
    _, p = ready(); calls, _ = wire(p)
    def forbidden(*args, **kwargs): raise AssertionError('legacy helper entered hook')
    p._build_scope = forbidden
    p._load_server_identity = forbidden
    p._resolve_tenant_id = forbidden
    p._auth_headers = forbidden
    assert p.on_pre_compress([event()], require_checkpoint=True)
    assert len(calls) == 2
    assert all('snapshot' in kwargs and 'deadline' in kwargs for _, _, kwargs in calls)


def test_R2_cold_oauth_rejected_without_lookup():
    _, p = ready(); calls, _ = wire(p)
    p._oauth_client_secret = 'fixture'
    p._server_identity_loaded = False
    p._load_server_identity = lambda: pytest.fail('identity lookup in hook')
    with pytest.raises(RuntimeError): p.on_pre_compress([event()], require_checkpoint=True)
    assert not calls


def test_R2_total_budget_includes_lock(monkeypatch):
    _, p = ready(); calls, _ = wire(p)
    # Test-only clock budget; production constant is bounded and not configurable upwards.
    monkeypatch.setattr(type(p), 'CHECKPOINT_TOTAL_SECONDS', 0.05, raising=False)
    p._checkpoint_lock.acquire()
    start = time.perf_counter()
    try:
        with pytest.raises(RuntimeError): p.on_pre_compress([event()], require_checkpoint=True)
    finally: p._checkpoint_lock.release()
    assert time.perf_counter() - start < 0.3
    assert not calls


def test_R4_closed_and_automatic_cron_admission():
    _, p = ready(); calls, _ = wire(p)
    p._closed = True
    with pytest.raises(RuntimeError): p.on_pre_compress([event()], require_checkpoint=True)
    assert not calls
    p._closed = False
    p._lifecycle_writes_disabled = True
    p._writes_disabled = False  # Explicit cron writes remain allowed.
    with pytest.raises(RuntimeError): p.on_pre_compress([event()], require_checkpoint=True)
    assert not calls


def test_R5_order_occurrences_restart_and_overlap():
    _, p = ready(); _, payloads = wire(p)
    a, b, c = event('a', 'same'), event('b', 'same'), event('c', 'third')
    p.on_pre_compress([a, b], require_checkpoint=True)
    p.on_pre_compress([b, c], require_checkpoint=True)
    p.on_pre_compress([b, a], require_checkpoint=True)
    _, restarted = ready(); _, restart_payloads = wire(restarted)
    restarted.on_pre_compress([a, b], require_checkpoint=True)
    assert payloads[0]['idempotency_key'] == restart_payloads[0]['idempotency_key']
    assert payloads[0]['body'] == restart_payloads[0]['body']
    assert payloads[0]['idempotency_key'] != payloads[2]['idempotency_key']
    manifests = [json.loads(row['body']) for row in payloads]
    assert [row['source_event_id'] for row in manifests[0]['source_evidence']] == ['a', 'b']
    assert [row['source_event_id'] for row in manifests[1]['source_evidence']] == ['b', 'c']
    assert manifests[0]['source_evidence'][1]['evidence_id'] == manifests[1]['source_evidence'][0]['evidence_id']


@pytest.mark.parametrize('rows', [
    [event(), event('bad', json.dumps({'checkpoint_handoff': 2, 'constraints': 'not a list'}))],
    [event(), event('bad', 'x' * 25000)],
    [event(str(i)) for i in range(17)],
    [event(), {'role': 'user', 'content': 'missing identity/time'}],
    [event(), event()],
])
def test_R5_prevalidate_entire_manifest_before_any_write(rows):
    _, p = ready(); calls, _ = wire(p)
    with pytest.raises(RuntimeError): p.on_pre_compress(rows, require_checkpoint=True)
    assert not calls
