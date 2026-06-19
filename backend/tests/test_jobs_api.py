import uuid
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import app.api.jobs as jobs_api
from app.api.jobs import router
from app.auth import verify_api_key, verify_capture_job_read_auth
from app.database import get_db
from app.models.item import Item
from app.models.job import Job, JobProgressEvent
from app.workers.queues import DEFAULT_WORKER_QUEUE, MEDIA_FAIR_DISPATCH_TASK_NAME, singleton_job_id


class FakeSession:
    def __init__(self, jobs=None, items=None, progress_events=None) -> None:
        self.jobs = jobs or {}
        self.items = items or {}
        self.progress_events = progress_events or []
        self.commits = 0

    async def get(self, model, key):
        if model is Job:
            return self.jobs.get(key)
        if model is Item:
            return self.items.get(key)
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, value) -> None:
        return None

    async def execute(self, statement):
        return FakeResult(self.progress_events)

    def add(self, value) -> None:
        if isinstance(value, JobProgressEvent):
            self.progress_events.append(value)


class FakeResult:
    def __init__(self, rows) -> None:
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


class FakeArqPool:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.enqueued = []
        self.error = error

    async def enqueue_job(self, name: str, **kwargs) -> None:
        if self.error is not None:
            raise self.error
        self.enqueued.append((name, kwargs))


def _client(session: FakeSession, *, arq_pool: FakeArqPool | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.arq_pool = arq_pool or FakeArqPool()

    async def override_get_db():
        yield session

    async def override_verify(request: Request):
        request.state.tenant_id = "tenant-a"
        request.state.key_hash = "key-hash"
        return "raw-key"

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[verify_api_key] = override_verify
    app.dependency_overrides[verify_capture_job_read_auth] = override_verify
    return TestClient(app)


def test_generic_jobs_hide_memory_job() -> None:
    job_id = uuid.uuid4()
    client = _client(
        FakeSession(
            jobs={
                job_id: Job(
                    id=job_id,
                    item_id=uuid.uuid4(),
                    job_type="memory_artifact",
                    tenant_id="tenant-a",
                    status="queued",
                    progress=0,
                    created_at=datetime.now(timezone.utc),
                )
            }
        )
    )

    assert client.get(f"/api/v1/jobs/{job_id}").status_code == 404
    assert client.post(f"/api/v1/jobs/{job_id}/retry").status_code == 404
    assert client.delete(f"/api/v1/jobs/{job_id}").status_code == 404


def test_generic_jobs_still_return_normal_jobs() -> None:
    job_id = uuid.uuid4()
    event = JobProgressEvent(
        id=uuid.uuid4(),
        job_id=job_id,
        tenant_id="tenant-a",
        phase="embedded",
        status="processing",
        progress=60,
        message="Embedded chunks",
        metadata_={"chunk_count": 3},
        created_at=datetime.now(timezone.utc),
    )
    client = _client(
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
                    completed_at=datetime.now(timezone.utc),
                )
            },
            progress_events=[event],
        )
    )

    response = client.get(f"/api/v1/jobs/{job_id}")

    assert response.status_code == 200
    assert response.json()["job_type"] == "note"
    assert response.json()["recent_progress_events"][0]["phase"] == "embedded"
    assert response.json()["recent_progress_events"][0]["metadata_"] == {"chunk_count": 3}


def test_extension_job_status_read_is_limited_to_audited_capture_jobs(monkeypatch) -> None:
    allowed_job_id = uuid.uuid4()
    hidden_job_id = uuid.uuid4()

    async def fake_extension_visible_job_ids(db, request: Request):
        return [allowed_job_id]

    monkeypatch.setattr(jobs_api, "_extension_visible_job_ids", fake_extension_visible_job_ids)
    client = _client(
        FakeSession(
            jobs={
                allowed_job_id: Job(
                    id=allowed_job_id,
                    item_id=uuid.uuid4(),
                    job_type="webpage",
                    tenant_id="tenant-a",
                    status="queued",
                    progress=0,
                    created_at=datetime.now(timezone.utc),
                ),
                hidden_job_id: Job(
                    id=hidden_job_id,
                    item_id=uuid.uuid4(),
                    job_type="webpage",
                    tenant_id="tenant-a",
                    status="queued",
                    progress=0,
                    created_at=datetime.now(timezone.utc),
                ),
            }
        )
    )

    assert client.get(f"/api/v1/jobs/{allowed_job_id}").status_code == 200
    assert client.get(f"/api/v1/jobs/{hidden_job_id}").status_code == 404


def test_retry_rejects_missing_retry_input_without_mutating_state() -> None:
    item_id = uuid.uuid4()
    job_id = uuid.uuid4()
    completed_at = datetime.now(timezone.utc)
    item = Item(
        id=item_id,
        source_type="webpage",
        source_url=None,
        title="Broken page",
        raw_content=None,
        metadata_={},
        tags=[],
        categories=[],
        tenant_id="tenant-a",
        status="failed",
    )
    job = Job(
        id=job_id,
        item_id=item_id,
        job_type="webpage",
        tenant_id="tenant-a",
        status="failed",
        progress=65,
        error_message="timed out",
        created_at=datetime.now(timezone.utc),
        completed_at=completed_at,
    )
    session = FakeSession(jobs={job_id: job}, items={item_id: item})
    client = _client(session)

    response = client.post(f"/api/v1/jobs/{job_id}/retry")

    assert response.status_code == 409
    assert response.json()["detail"] == "No source URL on item; cannot retry"
    assert session.commits == 0
    assert job.status == "failed"
    assert job.progress == 65
    assert job.error_message == "timed out"
    assert job.completed_at == completed_at
    assert item.status == "failed"
    assert client.app.state.arq_pool.enqueued == []


def test_retry_requeues_note_from_persisted_payload_when_item_content_is_missing() -> None:
    item_id = uuid.uuid4()
    job_id = uuid.uuid4()
    item = Item(
        id=item_id,
        source_type="note",
        title="Stale title",
        raw_content=None,
        metadata_={},
        tags=[],
        categories=[],
        tenant_id="tenant-a",
        status="failed",
    )
    job = Job(
        id=job_id,
        item_id=item_id,
        job_type="note",
        tenant_id="tenant-a",
        status="failed",
        progress=40,
        error_message="worker crashed",
        created_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        payload={
            "retry_task": {
                "name": "process_note",
                "kwargs": {
                    "title": "Quarterly planning",
                    "content": "Capture decisions and follow-ups.",
                    "model": "note-model",
                },
            }
        },
    )
    session = FakeSession(jobs={job_id: job}, items={item_id: item})
    client = _client(session)

    response = client.post(f"/api/v1/jobs/{job_id}/retry")

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert session.commits == 1
    assert job.status == "queued"
    assert job.progress == 0
    assert job.error_message is None
    assert job.completed_at is None
    assert item.status == "processing"
    assert client.app.state.arq_pool.enqueued == [
        (
            "process_note",
            {
                "job_id": str(job_id),
                "tenant_id": "tenant-a",
                "title": "Quarterly planning",
                "content": "Capture decisions and follow-ups.",
                "model": "note-model",
            },
        )
    ]


def test_retry_requeues_media_to_bounded_media_queue() -> None:
    item_id = uuid.uuid4()
    job_id = uuid.uuid4()
    item = Item(
        id=item_id,
        source_type="media",
        title="https://example.com/watch?v=media",
        source_url="https://example.com/watch?v=media",
        raw_content=None,
        metadata_={},
        tags=[],
        categories=[],
        tenant_id="tenant-a",
        status="failed",
    )
    job = Job(
        id=job_id,
        item_id=item_id,
        job_type="media",
        tenant_id="tenant-a",
        status="failed",
        progress=20,
        error_message="worker cancelled",
        created_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
    )
    session = FakeSession(jobs={job_id: job}, items={item_id: item})
    client = _client(session)

    response = client.post(f"/api/v1/jobs/{job_id}/retry")

    assert response.status_code == 200
    assert client.app.state.arq_pool.enqueued == [
        (
            MEDIA_FAIR_DISPATCH_TASK_NAME,
            {
                "_queue_name": DEFAULT_WORKER_QUEUE,
                "_job_id": singleton_job_id(MEDIA_FAIR_DISPATCH_TASK_NAME, "media"),
            },
        )
    ]


def test_retry_requeues_cancelled_media_with_current_item_source_url_and_model() -> None:
    item_id = uuid.uuid4()
    job_id = uuid.uuid4()
    stale_created_at = datetime.now(timezone.utc) - timedelta(hours=2)
    item = Item(
        id=item_id,
        source_type="media",
        title="https://example.com/watch?v=current",
        source_url="https://example.com/watch?v=current",
        raw_content=None,
        metadata_={},
        tags=[],
        categories=[],
        tenant_id="tenant-a",
        status="failed",
    )
    job = Job(
        id=job_id,
        item_id=item_id,
        job_type="media",
        tenant_id="tenant-a",
        status="cancelled",
        progress=66,
        error_message="Worker cancelled the job before completion",
        created_at=stale_created_at,
        completed_at=datetime.now(timezone.utc),
        payload={
            "retry_task": {
                "name": "process_media",
                "kwargs": {
                    "url": "https://example.com/watch?v=stale",
                    "model": "media-model",
                },
            }
        },
    )
    session = FakeSession(jobs={job_id: job}, items={item_id: item})
    client = _client(session)

    response = client.post(f"/api/v1/jobs/{job_id}/retry")

    assert response.status_code == 200
    assert job.status == "queued"
    assert job.progress == 0
    assert job.error_message is None
    assert job.completed_at is None
    assert job.created_at > stale_created_at
    assert item.status == "processing"
    assert client.app.state.arq_pool.enqueued == [
        (
            MEDIA_FAIR_DISPATCH_TASK_NAME,
            {
                "_queue_name": DEFAULT_WORKER_QUEUE,
                "_job_id": singleton_job_id(MEDIA_FAIR_DISPATCH_TASK_NAME, "media"),
            },
        )
    ]


def test_retry_rejects_orphaned_media_payload_without_requeueing() -> None:
    job_id = uuid.uuid4()
    job = Job(
        id=job_id,
        item_id=None,
        job_type="media",
        tenant_id="tenant-a",
        status="cancelled",
        progress=66,
        error_message="Worker cancelled the job before completion",
        created_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        payload={
            "retry_task": {
                "name": "process_media",
                "kwargs": {
                    "url": "https://example.com/watch?v=orphan",
                    "model": "media-model",
                },
            }
        },
    )
    session = FakeSession(jobs={job_id: job})
    client = _client(session)

    response = client.post(f"/api/v1/jobs/{job_id}/retry")

    assert response.status_code == 409
    assert response.json()["detail"] == "No source URL on item; cannot retry"
    assert session.commits == 0
    assert job.status == "cancelled"
    assert client.app.state.arq_pool.enqueued == []


def test_retry_requeues_pdf_with_tenant_context() -> None:
    item_id = uuid.uuid4()
    job_id = uuid.uuid4()
    item = Item(
        id=item_id,
        source_type="pdf",
        title="Launch brief.pdf",
        raw_content="Recovered PDF text",
        metadata_={},
        tags=[],
        categories=[],
        tenant_id="tenant-a",
        status="failed",
    )
    job = Job(
        id=job_id,
        item_id=item_id,
        job_type="pdf",
        tenant_id="tenant-a",
        status="cancelled",
        progress=100,
        error_message="operator cancelled",
        created_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
    )
    session = FakeSession(jobs={job_id: job}, items={item_id: item})
    client = _client(session)

    response = client.post(f"/api/v1/jobs/{job_id}/retry")

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert session.commits == 1
    assert job.status == "queued"
    assert job.progress == 0
    assert job.error_message is None
    assert job.completed_at is None
    assert item.status == "processing"
    assert client.app.state.arq_pool.enqueued == [
        (
            "process_pdf",
            {
                "job_id": str(job_id),
                "tenant_id": "tenant-a",
                "extracted_text": "Recovered PDF text",
                "pdf_metadata": {},
            },
        )
    ]


def test_retry_restores_state_when_enqueue_fails() -> None:
    item_id = uuid.uuid4()
    job_id = uuid.uuid4()
    duplicate_of = uuid.uuid4()
    completed_at = datetime.now(timezone.utc)
    created_at = datetime.now(timezone.utc) - timedelta(hours=2)
    item = Item(
        id=item_id,
        source_type="note",
        title="Quarterly planning",
        raw_content="Original note body",
        metadata_={},
        tags=[],
        categories=[],
        tenant_id="tenant-a",
        status="failed",
    )
    job = Job(
        id=job_id,
        item_id=item_id,
        job_type="note",
        tenant_id="tenant-a",
        status="failed",
        progress=65,
        error_message="worker crashed",
        duplicate_of=duplicate_of,
        created_at=created_at,
        completed_at=completed_at,
    )
    session = FakeSession(jobs={job_id: job}, items={item_id: item})
    client = _client(session, arq_pool=FakeArqPool(error=RuntimeError("redis unavailable")))

    response = client.post(f"/api/v1/jobs/{job_id}/retry")

    assert response.status_code == 503
    assert response.json()["detail"] == "Retry enqueue failed; job state restored"
    assert session.commits == 2
    assert job.status == "failed"
    assert job.progress == 65
    assert job.error_message == "worker crashed"
    assert job.duplicate_of == duplicate_of
    assert job.created_at == created_at
    assert job.completed_at == completed_at
    assert item.status == "failed"
    assert client.app.state.arq_pool.enqueued == []
