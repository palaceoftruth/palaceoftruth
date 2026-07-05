import asyncio
import uuid
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.api.memory import router
from app.auth import verify_memory_auth
from app.database import get_db
from app.main import app as main_app
from app.models.item import Item
from app.models.job import Job
from app.schemas.memory import (
    AgentMemoryRetrieveRequest,
    AgentMemoryRetrieveResponse,
    AgentMemoryRetrieveTrace,
    LegacyMemoryArtifactRequest,
    MemoryEntryRequest,
    MemoryEntryListItem,
    MemoryEntryListResponse,
    MemoryRetrievalDoctorAuthShape,
    MemoryRetrievalDoctorCheck,
    MemoryRetrievalDoctorRequest,
    MemoryRetrievalDoctorResponse,
    MemoryRetrievalDoctorProbeReport,
    MemoryRetrievalDoctorProbeTopResult,
    MemoryRetrievalDoctorRelationshipState,
    MemoryRetrieveRequest,
    MemoryRetrieveResponse,
    MemoryScopeListResponse,
    MemoryScopeSummary,
    MemorySourceTrustSummaryResponse,
    MemoryTrajectoryEntry,
    MemoryTrajectoryResponse,
    MemoryWakeupBriefResponse,
)
from app.schemas.palace import PalaceRetrieveResponse, PalaceRetrieveTrace, PalaceWorkerBackpressureSummary
from app.schemas.search import SearchResult
from app.services import memory as memory_service
from app.services.memory import (
    MEMORY_JOB_TYPE,
    DelegatedAgentMemoryReadPolicy,
    MemoryArtifactAcceptanceResult,
    build_memory_retrieval_doctor,
    delegated_agent_memory_policy_from_config,
    parse_delegated_agent_memory_read_policies,
)
from app.services.source_trust_summary import SourceTrustSummary
from app.workers.queues import singleton_job_id


class FakeSession:
    def __init__(self, jobs=None) -> None:
        self.jobs = jobs or {}
        self.mcp_clients = []
        self.mcp_audit_events = []
        self.executed = []
        self.commits = 0

    async def get(self, model, key):
        return self.jobs.get(key)

    async def scalar(self, statement):
        for value in self.jobs.values():
            if isinstance(value, Job) and value.job_type == MEMORY_JOB_TYPE and value.tenant_id == "tenant-a":
                return value
        return None

    async def execute(self, statement, params=None):
        params = params or {}
        raw_sql = str(statement).lower()
        if "insert into mcp_clients" in raw_sql:
            row = next(
                (
                    client
                    for client in self.mcp_clients
                    if client["tenant_id"] == params["tenant_id"]
                    and client["client_key"] == params["client_key"]
                ),
                None,
            )
            if row is None:
                row = {
                    "id": uuid.uuid4(),
                    "tenant_id": params["tenant_id"],
                    "client_key": params["client_key"],
                    "display_name": params["display_name"],
                }
                self.mcp_clients.append(row)
            else:
                row["display_name"] = params["display_name"]
            return _MappingRows([{"id": row["id"]}])

        if "insert into mcp_request_audit_events" in raw_sql:
            row = {
                "id": uuid.uuid4(),
                "tenant_id": params["tenant_id"],
                "client_id": params["client_id"],
                "client_key": params["client_key"],
                "client_name": params["client_name"],
                "operation": params["operation"],
                "required_scope": params["required_scope"],
                "params_summary": params["params_summary"],
                "status": params["status"],
                "latency_ms": params["latency_ms"],
                "error_class": params["error_class"],
                "app_version": params["app_version"],
            }
            self.mcp_audit_events.append(row)
            return _MappingRows([{"id": row["id"]}])

        sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
        self.executed.append(sql)
        filtered = list(self.jobs.values())

        if "jobs.job_type = 'memory_artifact'" in sql:
            filtered = [job for job in filtered if job.job_type == MEMORY_JOB_TYPE]
        if "jobs.tenant_id = 'tenant-a'" in sql:
            filtered = [job for job in filtered if job.tenant_id == "tenant-a"]
        if "jobs.status = 'completed'" in sql:
            filtered = [job for job in filtered if job.status == "completed"]

        if "count(*)" in sql.lower():
            return _ScalarResult(len(filtered))
        return _ScalarsResult(filtered)

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _value) -> None:
        return None


class _ScalarResult:
    def __init__(self, value: int) -> None:
        self.value = value

    def scalar_one(self) -> int:
        return self.value


class _ScalarsResult:
    def __init__(self, values) -> None:
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return self.values


class _MappingRows:
    def __init__(self, rows) -> None:
        self.rows = rows

    def mappings(self):
        return self

    def one(self):
        if len(self.rows) != 1:
            raise AssertionError(f"Expected exactly one row, got {len(self.rows)}")
        return self.rows[0]

    def all(self):
        return self.rows


class FakeArqPool:
    def __init__(self) -> None:
        self.enqueued = []
        self.next_result = SimpleNamespace(job_id="queued")
        self.raise_on_enqueue: Exception | None = None

    async def enqueue_job(self, name: str, **kwargs) -> None:
        if self.raise_on_enqueue is not None:
            raise self.raise_on_enqueue
        self.enqueued.append((name, kwargs))
        return self.next_result


def _build_app(
    session: FakeSession,
    *,
    tenant_id: str = "tenant-a",
    auth_mode: str | None = None,
    mcp_client_key: str | None = None,
    mcp_allowed_scopes: list[str] | None = None,
) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.arq_pool = FakeArqPool()
    app.state.embedder = object()

    async def override_get_db():
        yield session

    async def override_verify(request: Request):
        request.state.tenant_id = tenant_id
        request.state.key_hash = "key-hash"
        request.state.auth_mode = auth_mode
        request.state.mcp_client_key = mcp_client_key
        request.state.mcp_allowed_scopes = mcp_allowed_scopes
        return "raw-key"

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[verify_memory_auth] = override_verify
    return TestClient(app)


def _legacy_payload(memory_kind: str = "task_retrospective") -> dict:
    payload = {
        "tenant_id": "tenant-a",
        "company_id": "company-a",
        "memory_kind": memory_kind,
        "title": "Task Retrospective: task-123",
        "summary": "Founder marketing CTA decision.",
        "body": "task-123 was approved and instrumented for the founder review.",
        "tags": ["agent-retrospective"],
        "created_by_role": "agent",
        "source": "exampleos",
        "created_at": "2026-04-08T20:00:00Z",
        "task_id": "task-123",
    }
    if memory_kind == "content_approval":
        payload.update(
            {
                "title": "Content Approval: ticket-9",
                "body": "ticket-9 approved for a LinkedIn launch post.",
                "task_id": None,
                "ticket_id": "ticket-9",
            }
        )
    if memory_kind == "founder_note":
        payload.update(
            {
                "title": "Founder Note",
                "body": "Founder note about tomorrow's launch sequence.",
                "task_id": None,
            }
        )
    return payload


def _canonical_payload() -> dict:
    return {
        "tenant_id": "tenant-a",
        "title": "Shared launch brief",
        "summary": "Cross-host workspace context.",
        "body": "Agents should reuse the same launch brief when they migrate hosts.",
        "source": "hermes",
        "created_at": "2026-04-12T12:00:00Z",
        "tags": ["launch"],
        "scope": {"type": "workspace", "key": "launch-pad"},
    }


def _memory_result(
    item_id: str,
    title: str,
    chunk_text: str,
    *,
    workspace_key: str | None = None,
    score: float = 0.9,
) -> SearchResult:
    tags = [f"workspace-{workspace_key}"] if workspace_key else []
    return SearchResult(
        item_id=uuid.UUID(item_id),
        title=title,
        summary=None,
        source_type="note",
        source_url=None,
        tags=tags,
        source_project=workspace_key,
        created_at=datetime.now(timezone.utc),
        chunk_text=chunk_text,
        chunk_index=0,
        score=score,
    )


def _agent_memory_result(
    item_id: str,
    title: str,
    chunk_text: str,
    *,
    agent_key: str,
    score: float = 0.9,
) -> SearchResult:
    return SearchResult(
        item_id=uuid.UUID(item_id),
        title=title,
        summary=None,
        source_type="note",
        source_url=None,
        tags=[f"scope-agent", f"agent-{agent_key}"],
        retrieved_scope_type="agent",
        retrieved_scope_key=agent_key,
        retrieved_scope_label=f"agent/{agent_key}",
        created_at=datetime.now(timezone.utc),
        chunk_text=chunk_text,
        chunk_index=0,
        score=score,
    )


def test_record_mcp_request_audit_upserts_client_and_redacted_event() -> None:
    session = FakeSession()
    client = _build_app(session)

    response = client.post(
        "/api/v1/memory/mcp/audit",
        json={
            "client": {
                "client_key": "codex-local",
                "display_name": "Codex local MCP",
                "allowed_scopes": ["read", "write"],
                "metadata": {"install": "repo-plugin"},
            },
            "operation": "create_memory_entry",
            "required_scope": "write",
            "params_summary": {
                "title": "Launch note",
                "body": {"redacted": True, "present": True},
                "metadata": {"redacted": True, "present": True},
            },
            "status": "success",
            "latency_ms": 42,
            "app_version": "0.1.200",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["tenant_id"] == "tenant-a"
    assert body["status"] == "recorded"
    assert len(session.mcp_clients) == 1
    assert session.mcp_clients[0]["client_key"] == "codex-local"
    assert len(session.mcp_audit_events) == 1
    event = session.mcp_audit_events[0]
    assert event["tenant_id"] == "tenant-a"
    assert event["operation"] == "create_memory_entry"
    assert event["required_scope"] == "write"
    assert event["status"] == "success"
    assert "raw-key" not in str(event)
    assert session.commits == 1


def test_record_mcp_request_audit_rejects_api_key_without_scope_header() -> None:
    session = FakeSession()
    client = _build_app(session, auth_mode="api_key")

    response = client.post(
        "/api/v1/memory/mcp/audit",
        json={
            "client": {
                "client_key": "codex-local",
                "display_name": "Codex local MCP",
                "allowed_scopes": ["read", "write"],
                "metadata": {},
            },
            "operation": "create_memory_entry",
            "required_scope": "write",
            "params_summary": {},
            "status": "success",
            "latency_ms": 42,
            "app_version": "0.1.200",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "API key missing write MCP scope header"
    assert session.mcp_clients == []
    assert session.mcp_audit_events == []


def test_record_mcp_request_audit_accepts_api_key_with_scope_header() -> None:
    session = FakeSession()
    client = _build_app(session, auth_mode="api_key")

    response = client.post(
        "/api/v1/memory/mcp/audit",
        headers={"X-MCP-Scope": "write"},
        json={
            "client": {
                "client_key": "codex-local",
                "display_name": "Codex local MCP",
                "allowed_scopes": ["read", "write"],
                "metadata": {},
            },
            "operation": "create_memory_entry",
            "required_scope": "write",
            "params_summary": {},
            "status": "success",
            "latency_ms": 42,
            "app_version": "0.1.200",
        },
    )

    assert response.status_code == 201
    assert len(session.mcp_audit_events) == 1


def test_memory_whoami_returns_authenticated_tenant() -> None:
    client = _build_app(FakeSession())

    response = client.get("/api/v1/memory/whoami")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "tenant_id": "tenant-a",
        "auth_mode": None,
        "mcp_client_id": None,
        "mcp_client_key": None,
        "allowed_scopes": [],
        "resource": None,
        "audience": None,
        "token_hash_prefix": "key-hash",
    }


def test_memory_whoami_returns_mcp_oauth_scope_metadata() -> None:
    client = _build_app(
        FakeSession(),
        auth_mode="mcp_oauth",
        mcp_client_key="codex-remote",
        mcp_allowed_scopes=["read", "write"],
    )

    response = client.get("/api/v1/memory/whoami")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "tenant_id": "tenant-a",
        "auth_mode": "mcp_oauth",
        "mcp_client_id": None,
        "mcp_client_key": "codex-remote",
        "allowed_scopes": ["read", "write"],
        "resource": None,
        "audience": None,
        "token_hash_prefix": "key-hash",
    }


def test_memory_entries_accepts_canonical_payload(monkeypatch) -> None:
    client = _build_app(FakeSession())

    async def fake_accept_canonical_memory_entry(
        db, *, body: MemoryEntryRequest, signing_key: str, admission_audit: dict | None = None
    ):
        assert signing_key == "key-hash"
        assert body.scope.type == "workspace"
        assert body.scope.key == "launch-pad"
        assert body.relationship_policy == "deferred"
        assert admission_audit is not None
        assert admission_audit["body_sha256"]
        assert "Agents should reuse" not in json.dumps(admission_audit)
        return MemoryArtifactAcceptanceResult(
            job=Job(
                id=uuid.uuid4(),
                job_type=MEMORY_JOB_TYPE,
                tenant_id=body.tenant_id,
                status="queued",
                progress=0,
                created_at=datetime.now(timezone.utc),
            ),
            enqueue_requested=True,
            scope_type=body.scope.type,
            scope_key=body.scope.key,
            accepted_as="canonical",
        )

    monkeypatch.setattr("app.api.memory.accept_canonical_memory_entry", fake_accept_canonical_memory_entry)

    payload = _canonical_payload()
    payload["relationship_policy"] = "deferred"
    response = client.post("/api/v1/memory/entries", json=payload)

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert response.json()["contract_status"] == "accepted"
    assert response.json()["poll_url"].endswith(f"/api/v1/memory/jobs/{response.json()['job_id']}")
    assert response.json()["poll_after_seconds"] == 5
    assert response.headers["X-Palace-Memory-Contract-Status"] == "accepted"
    assert response.headers["X-Palace-Memory-Poll-After"] == "5"
    assert response.headers["X-Palace-Rate-Limit-State"] == "not_enforced"
    assert response.json()["scope"] == {"type": "workspace", "key": "launch-pad"}
    assert response.json()["accepted_as"] == "canonical"
    assert len(client.app.state.arq_pool.enqueued) == 1


def test_memory_entries_reports_saturated_queue_hint(monkeypatch) -> None:
    client = _build_app(FakeSession())

    async def fake_queue_hint(_arq_pool):
        return {
            "state": "saturated",
            "queue_name": "arq:queue",
            "queued_depth": 120,
            "worker_queue_depth": 120,
            "oldest_queued_age_seconds": 901,
            "retry_after_seconds": 60,
            "poll_after_seconds": 5,
            "rate_limit_state": "not_enforced",
        }

    async def fake_accept_canonical_memory_entry(
        db, *, body: MemoryEntryRequest, signing_key: str, admission_audit: dict | None = None
    ):
        return MemoryArtifactAcceptanceResult(
            job=Job(
                id=uuid.uuid4(),
                job_type=MEMORY_JOB_TYPE,
                tenant_id=body.tenant_id,
                status="queued",
                progress=0,
                created_at=datetime.now(timezone.utc),
            ),
            enqueue_requested=True,
            scope_type=body.scope.type,
            scope_key=body.scope.key,
            accepted_as="canonical",
        )

    monkeypatch.setattr("app.api.memory.build_memory_queue_hint", fake_queue_hint)
    monkeypatch.setattr("app.api.memory.accept_canonical_memory_entry", fake_accept_canonical_memory_entry)

    response = client.post("/api/v1/memory/entries", json=_canonical_payload())

    assert response.status_code == 202
    payload = response.json()
    assert payload["contract_status"] == "retryable_degraded"
    assert payload["retryable"] is True
    assert payload["retry_after_seconds"] == 60
    assert payload["queue"]["state"] == "saturated"
    assert payload["queue"]["queued_depth"] == 120
    assert response.headers["Retry-After"] == "60"
    assert response.headers["X-Palace-Memory-Queue-State"] == "saturated"
    assert response.headers["X-Palace-Memory-Queue-Depth"] == "120"


def test_memory_entries_preserves_retry_contract_for_failed_replay(monkeypatch) -> None:
    client = _build_app(FakeSession())
    job_id = uuid.uuid4()

    async def fake_accept_canonical_memory_entry(
        db, *, body: MemoryEntryRequest, signing_key: str, admission_audit: dict | None = None
    ):
        return MemoryArtifactAcceptanceResult(
            job=Job(
                id=job_id,
                job_type=MEMORY_JOB_TYPE,
                tenant_id=body.tenant_id,
                status="failed",
                progress=100,
                payload={"relationship_policy": "immediate"},
                error_message="Worker failed before enrichment",
                created_at=datetime.now(timezone.utc),
                completed_at=datetime.now(timezone.utc),
            ),
            enqueue_requested=False,
            scope_type=body.scope.type,
            scope_key=body.scope.key,
            accepted_as="canonical",
        )

    monkeypatch.setattr("app.api.memory.accept_canonical_memory_entry", fake_accept_canonical_memory_entry)

    response = client.post("/api/v1/memory/entries", json=_canonical_payload())

    assert response.status_code == 202
    assert response.json()["job_id"] == str(job_id)
    assert response.json()["status"] == "failed"
    assert response.json()["contract_status"] == "retryable_degraded"
    assert response.json()["retryable"] is True
    assert response.json()["retry_after_seconds"] == 30


def test_memory_entries_reports_duplicate_replay_metadata(monkeypatch) -> None:
    client = _build_app(FakeSession())
    job_id = uuid.uuid4()
    item_id = uuid.uuid4()

    async def fake_accept_canonical_memory_entry(
        db, *, body: MemoryEntryRequest, signing_key: str, admission_audit: dict | None = None
    ):
        return MemoryArtifactAcceptanceResult(
            job=Job(
                id=job_id,
                item_id=item_id,
                job_type=MEMORY_JOB_TYPE,
                tenant_id=body.tenant_id,
                status="completed",
                progress=100,
                created_at=datetime.now(timezone.utc),
                completed_at=datetime.now(timezone.utc),
            ),
            enqueue_requested=False,
            scope_type=body.scope.type,
            scope_key=body.scope.key,
            accepted_as="canonical",
            replayed=True,
            source_item_id=item_id,
        )

    monkeypatch.setattr("app.api.memory.accept_canonical_memory_entry", fake_accept_canonical_memory_entry)

    response = client.post("/api/v1/memory/entries", json=_canonical_payload())

    assert response.status_code == 202
    payload = response.json()
    assert payload["job_id"] == str(job_id)
    assert payload["source_item_id"] == str(item_id)
    assert payload["replayed"] is True
    assert payload["status"] == "complete"
    assert payload["contract_status"] == "completed"


def test_memory_entries_reports_queued_duplicate_replay_metadata(monkeypatch) -> None:
    client = _build_app(FakeSession())
    job_id = uuid.uuid4()
    item_id = uuid.uuid4()

    async def fake_accept_canonical_memory_entry(
        db, *, body: MemoryEntryRequest, signing_key: str, admission_audit: dict | None = None
    ):
        return MemoryArtifactAcceptanceResult(
            job=Job(
                id=job_id,
                item_id=item_id,
                job_type=MEMORY_JOB_TYPE,
                tenant_id=body.tenant_id,
                status="queued",
                progress=0,
                created_at=datetime.now(timezone.utc),
            ),
            enqueue_requested=False,
            scope_type=body.scope.type,
            scope_key=body.scope.key,
            accepted_as="canonical",
            replayed=True,
            source_item_id=item_id,
        )

    monkeypatch.setattr("app.api.memory.accept_canonical_memory_entry", fake_accept_canonical_memory_entry)

    response = client.post("/api/v1/memory/entries", json=_canonical_payload())

    assert response.status_code == 202
    payload = response.json()
    assert payload["job_id"] == str(job_id)
    assert payload["source_item_id"] == str(item_id)
    assert payload["replayed"] is True
    assert payload["status"] == "queued"
    assert payload["contract_status"] == "accepted"


def test_memory_entries_returns_structured_duplicate_conflict(monkeypatch) -> None:
    client = _build_app(FakeSession())
    job_id = uuid.uuid4()
    item_id = uuid.uuid4()

    async def fake_accept_canonical_memory_entry(*args, **kwargs):
        raise HTTPException(
            status_code=409,
            detail={
                "status": "duplicate_conflict",
                "contract_status": "rejected",
                "message": "Memory entry idempotency key already exists for a different payload or scope",
                "retryable": False,
                "conflict_kind": "payload_mismatch",
                "idempotency_key": "shared-key",
                "existing_job_id": str(job_id),
                "existing_source_item_id": str(item_id),
                "scope": {"type": "workspace", "key": "launch-pad"},
            },
        )

    monkeypatch.setattr("app.api.memory.accept_canonical_memory_entry", fake_accept_canonical_memory_entry)

    payload = _canonical_payload()
    payload["idempotency_key"] = "shared-key"
    response = client.post("/api/v1/memory/entries", json=payload)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["status"] == "duplicate_conflict"
    assert detail["contract_status"] == "rejected"
    assert detail["retryable"] is False
    assert detail["conflict_kind"] == "payload_mismatch"
    assert detail["existing_job_id"] == str(job_id)
    assert detail["existing_source_item_id"] == str(item_id)


def test_memory_entries_reports_queue_dependency_unavailable(monkeypatch) -> None:
    client = _build_app(FakeSession())
    client.app.state.arq_pool.raise_on_enqueue = RuntimeError("MasterNotFoundError: sensitive endpoint")
    job_id = uuid.uuid4()
    job = Job(
        id=job_id,
        job_type=MEMORY_JOB_TYPE,
        tenant_id="tenant-a",
        status="queued",
        progress=0,
        created_at=datetime.now(timezone.utc),
        payload={"relationship_policy": "immediate"},
    )

    async def fake_accept_canonical_memory_entry(
        db, *, body: MemoryEntryRequest, signing_key: str, admission_audit: dict | None = None
    ):
        return MemoryArtifactAcceptanceResult(
            job=job,
            enqueue_requested=True,
            scope_type=body.scope.type,
            scope_key=body.scope.key,
            accepted_as="canonical",
        )

    monkeypatch.setattr("app.api.memory.accept_canonical_memory_entry", fake_accept_canonical_memory_entry)

    response = client.post("/api/v1/memory/entries", json=_canonical_payload())

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "status": "dependency_unavailable",
        "message": "Memory queue unavailable; retry the accepted job after dependency recovery",
        "retryable": True,
        "job_id": str(job_id),
        "contract_status": "dependency_unavailable",
        "poll_url": f"http://testserver/api/v1/memory/jobs/{job_id}",
        "poll_after_seconds": 10,
        "retry_after_seconds": 30,
        "rate_limit_state": "not_enforced",
    }
    assert response.headers["Retry-After"] == "30"
    assert response.headers["X-Palace-Memory-Contract-Status"] == "dependency_unavailable"
    assert job.status == "failed"
    assert job.payload["contract_status"] == "dependency_unavailable"
    assert "MasterNotFoundError" not in response.text


def test_memory_entries_batch_reports_mixed_item_results(monkeypatch) -> None:
    client = _build_app(FakeSession())
    accepted_job_id = uuid.uuid4()
    duplicate_job_id = uuid.uuid4()
    calls: list[str] = []

    async def fake_accept_canonical_memory_entry(
        db, *, body: MemoryEntryRequest, signing_key: str, admission_audit: dict | None = None
    ):
        assert signing_key == "key-hash"
        assert admission_audit is not None
        calls.append(body.title)
        job_id = accepted_job_id if body.title == "Shared launch brief" else duplicate_job_id
        status = "queued" if body.title == "Shared launch brief" else "completed"
        return MemoryArtifactAcceptanceResult(
            job=Job(
                id=job_id,
                job_type=MEMORY_JOB_TYPE,
                tenant_id=body.tenant_id,
                status=status,
                progress=0 if status == "queued" else 100,
                created_at=datetime.now(timezone.utc),
                completed_at=datetime.now(timezone.utc) if status == "completed" else None,
            ),
            enqueue_requested=status == "queued",
            scope_type=body.scope.type,
            scope_key=body.scope.key,
            accepted_as="canonical",
        )

    monkeypatch.setattr("app.api.memory.accept_canonical_memory_entry", fake_accept_canonical_memory_entry)

    duplicate = _canonical_payload()
    duplicate["title"] = "Duplicate memory"
    duplicate["idempotency_key"] = "duplicate-key"
    tenant_mismatch = _canonical_payload()
    tenant_mismatch["tenant_id"] = "tenant-b"
    tenant_mismatch["idempotency_key"] = "tenant-mismatch-key"

    response = client.post(
        "/api/v1/memory/entries:batch",
        json={"entries": [_canonical_payload(), duplicate, tenant_mismatch]},
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "partial"
    assert payload["accepted"] == 2
    assert payload["failed"] == 1
    assert payload["max_entries"] == 100
    assert [result["index"] for result in payload["results"]] == [0, 1, 2]
    assert payload["results"][0]["job_id"] == str(accepted_job_id)
    assert payload["results"][0]["poll_url"].endswith(f"/api/v1/memory/jobs/{accepted_job_id}")
    assert payload["results"][1]["status"] == "complete"
    assert payload["results"][1]["contract_status"] == "completed"
    assert payload["results"][2]["status"] == "failed"
    assert payload["results"][2]["contract_status"] == "permanent_tenant_mismatch"
    assert payload["results"][2]["error"]["retryable"] is False
    assert calls == ["Shared launch brief", "Duplicate memory"]
    assert len(client.app.state.arq_pool.enqueued) == 1


def test_memory_entries_batch_reports_duplicate_conflict_per_item(monkeypatch) -> None:
    client = _build_app(FakeSession())
    job_id = uuid.uuid4()
    item_id = uuid.uuid4()

    async def fake_accept_canonical_memory_entry(*args, **kwargs):
        raise HTTPException(
            status_code=409,
            detail={
                "status": "duplicate_conflict",
                "contract_status": "rejected",
                "message": "Memory entry idempotency key already exists for a different payload or scope",
                "retryable": False,
                "conflict_kind": "payload_mismatch",
                "idempotency_key": "shared-key",
                "existing_job_id": str(job_id),
                "existing_source_item_id": str(item_id),
                "scope": {"type": "workspace", "key": "launch-pad"},
            },
        )

    monkeypatch.setattr("app.api.memory.accept_canonical_memory_entry", fake_accept_canonical_memory_entry)

    payload = _canonical_payload()
    payload["idempotency_key"] = "shared-key"
    response = client.post("/api/v1/memory/entries:batch", json={"entries": [payload]})

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "failed"
    assert body["accepted"] == 0
    assert body["failed"] == 1
    result = body["results"][0]
    assert result["status"] == "failed"
    assert result["contract_status"] == "rejected"
    assert result["retryable"] is False
    assert result["job_id"] == str(job_id)
    assert result["source_item_id"] == str(item_id)
    assert result["error"]["status"] == "duplicate_conflict"


def test_memory_entries_batch_reports_queue_dependency_failure_per_item(monkeypatch) -> None:
    client = _build_app(FakeSession())
    client.app.state.arq_pool.raise_on_enqueue = RuntimeError("MasterNotFoundError: private endpoint")
    job_id = uuid.uuid4()

    async def fake_accept_canonical_memory_entry(
        db, *, body: MemoryEntryRequest, signing_key: str, admission_audit: dict | None = None
    ):
        return MemoryArtifactAcceptanceResult(
            job=Job(
                id=job_id,
                job_type=MEMORY_JOB_TYPE,
                tenant_id=body.tenant_id,
                status="queued",
                progress=0,
                created_at=datetime.now(timezone.utc),
                payload={"relationship_policy": "immediate"},
            ),
            enqueue_requested=True,
            scope_type=body.scope.type,
            scope_key=body.scope.key,
            accepted_as="canonical",
        )

    monkeypatch.setattr("app.api.memory.accept_canonical_memory_entry", fake_accept_canonical_memory_entry)

    response = client.post("/api/v1/memory/entries:batch", json={"entries": [_canonical_payload()]})

    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "failed"
    assert payload["accepted"] == 0
    assert payload["failed"] == 1
    assert payload["retryable"] is True
    assert response.headers["Retry-After"] == "30"
    result = payload["results"][0]
    assert result["job_id"] == str(job_id)
    assert result["contract_status"] == "dependency_unavailable"
    assert result["retryable"] is True
    assert "MasterNotFoundError" not in response.text


def test_memory_entries_quarantines_secret_body_before_storage(monkeypatch) -> None:
    client = _build_app(FakeSession())

    async def fake_accept_canonical_memory_entry(*args, **kwargs):
        raise AssertionError("quarantined writes must not reach storage")

    monkeypatch.setattr("app.api.memory.accept_canonical_memory_entry", fake_accept_canonical_memory_entry)

    payload = _canonical_payload()
    payload["body"] = "Do not store Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456"
    response = client.post("/api/v1/memory/entries", json=payload)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["status"] == "quarantined"
    assert detail["reason_code"] == "potential_secret"
    assert detail["retryable"] is False
    assert "abcdefghijklmnopqrstuvwxyz" not in response.text
    assert detail["audit"]["privacy_scan"]["findings"][0]["pattern"] == "bearer_authorization"
    assert client.app.state.arq_pool.enqueued == []


def test_memory_entries_quarantines_raw_transcript_body_before_storage(monkeypatch) -> None:
    client = _build_app(FakeSession())

    async def fake_accept_canonical_memory_entry(*args, **kwargs):
        raise AssertionError("quarantined writes must not reach storage")

    monkeypatch.setattr("app.api.memory.accept_canonical_memory_entry", fake_accept_canonical_memory_entry)

    payload = _canonical_payload()
    payload["source"] = "agent-transcript-import"
    payload["body"] = "\n".join(
        [
            "User: first raw turn",
            "Assistant: second raw turn",
            "User: third raw turn",
            "Assistant: fourth raw turn",
            "User: fifth raw turn",
            "Assistant: sixth raw turn",
        ]
    )
    response = client.post("/api/v1/memory/entries", json=payload)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["status"] == "quarantined"
    assert detail["reason_code"] == "raw_transcript_body"
    assert "first raw turn" not in response.text
    assert client.app.state.arq_pool.enqueued == []


def test_memory_entries_redacts_secret_source_in_quarantine_response(monkeypatch) -> None:
    client = _build_app(FakeSession())

    async def fake_accept_canonical_memory_entry(*args, **kwargs):
        raise AssertionError("quarantined writes must not reach storage")

    monkeypatch.setattr("app.api.memory.accept_canonical_memory_entry", fake_accept_canonical_memory_entry)

    payload = _canonical_payload()
    payload["source"] = "Bearer abcdefghijklmnopqrstuvwxyz123456"
    response = client.post("/api/v1/memory/entries", json=payload)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["status"] == "quarantined"
    assert detail["reason_code"] == "potential_secret"
    assert "abcdefghijklmnopqrstuvwxyz" not in response.text
    assert "source_hash" in detail["audit"]
    assert "source" not in detail["audit"]


def test_memory_entries_rejects_idempotency_key_longer_than_database_limit() -> None:
    client = _build_app(FakeSession())

    payload = _canonical_payload()
    payload["idempotency_key"] = "x" * 65

    response = client.post("/api/v1/memory/entries", json=payload)

    assert response.status_code == 422
    assert "idempotency_key" in response.text
    assert "64" in response.text


def test_memory_entries_list_uses_authenticated_tenant_and_scope_filters(monkeypatch) -> None:
    client = _build_app(FakeSession())
    item_id = uuid.uuid4()

    async def fake_list_memory_entries(db, *, tenant_id, scope, tags, tags_mode, limit, cursor):
        assert tenant_id == "tenant-a"
        assert scope.type == "agent"
        assert scope.key == "codex"
        assert tags == ["launch", "agent-memory"]
        assert tags_mode == "all"
        assert limit == 10
        assert cursor.isoformat() == "2026-04-13T12:00:00+00:00"
        return MemoryEntryListResponse(
            entries=[
                MemoryEntryListItem(
                    source_item_id=item_id,
                    title="Launch brief",
                    summary="Cross-host launch context.",
                    source="mcp",
                    source_url=None,
                    scope=scope,
                    tags=["launch", "agent-memory"],
                    created_at=datetime(2026, 4, 12, 12, 0, tzinfo=timezone.utc),
                    updated_at=datetime(2026, 4, 12, 12, 3, tzinfo=timezone.utc),
                    readiness_state="ready",
                    job_id=None,
                    job_status=None,
                )
            ],
            total=1,
            limit=10,
            next_cursor=None,
        )

    monkeypatch.setattr("app.api.memory.list_memory_entries", fake_list_memory_entries)

    response = client.get(
        "/api/v1/memory/entries"
        "?scope_type=agent&scope_key=codex"
        "&tags=launch&tags=agent-memory"
        "&tags_mode=all&limit=10&cursor=2026-04-13T12:00:00Z"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["entries"][0]["source_item_id"] == str(item_id)
    assert payload["entries"][0]["scope"] == {"type": "agent", "key": "codex"}


def test_memory_entries_list_validates_scope_without_query() -> None:
    client = _build_app(FakeSession())

    response = client.get("/api/v1/memory/entries?scope_type=agent")

    assert response.status_code == 422
    assert "agent scope requires a key" in response.json()["detail"]


def test_memory_entries_list_rejects_blank_tags() -> None:
    client = _build_app(FakeSession())

    response = client.get("/api/v1/memory/entries?tags=%20")

    assert response.status_code == 422
    assert "tags must not contain blank values" in response.json()["detail"]


def test_memory_source_trust_summaries_uses_authenticated_tenant(monkeypatch) -> None:
    item_id = uuid.uuid4()
    client = _build_app(FakeSession())

    async def fake_source_trust(db, *, tenant_id: str, item_ids):
        assert tenant_id == "tenant-a"
        assert item_ids == [item_id]
        return {
            item_id: SourceTrustSummary(
                item_id=item_id,
                state="source_backed",
                source_status="active",
                chunk_count=2,
                source_title="Operator source",
                source_url="https://example.test/source",
            )
        }

    monkeypatch.setattr("app.api.memory.get_source_trust_summaries", fake_source_trust)

    response = client.post("/api/v1/memory/source-trust-summaries", json={"item_ids": [str(item_id)]})

    assert response.status_code == 200
    payload = MemorySourceTrustSummaryResponse.model_validate(response.json())
    assert payload.summaries[0].item_id == item_id
    assert payload.summaries[0].state == "source_backed"
    response_json = json.dumps(response.json())
    assert "preview" not in response_json
    assert "chunk_text" not in response_json
    assert "raw production content" not in response_json


def test_memory_scopes_list_uses_authenticated_tenant(monkeypatch) -> None:
    client = _build_app(FakeSession())

    async def fake_list_memory_scopes(db, *, tenant_id: str, limit: int, sample_limit: int):
        assert tenant_id == "tenant-a"
        assert limit == 25
        assert sample_limit == 4
        return MemoryScopeListResponse(
            scopes=[
                MemoryScopeSummary(
                    scope={"type": "workspace", "key": "exampleos"},
                    entry_count=3,
                    latest_created_at=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
                    latest_updated_at=datetime(2026, 5, 6, 12, 5, tzinfo=timezone.utc),
                    tags=["codex-memory"],
                    sources=["codex"],
                )
            ],
            total=1,
            limit=limit,
        )

    monkeypatch.setattr("app.api.memory.list_memory_scopes", fake_list_memory_scopes)

    response = client.get("/api/v1/memory/scopes?limit=25&sample_limit=4")

    assert response.status_code == 200
    payload = response.json()
    assert payload["scopes"][0]["scope"] == {"type": "workspace", "key": "exampleos"}
    assert payload["scopes"][0]["entry_count"] == 3
    assert payload["scopes"][0]["sources"] == ["codex"]


def test_memory_relationship_backfill_endpoint_enqueues_tenant_sweep() -> None:
    client = _build_app(FakeSession())
    lease_key = singleton_job_id("backfill_deferred_relationships", "tenant-a")

    response = client.post("/api/v1/memory/relationships/backfill", json={"limit": 25, "defer_seconds": 7})

    assert response.status_code == 202
    assert response.json() == {
        "status": "queued",
        "tenant_id": "tenant-a",
        "limit": 25,
        "defer_seconds": 7,
        "lease_key": lease_key,
        "lease_holder": lease_key,
    }
    assert client.app.state.arq_pool.enqueued == [
        (
            "backfill_deferred_relationships",
            {
                "_job_id": lease_key,
                "tenant_id": "tenant-a",
                "limit": 25,
                "defer_seconds": 7,
            },
        )
    ]


def test_memory_relationship_backfill_endpoint_reports_active_duplicate_lease() -> None:
    client = _build_app(FakeSession())
    client.app.state.arq_pool.next_result = None
    lease_key = singleton_job_id("backfill_deferred_relationships", "tenant-a")

    response = client.post("/api/v1/memory/relationships/backfill", json={"limit": 25, "defer_seconds": 7})

    assert response.status_code == 202
    assert response.json()["status"] == "active"
    assert response.json()["lease_key"] == lease_key
    assert response.json()["lease_holder"] == lease_key


def test_memory_oauth_bearer_scope_denies_write_without_write_scope() -> None:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.arq_pool = FakeArqPool()
    app.state.embedder = object()

    async def override_get_db():
        yield FakeSession()

    async def override_verify(request: Request):
        request.state.tenant_id = "tenant-a"
        request.state.key_hash = "token-hash"
        request.state.auth_mode = "mcp_oauth"
        request.state.mcp_client_key = "read-only"
        request.state.mcp_allowed_scopes = ["read"]
        return "token"

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[verify_memory_auth] = override_verify
    client = TestClient(app)

    response = client.post("/api/v1/memory/entries", json=_canonical_payload())

    assert response.status_code == 403
    assert response.json()["detail"] == "MCP bearer token missing write scope"


def test_memory_api_key_scope_denies_write_without_scope_header(monkeypatch) -> None:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.arq_pool = FakeArqPool()
    app.state.embedder = object()

    async def override_get_db():
        yield FakeSession()

    async def override_verify(request: Request):
        request.state.tenant_id = "tenant-a"
        request.state.key_hash = "key-hash"
        request.state.auth_mode = "api_key"
        request.state.mcp_client_key = None
        request.state.mcp_allowed_scopes = None
        return "raw-key"

    async def fake_accept_canonical_memory_entry(*args, **kwargs):
        raise AssertionError("unscoped API-key writes must not reach storage")

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[verify_memory_auth] = override_verify
    monkeypatch.setattr("app.api.memory.accept_canonical_memory_entry", fake_accept_canonical_memory_entry)
    client = TestClient(app)

    response = client.post("/api/v1/memory/entries", json=_canonical_payload())

    assert response.status_code == 403
    assert response.json()["detail"] == "API key missing write MCP scope header"


def test_memory_api_key_scope_specific_grant_allows_workspace_write(monkeypatch) -> None:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.arq_pool = FakeArqPool()
    app.state.embedder = object()

    async def override_get_db():
        yield FakeSession()

    async def override_verify(request: Request):
        request.state.tenant_id = "tenant-a"
        request.state.key_hash = "key-hash"
        request.state.auth_mode = "api_key"
        request.state.mcp_client_key = None
        request.state.mcp_allowed_scopes = None
        return "raw-key"

    async def fake_accept_canonical_memory_entry(
        db, *, body: MemoryEntryRequest, signing_key: str, admission_audit: dict | None = None
    ):
        assert signing_key == "key-hash"
        assert admission_audit is not None
        assert admission_audit["scope_grant"]["required_scope"] == "write:workspace"
        assert admission_audit["scope_grant"]["grant_present"] is True
        return MemoryArtifactAcceptanceResult(
            job=Job(
                id=uuid.uuid4(),
                job_type=MEMORY_JOB_TYPE,
                tenant_id=body.tenant_id,
                status="queued",
                progress=0,
                created_at=datetime.now(timezone.utc),
            ),
            enqueue_requested=True,
            scope_type=body.scope.type,
            scope_key=body.scope.key,
            accepted_as="canonical",
        )

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[verify_memory_auth] = override_verify
    monkeypatch.setattr("app.api.memory.accept_canonical_memory_entry", fake_accept_canonical_memory_entry)
    client = TestClient(app)

    response = client.post(
        "/api/v1/memory/entries",
        headers={"X-MCP-Scope": "write", "X-MCP-Scopes": "read,write,write:workspace"},
        json=_canonical_payload(),
    )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"


def test_memory_oauth_bearer_write_does_not_imply_scoped_workspace_write(monkeypatch) -> None:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.arq_pool = FakeArqPool()
    app.state.embedder = object()

    async def override_get_db():
        yield FakeSession()

    async def override_verify(request: Request):
        request.state.tenant_id = "tenant-a"
        request.state.key_hash = "token-hash"
        request.state.auth_mode = "mcp_oauth"
        request.state.mcp_client_key = "plain-writer"
        request.state.mcp_allowed_scopes = ["read", "write"]
        return "token"

    async def fake_accept_canonical_memory_entry(*args, **kwargs):
        raise AssertionError("rejected writes must not reach storage")

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[verify_memory_auth] = override_verify
    monkeypatch.setattr("app.api.memory.accept_canonical_memory_entry", fake_accept_canonical_memory_entry)
    client = TestClient(app)

    response = client.post("/api/v1/memory/entries", json=_canonical_payload())

    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail["status"] == "rejected"
    assert detail["reason_code"] == "missing_write_workspace"
    assert detail["audit"]["scope_grant"]["required_scope"] == "write:workspace"
    assert "launch-pad" not in response.text


def test_memory_oauth_bearer_scope_specific_grant_allows_workspace_write(monkeypatch) -> None:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.arq_pool = FakeArqPool()
    app.state.embedder = object()

    async def override_get_db():
        yield FakeSession()

    async def override_verify(request: Request):
        request.state.tenant_id = "tenant-a"
        request.state.key_hash = "token-hash"
        request.state.auth_mode = "mcp_oauth"
        request.state.mcp_client_key = "workspace-writer"
        request.state.mcp_allowed_scopes = ["read", "write", "write:workspace"]
        return "token"

    async def fake_accept_canonical_memory_entry(
        db, *, body: MemoryEntryRequest, signing_key: str, admission_audit: dict | None = None
    ):
        assert admission_audit is not None
        assert admission_audit["scope_grant"]["grant_present"] is True
        assert admission_audit["scope_grant"]["required_scope"] == "write:workspace"
        return MemoryArtifactAcceptanceResult(
            job=Job(
                id=uuid.uuid4(),
                job_type=MEMORY_JOB_TYPE,
                tenant_id=body.tenant_id,
                status="queued",
                progress=0,
                created_at=datetime.now(timezone.utc),
            ),
            enqueue_requested=True,
            scope_type=body.scope.type,
            scope_key=body.scope.key,
            accepted_as="canonical",
        )

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[verify_memory_auth] = override_verify
    monkeypatch.setattr("app.api.memory.accept_canonical_memory_entry", fake_accept_canonical_memory_entry)
    client = TestClient(app)

    response = client.post("/api/v1/memory/entries", json=_canonical_payload())

    assert response.status_code == 202
    assert response.json()["status"] == "queued"


def test_memory_artifacts_accepts_legacy_payloads(monkeypatch) -> None:
    client = _build_app(FakeSession())

    async def fake_accept_memory_artifact(db, *, body: LegacyMemoryArtifactRequest, signing_key: str):
        assert signing_key == "key-hash"
        return MemoryArtifactAcceptanceResult(
            job=Job(
                id=uuid.uuid4(),
                job_type=MEMORY_JOB_TYPE,
                tenant_id=body.tenant_id,
                status="queued",
                progress=0,
                created_at=datetime.now(timezone.utc),
            ),
            enqueue_requested=True,
            scope_type="tenant_shared",
            scope_key=None,
            accepted_as="legacy_artifact",
        )

    monkeypatch.setattr("app.api.memory.accept_memory_artifact", fake_accept_memory_artifact)

    for kind in ("task_retrospective", "content_approval", "founder_note"):
        response = client.post("/api/v1/memory/artifacts", json=_legacy_payload(kind))
        assert response.status_code == 202
        assert response.json()["status"] == "queued"
        assert response.json()["accepted_as"] == "legacy_artifact"

    assert len(client.app.state.arq_pool.enqueued) == 3


def test_memory_artifacts_reject_tenant_mismatch() -> None:
    client = _build_app(FakeSession(), tenant_id="tenant-a")

    payload = _legacy_payload()
    payload["tenant_id"] = "tenant-b"
    response = client.post("/api/v1/memory/artifacts", json=payload)

    assert response.status_code == 403
    assert response.json()["detail"]["status"] == "permanent_tenant_mismatch"
    assert response.json()["detail"]["retryable"] is False


def test_memory_job_endpoint_maps_completed_to_complete() -> None:
    job_id = uuid.uuid4()
    client = _build_app(
        FakeSession(
            jobs={
                job_id: Job(
                    id=job_id,
                    item_id=uuid.uuid4(),
                    job_type=MEMORY_JOB_TYPE,
                    tenant_id="tenant-a",
                    status="completed",
                    progress=100,
                    created_at=datetime.now(timezone.utc),
                    completed_at=datetime.now(timezone.utc),
                )
            }
        )
    )

    response = client.get(f"/api/v1/memory/jobs/{job_id}")

    assert response.status_code == 200
    assert response.json()["status"] == "complete"
    assert response.json()["contract_status"] == "completed"
    assert response.json()["created_at"] is not None


def test_memory_job_endpoint_marks_stale_queued_as_retryable_degraded() -> None:
    job_id = uuid.uuid4()
    client = _build_app(
        FakeSession(
            jobs={
                job_id: Job(
                    id=job_id,
                    item_id=uuid.uuid4(),
                    job_type=MEMORY_JOB_TYPE,
                    tenant_id="tenant-a",
                    status="queued",
                    progress=0,
                    created_at=datetime.now(timezone.utc) - timedelta(minutes=31),
                )
            }
        )
    )

    response = client.get(f"/api/v1/memory/jobs/{job_id}")

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert response.json()["contract_status"] == "retryable_degraded"


@pytest.mark.asyncio
async def test_dependency_unavailable_memory_job_replays_as_retryable_enqueue() -> None:
    item_id = uuid.uuid4()
    entry = memory_service.normalize_memory_entry(MemoryEntryRequest.model_validate(_canonical_payload()))
    job = Job(
        id=uuid.uuid4(),
        item_id=item_id,
        job_type=MEMORY_JOB_TYPE,
        tenant_id="tenant-a",
        status="failed",
        progress=100,
        error_message="Memory queue unavailable; retry the memory job after dependency recovery",
        payload={"contract_status": "dependency_unavailable", "relationship_policy": "immediate"},
        created_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
    )
    item = Item(
        id=item_id,
        tenant_id="tenant-a",
        title=entry.title,
        summary=entry.summary,
        source_type="note",
        source_url=entry.source_url,
        raw_content=entry.body,
        tags=entry.tags,
        status="processing",
        created_at=entry.created_at,
        updated_at=entry.created_at,
        metadata_=entry.metadata,
        idempotency_key=entry.idempotency_key,
    )
    session = FakeSession(jobs={job.id: job, item_id: item})

    result = await memory_service.accept_memory_entry(
        session,
        entry=entry,
        signing_key="key-hash",
    )

    assert result.job is job
    assert result.enqueue_requested is True
    assert job.status == "queued"
    assert job.progress == 0
    assert job.error_message is None
    assert job.completed_at is None
    assert "contract_status" not in job.payload


def test_memory_job_endpoint_hides_non_memory_jobs() -> None:
    job_id = uuid.uuid4()
    client = _build_app(
        FakeSession(
            jobs={
                job_id: Job(
                    id=job_id,
                    item_id=uuid.uuid4(),
                    job_type="note",
                    tenant_id="tenant-a",
                    status="completed",
                    progress=100,
                    created_at=datetime.now(timezone.utc),
                )
            }
        )
    )

    response = client.get(f"/api/v1/memory/jobs/{job_id}")

    assert response.status_code == 404


def test_memory_jobs_list_returns_memory_jobs_only_and_maps_complete_filter() -> None:
    completed_id = uuid.uuid4()
    queued_id = uuid.uuid4()
    client = _build_app(
        FakeSession(
            jobs={
                completed_id: Job(
                    id=completed_id,
                    item_id=uuid.uuid4(),
                    job_type=MEMORY_JOB_TYPE,
                    tenant_id="tenant-a",
                    status="completed",
                    progress=100,
                    created_at=datetime.now(timezone.utc),
                    completed_at=datetime.now(timezone.utc),
                ),
                queued_id: Job(
                    id=queued_id,
                    item_id=uuid.uuid4(),
                    job_type=MEMORY_JOB_TYPE,
                    tenant_id="tenant-a",
                    status="queued",
                    progress=0,
                    created_at=datetime.now(timezone.utc),
                ),
                uuid.uuid4(): Job(
                    id=uuid.uuid4(),
                    item_id=uuid.uuid4(),
                    job_type="note",
                    tenant_id="tenant-a",
                    status="failed",
                    progress=100,
                    created_at=datetime.now(timezone.utc),
                ),
            }
        )
    )

    response = client.get("/api/v1/memory/jobs?status=complete")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert [job["job_id"] for job in payload["jobs"]] == [str(completed_id)]
    assert payload["jobs"][0]["status"] == "complete"
    assert payload["jobs"][0]["contract_status"] == "completed"


def test_memory_jobs_list_rejects_unknown_status_filter() -> None:
    client = _build_app(FakeSession())

    response = client.get("/api/v1/memory/jobs?status=unknown")

    assert response.status_code == 422
    assert "Unsupported memory job status filter" in response.json()["detail"]


def test_memory_job_retry_requeues_failed_job() -> None:
    job_id = uuid.uuid4()
    item_id = uuid.uuid4()
    item = SimpleNamespace(id=item_id, raw_content="Recovered note", status="failed", updated_at=None)
    job = Job(
        id=job_id,
        item_id=item_id,
        job_type=MEMORY_JOB_TYPE,
        tenant_id="tenant-a",
        status="failed",
        progress=100,
        error_message="boom",
        payload={"contract_status": "dependency_unavailable"},
        created_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
    )
    session = FakeSession(jobs={job_id: job, item_id: item})
    client = _build_app(session)

    response = client.post(f"/api/v1/memory/jobs/{job_id}/retry")

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert response.json()["contract_status"] == "queued"
    assert job.progress == 0
    assert job.error_message is None
    assert "contract_status" not in job.payload
    assert job.completed_at is None
    assert item.status == "processing"
    assert client.app.state.arq_pool.enqueued == [("memory_artifact", {"job_id": str(job_id)})]


@pytest.mark.asyncio
async def test_dependency_unavailable_integrity_replay_requeues_existing_job() -> None:
    item_id = uuid.uuid4()
    entry = memory_service.normalize_memory_entry(MemoryEntryRequest.model_validate(_canonical_payload()))
    job = Job(
        id=uuid.uuid4(),
        item_id=item_id,
        job_type=MEMORY_JOB_TYPE,
        tenant_id="tenant-a",
        status="failed",
        progress=100,
        error_message="Memory queue unavailable; retry the memory job after dependency recovery",
        payload={"contract_status": "dependency_unavailable", "relationship_policy": "immediate"},
        created_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
    )
    item = Item(
        id=item_id,
        tenant_id="tenant-a",
        title=entry.title,
        summary=entry.summary,
        source_type="note",
        source_url=entry.source_url,
        raw_content=entry.body,
        tags=entry.tags,
        status="processing",
        created_at=entry.created_at,
        updated_at=entry.created_at,
        metadata_=entry.metadata,
        idempotency_key=entry.idempotency_key,
    )

    class RaceSession(FakeSession):
        def __init__(self) -> None:
            super().__init__(jobs={job.id: job, item_id: item})
            self.scalar_calls = 0
            self.rollbacks = 0

        async def scalar(self, statement):
            self.scalar_calls += 1
            return None if self.scalar_calls == 1 else job

        def add(self, value) -> None:
            return None

        async def flush(self) -> None:
            raise IntegrityError("insert", {}, Exception("duplicate"))

        async def rollback(self) -> None:
            self.rollbacks += 1

    session = RaceSession()

    result = await memory_service.accept_memory_entry(
        session,
        entry=entry,
        signing_key="key-hash",
    )

    assert result.job is job
    assert result.enqueue_requested is True
    assert session.rollbacks == 1
    assert job.status == "queued"
    assert "contract_status" not in job.payload


def test_memory_job_retry_rejects_missing_note_content() -> None:
    job_id = uuid.uuid4()
    item_id = uuid.uuid4()
    item = SimpleNamespace(id=item_id, raw_content=None, status="failed", updated_at=None)
    job = Job(
        id=job_id,
        item_id=item_id,
        job_type=MEMORY_JOB_TYPE,
        tenant_id="tenant-a",
        status="failed",
        progress=100,
        created_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
    )
    client = _build_app(FakeSession(jobs={job_id: job, item_id: item}))

    response = client.post(f"/api/v1/memory/jobs/{job_id}/retry")

    assert response.status_code == 409
    assert "re-submit the memory entry" in response.json()["detail"]


def test_memory_retrieve_returns_trace_and_scope(monkeypatch) -> None:
    client = _build_app(FakeSession())

    async def fake_retrieve_memory(db, *, embedder, tenant_id: str, body):
        assert tenant_id == "tenant-a"
        assert body.scope.type == "workspace"
        assert body.scope.key == "launch-pad"
        assert body.candidate_limit == 40
        return MemoryRetrieveResponse(
            scope=body.scope,
            routed_room_id=None,
            redirected_from_room_id=None,
            trace=PalaceRetrieveTrace(
                requested_scope_type="workspace",
                requested_scope_key="launch-pad",
                selected_wing="Product / Growth",
                candidate_rooms=["Launch Briefs"],
                expanded_rooms=[],
                fallback_used=True,
                completeness_warning="Global fallback used because room-scoped retrieval had low confidence.",
                steps=[],
                ranking_traces=[
                    {
                        "route": "room_scoped",
                        "ranking_features_version": 1,
                        "source_ranking_enabled": True,
                        "candidate_limit": 20,
                        "candidate_count": 1,
                        "result_count": 1,
                        "routing": {
                            "scope_type": "workspace",
                            "scope_key": "launch-pad",
                            "fallback_used": False,
                        },
                        "results": [
                            {
                                "rank": 1,
                                "item_id": uuid.UUID("00000000-0000-0000-0000-000000000021"),
                                "source_type": "note",
                                "base_score": 0.82,
                                "adjusted_score": 0.88,
                                "adjustments": {"lexical_rescue": 0.06},
                            }
                        ],
                    }
                ],
            ),
            results=[
                SearchResult(
                    item_id=uuid.uuid4(),
                    title="Shared launch brief",
                    summary="Cross-host context",
                    source_type="note",
                    source_url=None,
                    tags=["launch"],
                    source_project="launch-pad",
                    created_at=datetime.now(timezone.utc),
                    chunk_text="Agents should reuse the same launch brief when they migrate hosts.",
                    chunk_index=0,
                    score=0.88,
                )
            ],
            total=1,
        )

    monkeypatch.setattr("app.api.memory.retrieve_memory", fake_retrieve_memory)

    response = client.post(
        "/api/v1/memory/retrieve",
        json={
            "query": "launch brief",
            "limit": 5,
            "candidate_limit": 40,
            "scope": {"type": "workspace", "key": "launch-pad"},
        },
    )

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["results"][0]["source_project"] == "launch-pad"
    assert response.json()["trace"]["requested_scope_type"] == "workspace"
    assert response.json()["trace"]["fallback_used"] is True
    ranking_trace = response.json()["trace"]["ranking_traces"][0]
    assert ranking_trace["route"] == "room_scoped"
    assert ranking_trace["routing"]["scope_key"] == "launch-pad"
    assert ranking_trace["results"][0] == {
        "rank": 1,
        "item_id": "00000000-0000-0000-0000-000000000021",
        "source_type": "note",
        "base_score": 0.82,
        "adjusted_score": 0.88,
        "adjustments": {"lexical_rescue": 0.06},
        "derived_artifact_keys": [],
    }


def test_agent_memory_retrieve_uses_policy_request(monkeypatch, caplog) -> None:
    item_id = uuid.uuid4()
    client = _build_app(FakeSession())

    async def fake_retrieve_agent_memory(db, *, embedder, tenant_id: str, body, delegated_policy=None):
        assert tenant_id == "tenant-a"
        assert delegated_policy is None
        assert body.agent_scope_key == "orchestrator"
        assert body.include_agent_scope_keys == ["security-agent"]
        assert body.include_all_permitted_agent_scopes is True
        assert body.access_reason == "assemble delegated agent context"
        assert body.workspace_scope_keys == ["exampleos"]
        assert body.include_tenant_shared is True
        assert body.tenant_shared_policy == "fallback_only"
        assert body.include_broad_corpus is True
        assert body.broad_corpus_policy == "enabled"
        assert body.workspace_strict is True
        assert body.candidate_limit == 20
        assert body.broad_candidate_limit == 30
        assert body.display_limit == 8
        assert body.context_budget_chars == 4000
        return AgentMemoryRetrieveResponse(
            scopes=[
                {"type": "agent", "key": "orchestrator"},
                {"type": "workspace", "key": "exampleos"},
                {"type": "tenant_shared"},
            ],
            trace=AgentMemoryRetrieveTrace(
                searched_scopes=[
                    {"type": "agent", "key": "orchestrator"},
                    {"type": "workspace", "key": "exampleos"},
                    {"type": "tenant_shared"},
                ],
                broad_corpus_searched=True,
                workspace_strict=True,
                workspace_scope_exhausted=False,
                tenant_shared_policy="fallback_only",
                tenant_shared_fallback_used=False,
                broad_corpus_policy="enabled",
                excluded_scope_types=["agent", "workspace", "session"],
            ),
            results=[
                SearchResult(
                    item_id=item_id,
                    title="ExampleOS workspace memory",
                    summary="Route discovery found workspace context.",
                    source_type="note",
                    source_url=None,
                    tags=["codex-memory"],
                    created_at=datetime.now(timezone.utc),
                    chunk_text="workspace/exampleos context",
                    chunk_index=0,
                    score=0.93,
                )
            ],
            total=1,
        )

    monkeypatch.setattr("app.api.memory.retrieve_agent_memory", fake_retrieve_agent_memory)

    with caplog.at_level(logging.INFO, logger="app.api.memory"):
        response = client.post(
            "/api/v1/memory/retrieve-agent",
            json={
                "query": "available ExampleOS memory",
                "agent_scope_key": "orchestrator",
                "include_agent_scope_keys": ["security-agent"],
                "include_all_permitted_agent_scopes": True,
                "access_reason": "assemble delegated agent context",
                "workspace_scope_keys": ["exampleos", "exampleos"],
                "workspace_strict": True,
                "tenant_shared_policy": "fallback_only",
                "broad_corpus_policy": "enabled",
                "limit": 5,
                "candidate_limit": 20,
                "broad_candidate_limit": 30,
                "display_limit": 8,
                "context_budget_chars": 4000,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["trace"]["broad_corpus_searched"] is True
    assert payload["trace"]["excluded_scope_types"] == ["agent", "workspace", "session"]
    assert payload["trace"]["context_budget_chars"] == 4000
    assert payload["results"][0]["item_id"] == str(item_id)
    assert "memory retrieval diagnostics" in caplog.text
    assert "/api/v1/memory/retrieve-agent" in caplog.text
    assert "searched_scope_count" in caplog.text
    assert "context_budget_chars" in caplog.text
    assert "available ExampleOS memory" not in caplog.text
    assert "workspace/exampleos context" not in caplog.text


def test_agent_memory_retrieve_passes_configured_delegated_policy(monkeypatch) -> None:
    client = _build_app(FakeSession(), tenant_id="default")
    raw_policies = json.dumps(
        [
            {
                "tenant_id": "default",
                "subject_agent_scope_key": "orchestrator",
                "read_agent_scope_keys": ["security", "macos"],
                "policy_id": "example-lab-hermes-orchestrator",
                "policy_source": "argocd/palaceoftruth",
                "require_access_reason": True,
                "max_cross_agent_scopes": 2,
                "max_results_per_scope": 10,
            }
        ]
    )
    monkeypatch.setattr(
        "app.api.memory.settings.palaceoftruth_delegated_agent_memory_read_policies",
        raw_policies,
    )

    async def fake_retrieve_agent_memory(db, *, embedder, tenant_id: str, body, delegated_policy=None):
        assert tenant_id == "default"
        assert delegated_policy is not None
        assert delegated_policy.tenant_id == "default"
        assert delegated_policy.subject_agent_scope_key == "orchestrator"
        assert delegated_policy.read_agent_scope_keys == ("security", "macos")
        assert delegated_policy.max_cross_agent_scopes == 2
        assert delegated_policy.max_results_per_scope == 10
        return AgentMemoryRetrieveResponse(
            scopes=[{"type": "agent", "key": "orchestrator"}],
            trace=AgentMemoryRetrieveTrace(
                searched_scopes=[{"type": "agent", "key": "orchestrator"}],
                caller_agent_scope_key="orchestrator",
                requested_agent_scope_keys=["security", "macos"],
                authorized_agent_scope_keys=["security", "macos"],
                delegated_agent_policy_id="example-lab-hermes-orchestrator",
                delegated_agent_policy_source="argocd/palaceoftruth",
                delegated_agent_decision="allowed",
            ),
            results=[],
            total=0,
        )

    monkeypatch.setattr("app.api.memory.retrieve_agent_memory", fake_retrieve_agent_memory)

    response = client.post(
        "/api/v1/memory/retrieve-agent",
        json={
            "query": "policy memory",
            "agent_scope_key": "orchestrator",
            "include_agent_scope_keys": ["security", "macos"],
            "access_reason": "assemble delegated agent context",
        },
    )

    assert response.status_code == 200
    assert response.json()["trace"]["authorized_agent_scope_keys"] == ["security", "macos"]


def test_delegated_agent_memory_policy_config_ignores_other_callers() -> None:
    raw_policies = json.dumps(
        [
            {
                "tenant_id": "default",
                "subject_agent_scope_key": "orchestrator",
                "read_agent_scope_keys": ["security", "macos", "security"],
            }
        ]
    )

    assert (
        delegated_agent_memory_policy_from_config(
            tenant_id="default",
            agent_scope_key="security",
            raw_policies=raw_policies,
        )
        is None
    )

    policy = delegated_agent_memory_policy_from_config(
        tenant_id="default",
        agent_scope_key="orchestrator",
        raw_policies=raw_policies,
    )

    assert policy is not None
    assert policy.read_agent_scope_keys == ("security", "macos")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("max_cross_agent_scopes", -1, "max_cross_agent_scopes must be at least 0"),
        ("max_results_per_scope", 0, "max_results_per_scope must be at least 1"),
    ],
)
def test_delegated_agent_memory_policy_config_rejects_invalid_caps(
    field: str,
    value: int,
    message: str,
) -> None:
    raw_policies = json.dumps(
        [
            {
                "tenant_id": "default",
                "subject_agent_scope_key": "orchestrator",
                "read_agent_scope_keys": ["security"],
                field: value,
            }
        ]
    )

    with pytest.raises(ValueError, match=message):
        parse_delegated_agent_memory_read_policies(raw_policies)


def test_agent_memory_retrieve_workspace_strict_requires_workspace_scope() -> None:
    client = _build_app(FakeSession())

    response = client.post(
        "/api/v1/memory/retrieve-agent",
        json={
            "query": "strict workspace memory",
            "workspace_strict": True,
        },
    )

    assert response.status_code == 422
    assert "workspace_strict requires at least one workspace_scope_key" in response.text


@pytest.mark.asyncio
async def test_agent_memory_strict_workspace_does_not_leak_other_project_memory(
    monkeypatch,
) -> None:
    searched_scopes: list[tuple[str, str | None]] = []

    async def fake_retrieve_memory(db, *, embedder, tenant_id: str, body, query_vector=None):
        del db, embedder, tenant_id, query_vector
        searched_scopes.append((body.scope.type, body.scope.key))
        results = []
        if body.scope.type == "workspace" and body.scope.key == "project-b":
            results = [
                _memory_result(
                    "00000000-0000-0000-0000-0000000000b2",
                    "Project B release note",
                    "Project B deploy checklist only.",
                    workspace_key="project-b",
                    score=0.91,
                )
            ]
        if body.scope.type == "workspace" and body.scope.key == "project-a":
            results = [
                _memory_result(
                    "00000000-0000-0000-0000-0000000000a1",
                    "Project A secret note",
                    "Project A unique memory must not appear for Project B.",
                    workspace_key="project-a",
                    score=0.99,
                )
            ]
        return MemoryRetrieveResponse(
            scope=body.scope,
            trace=PalaceRetrieveTrace(
                requested_scope_type=body.scope.type,
                requested_scope_key=body.scope.key,
                fallback_used=False,
            ),
            results=results,
            total=len(results),
        )

    class FailingSearchService:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("strict workspace retrieval must not instantiate broad search")

    monkeypatch.setattr(memory_service, "retrieve_memory", fake_retrieve_memory)
    monkeypatch.setattr(memory_service, "SearchService", FailingSearchService)

    response = await memory_service.retrieve_agent_memory(
        FakeSession(),
        embedder=object(),
        tenant_id="tenant-a",
        body=AgentMemoryRetrieveRequest(
            query="release note",
            agent_scope_key="orchestrator",
            workspace_scope_keys=["project-b"],
            include_tenant_shared=True,
            tenant_shared_policy="fallback_only",
            include_broad_corpus=True,
            broad_corpus_policy="disabled",
            workspace_strict=True,
            limit=5,
        ),
    )

    assert searched_scopes == [("workspace", "project-b")]
    assert response.trace.workspace_strict is True
    assert response.trace.tenant_shared_fallback_used is False
    assert response.trace.broad_corpus_searched is False
    assert response.trace.broad_corpus_skipped_reason == "disabled_by_request"
    assert response.results[0].source_project == "project-b"
    assert "Project A" not in response.model_dump_json()


@pytest.mark.asyncio
async def test_delegated_agent_memory_without_policy_denies_requested_agent_scopes(
    monkeypatch,
) -> None:
    searched_scopes: list[tuple[str, str | None]] = []
    broad_calls: list[dict] = []

    async def fake_retrieve_memory(db, *, embedder, tenant_id: str, body, query_vector=None):
        del db, embedder, tenant_id, query_vector
        searched_scopes.append((body.scope.type, body.scope.key))
        return MemoryRetrieveResponse(
            scope=body.scope,
            trace=PalaceRetrieveTrace(
                requested_scope_type=body.scope.type,
                requested_scope_key=body.scope.key,
                fallback_used=False,
            ),
            results=[],
            total=0,
        )

    class BroadSearchService:
        def __init__(self, db, embedder, *, tenant_id: str) -> None:
            del db, embedder
            assert tenant_id == "tenant-a"

        async def vector_search(self, **kwargs):
            broad_calls.append(kwargs)
            return []

    monkeypatch.setattr(memory_service, "retrieve_memory", fake_retrieve_memory)
    monkeypatch.setattr(memory_service, "SearchService", BroadSearchService)

    response = await memory_service.retrieve_agent_memory(
        FakeSession(),
        embedder=object(),
        tenant_id="tenant-a",
        body=AgentMemoryRetrieveRequest(
            query="security memory",
            agent_scope_key="orchestrator",
            include_agent_scope_keys=["security-agent"],
            include_broad_corpus=True,
            broad_corpus_policy="enabled",
        ),
    )

    assert searched_scopes == [("agent", "orchestrator"), ("tenant_shared", None)]
    assert response.trace.delegated_agent_decision == "denied"
    assert response.trace.requested_agent_scope_keys == ["security-agent"]
    assert response.trace.authorized_agent_scope_keys == []
    assert response.trace.denied_agent_scope_keys == ["security-agent"]
    assert response.trace.delegated_agent_deny_reasons == ["no_delegated_agent_policy"]
    assert broad_calls
    assert broad_calls[0]["exclude_private_memory_scopes"] is True
    assert response.trace.broad_corpus_searched is True
    assert response.trace.broad_corpus_skipped_reason is None


@pytest.mark.asyncio
async def test_delegated_agent_memory_policy_adds_allowlisted_agent_scope(
    monkeypatch,
) -> None:
    searched_scopes: list[tuple[str, str | None, int]] = []

    async def fake_retrieve_memory(db, *, embedder, tenant_id: str, body, query_vector=None):
        del db, embedder, query_vector
        assert tenant_id == "tenant-a"
        searched_scopes.append((body.scope.type, body.scope.key, body.limit))
        results = []
        if body.scope.type == "agent" and body.scope.key == "security-agent":
            results = [
                _agent_memory_result(
                    "00000000-0000-0000-0000-0000000000d1",
                    "Security specialist note",
                    "Specialist memory approved for orchestrator read.",
                    agent_key="security-agent",
                    score=0.95,
                )
            ]
        return MemoryRetrieveResponse(
            scope=body.scope,
            trace=PalaceRetrieveTrace(
                requested_scope_type=body.scope.type,
                requested_scope_key=body.scope.key,
                fallback_used=False,
            ),
            results=results,
            total=len(results),
        )

    monkeypatch.setattr(memory_service, "retrieve_memory", fake_retrieve_memory)

    response = await memory_service.retrieve_agent_memory(
        FakeSession(),
        embedder=object(),
        tenant_id="tenant-a",
        delegated_policy=DelegatedAgentMemoryReadPolicy(
            tenant_id="tenant-a",
            subject_agent_scope_key="orchestrator",
            policy_id="policy-orchestrator",
            policy_source="test-policy",
            read_agent_scope_keys=("security-agent",),
            max_results_per_scope=1,
        ),
        body=AgentMemoryRetrieveRequest(
            query="security memory",
            agent_scope_key="orchestrator",
            include_agent_scope_keys=["security-agent"],
            access_reason="assemble incident context",
            include_tenant_shared=False,
            include_broad_corpus=False,
            limit=5,
        ),
    )

    assert searched_scopes == [("agent", "orchestrator", 20), ("agent", "security-agent", 1)]
    assert response.trace.delegated_agent_decision == "allowed"
    assert response.trace.delegated_agent_policy_id == "policy-orchestrator"
    assert response.trace.delegated_agent_policy_source == "test-policy"
    assert response.trace.authorized_agent_scope_keys == ["security-agent"]
    assert response.trace.denied_agent_scope_keys == []
    assert response.trace.result_counts_by_scope == {
        "agent/orchestrator": 0,
        "agent/security-agent": 1,
    }
    assert response.results[0].title == "Security specialist note"


@pytest.mark.asyncio
async def test_agent_scope_pattern_selects_bounded_policy_authorized_scopes(
    monkeypatch,
) -> None:
    searched_scopes: list[tuple[str, str | None]] = []

    async def fake_list_memory_scopes(db, *, tenant_id: str, limit: int = 50, sample_limit: int = 8):
        del db, limit, sample_limit
        assert tenant_id == "tenant-a"
        return MemoryScopeListResponse(
            scopes=[
                MemoryScopeSummary(
                    scope={"type": "agent", "key": "orchestrator"},
                    entry_count=1,
                    tags=["scope-agent"],
                ),
                MemoryScopeSummary(
                    scope={"type": "agent", "key": "security"},
                    entry_count=3,
                    tags=["security", "incident-response"],
                ),
                MemoryScopeSummary(
                    scope={"type": "agent", "key": "macos"},
                    entry_count=2,
                    tags=["macos", "runner"],
                ),
                MemoryScopeSummary(
                    scope={"type": "agent", "key": "frontend"},
                    entry_count=4,
                    tags=["ui"],
                ),
            ],
            total=4,
            limit=100,
        )

    async def fake_retrieve_memory(db, *, embedder, tenant_id: str, body, query_vector=None):
        del db, embedder, tenant_id, query_vector
        searched_scopes.append((body.scope.type, body.scope.key))
        results = []
        if body.scope.type == "agent" and body.scope.key == "security":
            results = [
                _agent_memory_result(
                    "00000000-0000-0000-0000-0000000000e1",
                    "Security wildcard match",
                    "Security-scoped memory selected from a pattern.",
                    agent_key="security",
                    score=0.95,
                )
            ]
        return MemoryRetrieveResponse(
            scope=body.scope,
            trace=PalaceRetrieveTrace(
                requested_scope_type=body.scope.type,
                requested_scope_key=body.scope.key,
                fallback_used=False,
            ),
            results=results,
            total=len(results),
        )

    monkeypatch.setattr(memory_service, "list_memory_scopes", fake_list_memory_scopes)
    monkeypatch.setattr(memory_service, "retrieve_memory", fake_retrieve_memory)

    response = await memory_service.retrieve_agent_memory(
        FakeSession(),
        embedder=object(),
        tenant_id="tenant-a",
        delegated_policy=DelegatedAgentMemoryReadPolicy(
            tenant_id="tenant-a",
            subject_agent_scope_key="orchestrator",
            policy_id="policy-orchestrator",
            policy_source="test-policy",
            read_agent_scope_keys=("security", "macos", "frontend"),
            max_cross_agent_scopes=2,
        ),
        body=AgentMemoryRetrieveRequest(
            query="security macos recovery",
            agent_scope_key="orchestrator",
            include_agent_scope_patterns=["agent/*"],
            agent_scope_pattern_limit=2,
            access_reason="assemble delegated agent context",
            include_tenant_shared=False,
            include_broad_corpus=False,
            limit=3,
        ),
    )

    assert searched_scopes == [
        ("agent", "orchestrator"),
        ("agent", "security"),
        ("agent", "macos"),
    ]
    assert response.trace.requested_agent_scope_patterns == ["agent/*"]
    assert response.trace.discovered_agent_scope_keys == [
        "orchestrator",
        "security",
        "macos",
        "frontend",
    ]
    assert response.trace.selected_agent_scope_keys == ["security", "macos"]
    assert response.trace.skipped_agent_scope_keys == ["orchestrator", "frontend"]
    assert response.trace.agent_scope_pattern_truncated is True
    assert "caller_agent_scope_excluded" in response.trace.agent_scope_pattern_skip_reasons
    assert "agent_scope_pattern_limit_exceeded" in response.trace.agent_scope_pattern_skip_reasons
    assert response.trace.authorized_agent_scope_keys == ["security", "macos"]
    assert response.trace.denied_agent_scope_keys == []
    assert response.results[0].retrieved_scope_label == "agent/security"


@pytest.mark.asyncio
async def test_agent_scope_pattern_denies_unauthorized_matches_without_searching_them(
    monkeypatch,
) -> None:
    searched_scopes: list[tuple[str, str | None]] = []

    async def fake_list_memory_scopes(db, *, tenant_id: str, limit: int = 50, sample_limit: int = 8):
        del db, tenant_id, limit, sample_limit
        return MemoryScopeListResponse(
            scopes=[
                MemoryScopeSummary(
                    scope={"type": "agent", "key": "security"},
                    entry_count=1,
                    tags=["security"],
                )
            ],
            total=1,
            limit=100,
        )

    async def fake_retrieve_memory(db, *, embedder, tenant_id: str, body, query_vector=None):
        del db, embedder, tenant_id, query_vector
        searched_scopes.append((body.scope.type, body.scope.key))
        return MemoryRetrieveResponse(
            scope=body.scope,
            trace=PalaceRetrieveTrace(
                requested_scope_type=body.scope.type,
                requested_scope_key=body.scope.key,
                fallback_used=False,
            ),
            results=[],
            total=0,
        )

    monkeypatch.setattr(memory_service, "list_memory_scopes", fake_list_memory_scopes)
    monkeypatch.setattr(memory_service, "retrieve_memory", fake_retrieve_memory)

    response = await memory_service.retrieve_agent_memory(
        FakeSession(),
        embedder=object(),
        tenant_id="tenant-a",
        delegated_policy=DelegatedAgentMemoryReadPolicy(
            tenant_id="tenant-a",
            subject_agent_scope_key="orchestrator",
            policy_id="policy-orchestrator",
            policy_source="test-policy",
            read_agent_scope_keys=("macos",),
        ),
        body=AgentMemoryRetrieveRequest(
            query="security incident context",
            agent_scope_key="orchestrator",
            include_agent_scope_patterns=["agent/*"],
            access_reason="assemble delegated agent context",
            include_tenant_shared=False,
            include_broad_corpus=False,
        ),
    )

    assert searched_scopes == [("agent", "orchestrator")]
    assert response.trace.requested_agent_scope_keys == ["security"]
    assert response.trace.matched_agent_scope_keys == ["security"]
    assert response.trace.selected_agent_scope_keys == ["security"]
    assert response.trace.authorized_agent_scope_keys == []
    assert response.trace.denied_agent_scope_keys == ["security"]
    assert response.trace.delegated_agent_decision == "denied"
    assert "agent_scope_not_allowlisted" in response.trace.delegated_agent_deny_reasons


@pytest.mark.asyncio
async def test_delegated_agent_memory_results_rank_ahead_of_broad_fallback(
    monkeypatch,
) -> None:
    searched_scopes: list[tuple[str, str | None]] = []

    async def fake_retrieve_memory(db, *, embedder, tenant_id: str, body, query_vector=None):
        del db, embedder, tenant_id, query_vector
        searched_scopes.append((body.scope.type, body.scope.key))
        results = []
        warning = None
        if body.scope.type == "agent" and body.scope.key == "security":
            results = [
                _agent_memory_result(
                    "00000000-0000-0000-0000-0000000000d2",
                    "Security delegated runbook",
                    "Use the delegated security memory before generic media notes.",
                    agent_key="security",
                    score=0.62,
                )
            ]
            warning = "Global fallback used because room-scoped retrieval had low confidence."
        if body.scope.type == "agent" and body.scope.key == "orchestrator":
            results = [
                _agent_memory_result(
                    "00000000-0000-0000-0000-0000000000d1",
                    "Orchestrator policy note",
                    "A high-scoring caller memory should not outrank requested delegated scopes.",
                    agent_key="orchestrator",
                    score=0.99,
                )
            ]
        if body.scope.type == "agent" and body.scope.key == "macos":
            results = [
                _agent_memory_result(
                    "00000000-0000-0000-0000-0000000000d3",
                    "macOS delegated runbook",
                    "Use the delegated macOS memory before generic media notes.",
                    agent_key="macos",
                    score=0.57,
                )
            ]
        return MemoryRetrieveResponse(
            scope=body.scope,
            trace=PalaceRetrieveTrace(
                requested_scope_type=body.scope.type,
                requested_scope_key=body.scope.key,
                fallback_used=warning is not None,
                completeness_warning=warning,
            ),
            results=results,
            total=len(results),
        )

    class BroadSearchService:
        def __init__(self, db, embedder, *, tenant_id: str) -> None:
            del db, embedder
            assert tenant_id == "tenant-a"

        async def vector_search(self, **kwargs):
            assert kwargs["exclude_private_memory_scopes"] is True
            return [
                SearchResult(
                    item_id=uuid.UUID("00000000-0000-0000-0000-0000000000b9"),
                    title="Generic media transcript",
                    summary=None,
                    source_type="youtube",
                    source_url=None,
                    tags=["media", "agent-security"],
                    retrieved_scope_label="general",
                    created_at=datetime.now(timezone.utc),
                    chunk_text="A high-scoring generic item should not drown delegated agent memories.",
                    chunk_index=0,
                    score=0.99,
                )
            ]

    monkeypatch.setattr(memory_service, "retrieve_memory", fake_retrieve_memory)
    monkeypatch.setattr(memory_service, "SearchService", BroadSearchService)

    response = await memory_service.retrieve_agent_memory(
        FakeSession(),
        embedder=object(),
        tenant_id="tenant-a",
        delegated_policy=DelegatedAgentMemoryReadPolicy(
            tenant_id="tenant-a",
            subject_agent_scope_key="orchestrator",
            policy_id="hermes-orchestrator",
            policy_source="test-policy",
            read_agent_scope_keys=("security", "macos"),
        ),
        body=AgentMemoryRetrieveRequest(
            query="Hermes security and macOS recovery context",
            agent_scope_key="orchestrator",
            include_agent_scope_keys=["security", "macos"],
            access_reason="assemble delegated agent context",
            include_tenant_shared=False,
            include_broad_corpus=True,
            broad_corpus_policy="enabled",
            display_limit=3,
            limit=3,
        ),
    )

    assert searched_scopes == [
        ("agent", "orchestrator"),
        ("agent", "security"),
        ("agent", "macos"),
    ]
    assert response.trace.delegated_agent_decision == "allowed"
    assert response.trace.authorized_agent_scope_keys == ["security", "macos"]
    assert response.trace.denied_agent_scope_keys == []
    assert response.trace.result_counts_by_scope == {
        "agent/orchestrator": 1,
        "agent/security": 1,
        "agent/macos": 1,
    }
    assert response.trace.broad_corpus_searched is True
    assert response.trace.broad_result_count == 1
    assert response.trace.fallback_used is True
    assert response.trace.selected_scope_fallback_used is True
    assert response.trace.completeness_warnings == [
        "Selected scoped retrieval reported low route confidence."
    ]
    assert response.results[0].retrieved_scope_label == "agent/security"
    assert response.results[1].retrieved_scope_label == "agent/macos"
    assert response.results[2].retrieved_scope_label == "agent/orchestrator"


@pytest.mark.asyncio
async def test_delegated_agent_memory_scoped_only_trace_avoids_global_fallback_wording(
    monkeypatch,
) -> None:
    async def fake_retrieve_memory(db, *, embedder, tenant_id: str, body, query_vector=None):
        del db, embedder, tenant_id, query_vector
        results = []
        warning = None
        if body.scope.type == "agent" and body.scope.key == "orchestrator":
            results = [
                _agent_memory_result(
                    "00000000-0000-0000-0000-0000000000d1",
                    "Orchestrator delegated-policy note",
                    "Caller-scope policy investigation memory.",
                    agent_key="orchestrator",
                    score=0.99,
                )
            ]
        if body.scope.type == "agent" and body.scope.key == "security":
            results = [
                _agent_memory_result(
                    "00000000-0000-0000-0000-0000000000d2",
                    "Security specialist policy note",
                    "Specialist security memory should be preferred for delegated recall.",
                    agent_key="security",
                    score=0.61,
                )
            ]
            warning = "Global fallback used because room-scoped retrieval had low confidence."
        return MemoryRetrieveResponse(
            scope=body.scope,
            trace=PalaceRetrieveTrace(
                requested_scope_type=body.scope.type,
                requested_scope_key=body.scope.key,
                fallback_used=warning is not None,
                completeness_warning=warning,
            ),
            results=results,
            total=len(results),
        )

    monkeypatch.setattr(memory_service, "retrieve_memory", fake_retrieve_memory)

    response = await memory_service.retrieve_agent_memory(
        FakeSession(),
        embedder=object(),
        tenant_id="tenant-a",
        delegated_policy=DelegatedAgentMemoryReadPolicy(
            tenant_id="tenant-a",
            subject_agent_scope_key="orchestrator",
            policy_id="hermes-orchestrator",
            policy_source="test-policy",
            read_agent_scope_keys=("security",),
        ),
        body=AgentMemoryRetrieveRequest(
            query="Hermes delegated retrieval security policy trace fallback",
            agent_scope_key="orchestrator",
            include_agent_scope_keys=["security"],
            access_reason="assemble delegated agent context",
            include_tenant_shared=False,
            include_broad_corpus=False,
            display_limit=2,
            limit=2,
        ),
    )

    assert response.trace.broad_corpus_searched is False
    assert response.trace.broad_corpus_skipped_reason == "disabled_by_request"
    assert response.trace.selected_scope_fallback_used is True
    assert response.trace.completeness_warnings == [
        "Selected scoped retrieval reported low route confidence."
    ]
    assert all("Global fallback" not in warning for warning in response.trace.completeness_warnings)
    assert response.results[0].retrieved_scope_label == "agent/security"
    assert response.results[1].retrieved_scope_label == "agent/orchestrator"


@pytest.mark.asyncio
async def test_delegated_agent_memory_policy_reports_denied_scopes_without_querying_them(
    monkeypatch,
) -> None:
    searched_scopes: list[tuple[str, str | None]] = []
    broad_calls: list[dict] = []

    async def fake_retrieve_memory(db, *, embedder, tenant_id: str, body, query_vector=None):
        del db, embedder, tenant_id, query_vector
        searched_scopes.append((body.scope.type, body.scope.key))
        return MemoryRetrieveResponse(
            scope=body.scope,
            trace=PalaceRetrieveTrace(
                requested_scope_type=body.scope.type,
                requested_scope_key=body.scope.key,
                fallback_used=False,
            ),
            results=[],
            total=0,
        )

    class BroadSearchService:
        def __init__(self, db, embedder, *, tenant_id: str) -> None:
            del db, embedder
            assert tenant_id == "tenant-a"

        async def vector_search(self, **kwargs):
            broad_calls.append(kwargs)
            return []

    monkeypatch.setattr(memory_service, "retrieve_memory", fake_retrieve_memory)
    monkeypatch.setattr(memory_service, "SearchService", BroadSearchService)

    response = await memory_service.retrieve_agent_memory(
        FakeSession(),
        embedder=object(),
        tenant_id="tenant-a",
        delegated_policy=DelegatedAgentMemoryReadPolicy(
            tenant_id="tenant-a",
            read_agent_scope_keys=("security-agent",),
        ),
        body=AgentMemoryRetrieveRequest(
            query="specialist memory",
            agent_scope_key="orchestrator",
            include_agent_scope_keys=["security-agent", "frontend-agent"],
            access_reason="assemble agent team context",
            include_broad_corpus=True,
            broad_corpus_policy="enabled",
        ),
    )

    assert ("agent", "frontend-agent") not in searched_scopes
    assert ("agent", "security-agent") in searched_scopes
    assert response.trace.delegated_agent_decision == "partial"
    assert response.trace.authorized_agent_scope_keys == ["security-agent"]
    assert response.trace.denied_agent_scope_keys == ["frontend-agent"]
    assert response.trace.delegated_agent_deny_reasons == ["agent_scope_not_allowlisted"]
    assert broad_calls
    assert broad_calls[0]["exclude_private_memory_scopes"] is True
    assert response.trace.broad_corpus_searched is True
    assert response.trace.broad_corpus_skipped_reason is None


@pytest.mark.asyncio
async def test_delegated_agent_memory_policy_is_same_tenant_only(
    monkeypatch,
) -> None:
    searched_scopes: list[tuple[str, str | None]] = []

    async def fake_retrieve_memory(db, *, embedder, tenant_id: str, body, query_vector=None):
        del db, embedder, tenant_id, query_vector
        searched_scopes.append((body.scope.type, body.scope.key))
        return MemoryRetrieveResponse(
            scope=body.scope,
            trace=PalaceRetrieveTrace(
                requested_scope_type=body.scope.type,
                requested_scope_key=body.scope.key,
                fallback_used=False,
            ),
            results=[],
            total=0,
        )

    monkeypatch.setattr(memory_service, "retrieve_memory", fake_retrieve_memory)

    response = await memory_service.retrieve_agent_memory(
        FakeSession(),
        embedder=object(),
        tenant_id="tenant-a",
        delegated_policy=DelegatedAgentMemoryReadPolicy(
            tenant_id="tenant-b",
            read_agent_scope_keys=("security-agent",),
        ),
        body=AgentMemoryRetrieveRequest(
            query="security memory",
            agent_scope_key="orchestrator",
            include_agent_scope_keys=["security-agent"],
            access_reason="tenant mismatch should deny",
            include_broad_corpus=False,
        ),
    )

    assert ("agent", "security-agent") not in searched_scopes
    assert response.trace.delegated_agent_decision == "denied"
    assert response.trace.denied_agent_scope_keys == ["security-agent"]
    assert response.trace.delegated_agent_deny_reasons == ["policy_tenant_mismatch"]


@pytest.mark.asyncio
async def test_agent_memory_uses_tenant_shared_only_as_empty_workspace_fallback(
    monkeypatch,
) -> None:
    searched_scopes: list[tuple[str, str | None]] = []

    async def fake_retrieve_memory(db, *, embedder, tenant_id: str, body, query_vector=None):
        del db, embedder, tenant_id, query_vector
        searched_scopes.append((body.scope.type, body.scope.key))
        results = []
        if body.scope.type == "tenant_shared":
            results = [
                _memory_result(
                    "00000000-0000-0000-0000-0000000000c1",
                    "Shared fallback note",
                    "Tenant-shared context used only after the workspace is empty.",
                    score=0.72,
                )
            ]
        return MemoryRetrieveResponse(
            scope=body.scope,
            trace=PalaceRetrieveTrace(
                requested_scope_type=body.scope.type,
                requested_scope_key=body.scope.key,
                fallback_used=False,
            ),
            results=results,
            total=len(results),
        )

    monkeypatch.setattr(memory_service, "retrieve_memory", fake_retrieve_memory)

    response = await memory_service.retrieve_agent_memory(
        FakeSession(),
        embedder=object(),
        tenant_id="tenant-a",
        body=AgentMemoryRetrieveRequest(
            query="shared fallback",
            agent_scope_key="orchestrator",
            workspace_scope_keys=["project-c"],
            include_tenant_shared=True,
            tenant_shared_policy="fallback_only",
            include_broad_corpus=False,
            broad_corpus_policy="disabled",
            workspace_strict=True,
            limit=5,
        ),
    )

    assert searched_scopes == [("workspace", "project-c"), ("tenant_shared", None)]
    assert [scope.model_dump() for scope in response.trace.searched_scopes] == [
        {"type": "workspace", "key": "project-c"},
        {"type": "tenant_shared", "key": None},
    ]
    assert response.trace.tenant_shared_fallback_used is True
    assert response.trace.broad_corpus_searched is False
    assert response.results[0].title == "Shared fallback note"


def test_agent_memory_retrieve_capture_records_trace_without_raw_query(monkeypatch, tmp_path) -> None:
    capture_path = tmp_path / "memory-agent-capture.ndjson"
    monkeypatch.setattr("app.services.retrieval_capture.settings.retrieval_capture_enabled", True)
    monkeypatch.setattr("app.services.retrieval_capture.settings.retrieval_capture_path", str(capture_path))
    monkeypatch.setattr("app.services.retrieval_capture.settings.retrieval_capture_query_mode", "redacted")
    monkeypatch.setattr("app.services.retrieval_capture.settings.app_version", "test-sha")
    item_id = uuid.UUID("00000000-0000-0000-0000-000000000031")
    client = _build_app(FakeSession())

    async def fake_retrieve_agent_memory(db, *, embedder, tenant_id: str, body, delegated_policy=None):
        assert delegated_policy is None
        return AgentMemoryRetrieveResponse(
            scopes=[
                {"type": "agent", "key": "orchestrator"},
                {"type": "workspace", "key": "feedvalue"},
                {"type": "tenant_shared"},
            ],
            trace=AgentMemoryRetrieveTrace(
                searched_scopes=[
                    {"type": "agent", "key": "orchestrator"},
                    {"type": "workspace", "key": "feedvalue"},
                    {"type": "tenant_shared"},
                ],
                broad_corpus_searched=True,
                selected_scope_query_count=3,
                selected_scope_result_count=2,
                broad_result_count=1,
                deduped_result_count=2,
                selected_scope_duration_ms=111,
                broad_corpus_duration_ms=22,
                merge_duration_ms=3,
                total_duration_ms=136,
                fallback_used=True,
                context_budget_truncated=True,
                completeness_warnings=["Global fallback used."],
            ),
            results=[
                SearchResult(
                    item_id=item_id,
                    title="FeedValue route memory",
                    summary="Sensitive summary must not be captured",
                    source_type="note",
                    source_url=None,
                    tags=["feedvalue"],
                    created_at=datetime.now(timezone.utc),
                    chunk_text="Sensitive FeedValue body must not be captured",
                    chunk_index=0,
                    score=0.91,
                )
            ],
            total=1,
        )

    monkeypatch.setattr("app.api.memory.retrieve_agent_memory", fake_retrieve_agent_memory)

    response = client.post(
        "/api/v1/memory/retrieve-agent",
        json={
            "query": "secret feedvalue miss",
            "agent_scope_key": "orchestrator",
            "workspace_scope_keys": ["feedvalue"],
            "candidate_limit": 20,
            "broad_candidate_limit": 30,
            "display_limit": 8,
            "context_budget_chars": 4000,
        },
    )

    assert response.status_code == 200
    record_text = capture_path.read_text(encoding="utf-8")
    record = json.loads(record_text)
    assert record["endpoint"] == "/api/v1/memory/retrieve-agent"
    assert record["request"]["query_redacted"] == "<term:6> <term:9> <term:4>"
    assert record["request"]["workspace_scope_keys"] == ["feedvalue"]
    assert record["trace"]["searched_scopes"][1] == {"type": "workspace", "key": "feedvalue"}
    assert record["trace"]["selected_scope_result_count"] == 2
    assert record["trace"]["context_budget_chars"] == 4000
    assert record["trace"]["context_budget_truncated"] is True
    assert record["results"][0]["item_id"] == str(item_id)
    assert "secret feedvalue miss" not in record_text
    assert "Sensitive FeedValue body" not in record_text
    assert "Sensitive summary" not in record_text


def test_memory_retrieval_doctor_returns_redacted_diagnostics(monkeypatch) -> None:
    client = _build_app(FakeSession())
    item_id = uuid.uuid4()

    async def fake_build_memory_retrieval_doctor(db, *, embedder, tenant_id: str, body, auth, arq_pool):
        assert tenant_id == "tenant-a"
        assert isinstance(body, MemoryRetrievalDoctorRequest)
        assert body.sample_probes[0].query == "secret launch phrase"
        assert isinstance(auth, MemoryRetrievalDoctorAuthShape)
        assert arq_pool is not None
        return MemoryRetrievalDoctorResponse(
            status="ok",
            tenant_id=tenant_id,
            auth=auth,
            selected_scopes=[{"type": "agent", "key": "codex"}, {"type": "tenant_shared"}],
            probes=[
                MemoryRetrievalDoctorProbeReport(
                    probe_index=0,
                    query_fingerprint="redacted12345678",
                    scope={"type": "agent", "key": "codex"},
                    status="ok",
                    route_confidence="high",
                    selected_scope_result_count=1,
                    deduped_result_count=1,
                    top_results=[
                        MemoryRetrievalDoctorProbeTopResult(
                            rank=1,
                            item_id=item_id,
                            source_type="note",
                            score=0.91,
                            tags=["manual-test"],
                            expected_match=True,
                        )
                    ],
                    expected_top_rank=1,
                )
            ],
            checks=[MemoryRetrievalDoctorCheck(name="probe_0", status="ok")],
        )

    monkeypatch.setattr("app.api.memory.build_memory_retrieval_doctor", fake_build_memory_retrieval_doctor)

    response = client.post(
        "/api/v1/memory/retrieval-doctor",
        json={
            "agent_scope_key": "codex",
            "sample_probes": [
                {
                    "query": "secret launch phrase",
                    "scope": {"type": "agent", "key": "codex"},
                    "expected_item_ids": [str(item_id)],
                }
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["tenant_id"] == "tenant-a"
    assert payload["probes"][0]["query_fingerprint"] == "redacted12345678"
    assert payload["probes"][0]["top_results"][0]["item_id"] == str(item_id)
    assert "secret launch phrase" not in json.dumps(payload)
    assert "chunk_text" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_memory_retrieval_doctor_builds_healthy_report(monkeypatch) -> None:
    item_id = uuid.uuid4()

    async def fake_queue_health(_arq_pool):
        return PalaceWorkerBackpressureSummary(generated_at=datetime.now(timezone.utc), queues=[])

    async def fake_wakeup_summary(*_args, **_kwargs):
        return {"fresh": 1, "stale": 0, "generated_for_day": None, "last_refreshed_at": None}

    async def fake_relationship_state(*_args, **_kwargs):
        return MemoryRetrievalDoctorRelationshipState()

    async def fake_retrieve_memory(db, *, embedder, tenant_id: str, body):
        return MemoryRetrieveResponse(
            scope=body.scope,
            routed_room_id=None,
            redirected_from_room_id=None,
            trace=PalaceRetrieveTrace(route_confidence="high"),
            results=[
                SearchResult(
                    item_id=item_id,
                    title="Matched memory",
                    summary=None,
                    source_type="note",
                    source_url=None,
                    tags=["doctor"],
                    created_at=datetime.now(timezone.utc),
                    chunk_text="raw text must not surface in doctor output",
                    chunk_index=0,
                    score=0.94,
                )
            ],
            total=1,
        )

    monkeypatch.setattr("app.services.memory.build_worker_backpressure", fake_queue_health)
    monkeypatch.setattr("app.services.memory.build_wakeup_brief_summary", fake_wakeup_summary)
    monkeypatch.setattr("app.services.memory._build_relationship_doctor_state", fake_relationship_state)
    monkeypatch.setattr("app.services.memory.retrieve_memory", fake_retrieve_memory)

    report = await build_memory_retrieval_doctor(
        FakeSession(),
        embedder=object(),
        tenant_id="tenant-a",
        body=MemoryRetrievalDoctorRequest(
            agent_scope_key="codex",
            sample_probes=[
                {
                    "query": "private probe text",
                    "scope": {"type": "agent", "key": "codex"},
                    "expected_item_ids": [item_id],
                }
            ],
        ),
        auth=MemoryRetrievalDoctorAuthShape(auth_mode="mcp_oauth", mcp_client_key="codex"),
    )

    payload = report.model_dump(mode="json")
    assert report.status == "ok"
    assert report.probes[0].expected_top_rank == 1
    assert "private probe text" not in json.dumps(payload)
    assert "raw text must not surface" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_memory_retrieval_doctor_marks_empty_probe_unhealthy(monkeypatch) -> None:
    async def fake_queue_health(_arq_pool):
        return PalaceWorkerBackpressureSummary(generated_at=datetime.now(timezone.utc), queues=[])

    async def fake_wakeup_summary(*_args, **_kwargs):
        return {"fresh": 0, "stale": 0, "generated_for_day": None, "last_refreshed_at": None}

    async def fake_relationship_state(*_args, **_kwargs):
        return MemoryRetrievalDoctorRelationshipState()

    async def fake_retrieve_memory(db, *, embedder, tenant_id: str, body):
        return MemoryRetrieveResponse(
            scope=body.scope,
            routed_room_id=None,
            redirected_from_room_id=None,
            trace=PalaceRetrieveTrace(fallback_used=True, completeness_warning="fallback needed"),
            results=[],
            total=0,
        )

    monkeypatch.setattr("app.services.memory.build_worker_backpressure", fake_queue_health)
    monkeypatch.setattr("app.services.memory.build_wakeup_brief_summary", fake_wakeup_summary)
    monkeypatch.setattr("app.services.memory._build_relationship_doctor_state", fake_relationship_state)
    monkeypatch.setattr("app.services.memory.retrieve_memory", fake_retrieve_memory)

    report = await build_memory_retrieval_doctor(
        FakeSession(),
        embedder=object(),
        tenant_id="tenant-a",
        body=MemoryRetrievalDoctorRequest(
            sample_probes=[
                {
                    "query": "missing private probe",
                    "scope": {"type": "tenant_shared"},
                    "expected_item_ids": [uuid.uuid4()],
                }
            ],
        ),
        auth=MemoryRetrievalDoctorAuthShape(auth_mode="mcp_oauth"),
    )

    assert report.status == "unhealthy"
    assert report.probes[0].status == "unhealthy"
    assert "probe returned no results" in report.probes[0].reasons
    assert "expected item was not returned" in report.probes[0].reasons


def test_memory_retrieve_capture_records_trace_without_raw_query(monkeypatch, tmp_path) -> None:
    capture_path = tmp_path / "memory-capture.ndjson"
    monkeypatch.setattr("app.services.retrieval_capture.settings.retrieval_capture_enabled", True)
    monkeypatch.setattr("app.services.retrieval_capture.settings.retrieval_capture_path", str(capture_path))
    monkeypatch.setattr("app.services.retrieval_capture.settings.retrieval_capture_query_mode", "redacted")
    monkeypatch.setattr("app.services.retrieval_capture.settings.app_version", "test-sha")
    client = _build_app(FakeSession())

    async def fake_retrieve_memory(db, *, embedder, tenant_id: str, body):
        return MemoryRetrieveResponse(
            scope=body.scope,
            routed_room_id=uuid.UUID("00000000-0000-0000-0000-000000000010"),
            redirected_from_room_id=None,
            trace=PalaceRetrieveTrace(
                requested_scope_type="workspace",
                requested_scope_key="launch-pad",
                selected_wing="Product / Growth",
                candidate_rooms=["Launch Briefs"],
                expanded_rooms=[],
                fallback_used=True,
                completeness_warning="Global fallback used.",
                steps=[{"title": "route", "detail": "secret launch brief"}],
            ),
            results=[
                SearchResult(
                    item_id=uuid.UUID("00000000-0000-0000-0000-000000000011"),
                    title="Shared launch brief",
                    summary="Cross-host context",
                    source_type="note",
                    source_url=None,
                    tags=["launch"],
                    created_at=datetime.now(timezone.utc),
                    chunk_text="Sensitive launch content",
                    chunk_index=0,
                    score=0.88,
                )
            ],
            total=1,
        )

    monkeypatch.setattr("app.api.memory.retrieve_memory", fake_retrieve_memory)

    response = client.post(
        "/api/v1/memory/retrieve",
        json={
            "query": "secret launch brief",
            "limit": 5,
            "scope": {"type": "workspace", "key": "launch-pad"},
        },
    )

    assert response.status_code == 200
    record_text = capture_path.read_text(encoding="utf-8")
    record = json.loads(record_text)
    assert record["endpoint"] == "/api/v1/memory/retrieve"
    assert record["request"]["query_redacted"] == "<term:6> <term:6> <term:5>"
    assert "secret launch brief" not in record_text
    assert "Sensitive launch content" not in record_text
    assert record["fallback_used"] is True
    assert record["trace"]["steps"] == [{"title": "route"}]
    assert record["request"]["scope"] == {"type": "workspace", "key": "launch-pad"}
    assert record["results"][0]["item_id"] == "00000000-0000-0000-0000-000000000011"


@pytest.mark.asyncio
async def test_retrieve_memory_forwards_larger_nist_limit_to_palace(monkeypatch) -> None:
    from app.services.memory import retrieve_memory

    async def fake_retrieve_palace(db, *, tenant_id: str, embedder, body, query_vector=None):
        assert tenant_id == "tenant-a"
        assert body.limit == 21
        assert body.candidate_limit == 90
        assert body.tags == ["benchmark:nist", "nist-sp800"]
        assert body.tags_mode == "all"
        return PalaceRetrieveResponse(
            routed_room_id=None,
            redirected_from_room_id=None,
            trace=PalaceRetrieveTrace(
                requested_scope_type="tenant_shared",
                requested_scope_key=None,
                selected_wing=None,
                candidate_rooms=[],
                expanded_rooms=[],
                fallback_used=False,
                completeness_warning=None,
                steps=[],
            ),
            results=[
                SearchResult(
                    item_id=uuid.uuid4(),
                    title="NIST SP 800-207 - Zero Trust Architecture",
                    summary="Zero trust architecture guidance.",
                    source_type="pdf",
                    source_url="https://doi.org/10.6028/NIST.SP.800-207",
                    tags=["benchmark:nist", "nist-sp800"],
                    created_at=datetime.now(timezone.utc),
                    chunk_text="NIST SP 800-207 defines zero trust architecture.",
                    chunk_index=0,
                    score=0.92,
                )
            ],
            total=1,
        )

    monkeypatch.setattr("app.services.memory.retrieve_palace", fake_retrieve_palace)

    response = await retrieve_memory(
        FakeSession(),
        embedder=object(),
        tenant_id="tenant-a",
        body=MemoryRetrieveRequest(
            query="zero trust architecture",
            limit=21,
            candidate_limit=90,
            tags=["benchmark:nist", "nist-sp800"],
            tags_mode="all",
        ),
    )

    assert response.total == 1
    assert response.results[0].title == "NIST SP 800-207 - Zero Trust Architecture"


def test_memory_wakeup_brief_returns_authenticated_tenant_scope(monkeypatch) -> None:
    item_id = uuid.uuid4()
    client = _build_app(FakeSession())

    async def fake_get_wakeup_brief(db, *, tenant_id: str, scope_type: str, scope_key: str | None):
        assert tenant_id == "tenant-a"
        assert scope_type == "wing"
        assert scope_key == "product-growth"
        return MemoryWakeupBriefResponse(
            source_item_id=item_id,
            title="Wake-up Brief 2026-04-23 [wing:product-growth]",
            summary="Startup context for product growth.",
            body="Current body",
            source_url="memory://wakeup-brief/wing/product-growth/2026-04-23",
            day="2026-04-23",
            scope_type="wing",
            scope_key="product-growth",
            generation=7,
            indexed_generation=8,
            freshness="stale",
            stale=True,
            room_count=3,
            diary_count=2,
            fact_count=5,
            updated_at=datetime(2026, 4, 23, 6, 0, tzinfo=timezone.utc),
        )

    monkeypatch.setattr("app.api.memory.get_memory_wakeup_brief", fake_get_wakeup_brief)

    response = client.get("/api/v1/memory/wakeup-brief?scope_type=wing&scope_key=product-growth")

    assert response.status_code == 200
    payload = response.json()
    assert payload["source_item_id"] == str(item_id)
    assert payload["scope_type"] == "wing"
    assert payload["scope_key"] == "product-growth"
    assert payload["freshness"] == "stale"


def test_memory_wakeup_brief_includes_compact_source_trust(monkeypatch) -> None:
    item_id = uuid.uuid4()
    now = datetime(2026, 4, 23, 6, 0, tzinfo=timezone.utc)
    brief_item = SimpleNamespace(
        id=item_id,
        tenant_id="tenant-a",
        status="ready",
        deleted_at=None,
        title="Wake-up Brief 2026-04-23",
        summary="Startup context.",
        raw_content="Current wakeup body",
        source_url="memory://wakeup-brief/tenant/default/2026-04-23",
        metadata_={
            "wakeup_brief": {
                "scope_type": "tenant",
                "day": "2026-04-23",
                "generation": 9,
                "room_count": 3,
                "diary_count": 2,
                "fact_count": 5,
            }
        },
        updated_at=now,
    )

    class _Session:
        async def get(self, model, key):
            return SimpleNamespace(indexed_generation=9)

        async def execute(self, statement):
            return _ScalarsResult([brief_item])

    async def fake_source_trust(db, *, tenant_id: str, item_ids):
        assert tenant_id == "tenant-a"
        assert item_ids == [item_id]
        return {
            item_id: SourceTrustSummary(
                item_id=item_id,
                state="generated_unpromoted",
                warning="generated_artifact_without_promoted_source_support",
            )
        }

    monkeypatch.setattr(memory_service, "get_source_trust_summaries", fake_source_trust)

    response = asyncio.run(memory_service.get_memory_wakeup_brief(_Session(), tenant_id="tenant-a"))

    assert response.source_item_id == item_id
    assert response.source_trust is not None
    assert response.source_trust.state == "generated_unpromoted"
    assert response.source_trust.warning == "generated_artifact_without_promoted_source_support"


def test_memory_trajectory_route_uses_authenticated_tenant_and_policy(monkeypatch) -> None:
    item_id = uuid.uuid4()
    client = _build_app(FakeSession())

    async def fake_retrieve_memory_trajectory(db, *, embedder, tenant_id: str, body, delegated_policy=None):
        assert tenant_id == "tenant-a"
        assert body.query == "how did deploy status change?"
        assert body.agent_scope_key == "codex"
        assert body.include_broad_corpus is False
        return MemoryTrajectoryResponse(
            query=body.query,
            trajectory_subject=body.trajectory_subject,
            scopes=[],
            trace=AgentMemoryRetrieveTrace(),
            entries=[
                MemoryTrajectoryEntry(
                    item_id=item_id,
                    title="Conversation fact: Andrew said",
                    subject="Andrew",
                    predicate="said",
                    object_text="Deploy is ready.",
                    trajectory_key="deploy status",
                    status="current",
                    event_time=datetime(2026, 5, 2, tzinfo=timezone.utc),
                    source_item_id=item_id,
                    source_span={"source_item_id": str(item_id), "line_start": 2, "line_end": 2},
                    retrieved_scope_label="agent/codex",
                    score=0.9,
                )
            ],
            current_entries=[],
            total=1,
        )

    monkeypatch.setattr("app.api.memory.retrieve_memory_trajectory", fake_retrieve_memory_trajectory)

    response = client.post(
        "/api/v1/memory/trajectory",
        json={
            "query": "how did deploy status change?",
            "trajectory_subject": "deploy status",
            "agent_scope_key": "codex",
            "include_broad_corpus": False,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["entries"][0]["object_text"] == "Deploy is ready."
    assert payload["entries"][0]["source_span"]["line_start"] == 2


def test_memory_whoami_can_reject_invalid_key() -> None:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    async def override_get_db():
        yield FakeSession()

    async def override_verify(_request: Request):
        raise HTTPException(status_code=403, detail="Invalid or revoked API key")

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[verify_memory_auth] = override_verify

    client = TestClient(app)
    response = client.get("/api/v1/memory/whoami")

    assert response.status_code == 403


def test_memory_facade_smoke_uses_main_app_routes(monkeypatch) -> None:
    session = FakeSession()
    main_app.state.arq_pool = FakeArqPool()
    main_app.state.embedder = object()

    async def override_get_db():
        yield session

    async def override_verify(request: Request):
        request.state.tenant_id = "tenant-a"
        request.state.key_hash = "key-hash"
        return "raw-key"

    async def fake_accept_canonical_memory_entry(
        db, *, body: MemoryEntryRequest, signing_key: str, admission_audit: dict | None = None
    ):
        assert db is session
        assert signing_key == "key-hash"
        assert body.tenant_id == "tenant-a"
        assert body.scope.type == "workspace"
        assert admission_audit is not None
        return MemoryArtifactAcceptanceResult(
            job=Job(
                id=uuid.uuid4(),
                job_type=MEMORY_JOB_TYPE,
                tenant_id=body.tenant_id,
                status="queued",
                progress=0,
                created_at=datetime.now(timezone.utc),
            ),
            enqueue_requested=True,
            scope_type=body.scope.type,
            scope_key=body.scope.key,
            accepted_as="canonical",
        )

    async def fake_retrieve_memory(db, *, embedder, tenant_id: str, body):
        assert db is session
        assert embedder is main_app.state.embedder
        assert tenant_id == "tenant-a"
        assert body.query == "launch brief"
        return MemoryRetrieveResponse(
            scope=body.scope,
            routed_room_id=None,
            redirected_from_room_id=None,
            trace=PalaceRetrieveTrace(
                requested_scope_type=body.scope.type,
                requested_scope_key=body.scope.key,
                selected_wing="Product / Growth",
                candidate_rooms=["Launch Briefs"],
                expanded_rooms=[],
                fallback_used=False,
                completeness_warning=None,
                steps=[],
            ),
            results=[
                SearchResult(
                    item_id=uuid.uuid4(),
                    title="Shared launch brief",
                    summary="Cross-host context",
                    source_type="note",
                    source_url=None,
                    tags=["launch"],
                    created_at=datetime.now(timezone.utc),
                    chunk_text="Agents should reuse the same launch brief when they migrate hosts.",
                    chunk_index=0,
                    score=0.88,
                )
            ],
            total=1,
        )

    monkeypatch.setattr("app.api.memory.accept_canonical_memory_entry", fake_accept_canonical_memory_entry)
    monkeypatch.setattr("app.api.memory.retrieve_memory", fake_retrieve_memory)
    main_app.dependency_overrides[get_db] = override_get_db
    main_app.dependency_overrides[verify_memory_auth] = override_verify

    try:
        client = TestClient(main_app)

        whoami_response = client.get("/api/v1/memory/whoami")
        write_response = client.post("/api/v1/memory/entries", json=_canonical_payload())
        retrieve_response = client.post(
            "/api/v1/memory/retrieve",
            json={
                "query": "launch brief",
                "limit": 5,
                "scope": {"type": "workspace", "key": "launch-pad"},
            },
        )
    finally:
        main_app.dependency_overrides.clear()

    assert whoami_response.status_code == 200
    assert whoami_response.json() == {
        "status": "ok",
        "tenant_id": "tenant-a",
        "auth_mode": None,
        "mcp_client_id": None,
        "mcp_client_key": None,
        "allowed_scopes": [],
        "resource": None,
        "audience": None,
        "token_hash_prefix": "key-hash",
    }
    assert write_response.status_code == 202
    assert write_response.json()["accepted_as"] == "canonical"
    assert write_response.json()["scope"] == {"type": "workspace", "key": "launch-pad"}
    assert main_app.state.arq_pool.enqueued == [
        ("memory_artifact", {"job_id": write_response.json()["job_id"]})
    ]
    assert retrieve_response.status_code == 200
    assert retrieve_response.json()["trace"]["requested_scope_key"] == "launch-pad"
    assert retrieve_response.json()["results"][0]["title"] == "Shared launch brief"
