from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.feeds import router as feeds_router
from app.api.items import router as items_router
from app.auth import AuthContext, verify_memory_auth
from app.database import get_db
from app.models.item import Item
from app.models.job import Job
from app.pipelines.image import ImagePipeline
from app.services.image_analysis import ImageAnalysisError
from app.services.relationships import RelationshipService
from app.workers.queues import PALACE_WORKER_QUEUE
from app.workers import feed_tasks
from app.workers import tasks


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


class RowsResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def scalar_one_or_none(self):
        return 1


class MappingResult:
    def __init__(self, *, rows=None, row=None):
        self.rows = rows or []
        self.row = row

    def mappings(self):
        return self

    def one_or_none(self):
        return self.row

    def all(self):
        return self.rows


class FakeLlm:
    async def classify_relationship(self, *_args):
        return ("related_to", 0.92)


class FakeRelationshipSession:
    def __init__(self, item: Item, candidate_rows: list[SimpleNamespace]) -> None:
        self.item = item
        self.candidate_rows = candidate_rows
        self.statements: list[tuple[str, dict | None]] = []
        self.committed = False

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append((sql, params))
        if "SELECT COUNT(*) FROM items" in sql:
            return ScalarResult(2)
        if "SELECT i.id, i.title, i.summary" in sql:
            return RowsResult(self.candidate_rows)
        if "INSERT INTO item_relationships" in sql:
            return RowsResult([])
        raise AssertionError(f"Unexpected SQL: {sql}")

    async def get(self, model, key):
        assert model is Item
        assert key == self.item.id
        return self.item

    async def commit(self):
        self.committed = True


class FakeArqPool:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict]] = []

    async def enqueue_job(self, name: str, **kwargs) -> None:
        self.enqueued.append((name, kwargs))


class FakeItemsSession:
    def __init__(self, origin_item: Item, related_rows: list[SimpleNamespace]) -> None:
        self.origin_item = origin_item
        self.related_rows = related_rows

    async def get(self, model, key):
        assert model is Item
        assert key == self.origin_item.id
        return self.origin_item

    async def execute(self, statement, params=None):
        sql = str(statement)
        assert "i.tenant_id = :tenant_id" in sql
        assert params == {"item_id": str(self.origin_item.id), "tenant_id": self.origin_item.tenant_id}
        return RowsResult(self.related_rows)


class FakeFeedsSession:
    def __init__(self, tenant_id: str, created_feed_ids: list[uuid.UUID]) -> None:
        self.tenant_id = tenant_id
        self.created_feed_ids = created_feed_ids
        self.insert_index = 0
        now = datetime.now(timezone.utc)
        self.feed_rows = [
            {
                "id": feed_id,
                "url": f"https://example.com/feed-{index}.xml",
                "name": None,
                "auto_tags": [],
                "poll_interval": 300,
                "enabled": True,
                "paused_reason": None,
                "last_fetched_at": None,
                "last_error": None,
                "consecutive_failures": 0,
                "feed_metadata": {},
                "item_count": 0,
                "created_at": now,
                "updated_at": now,
            }
            for index, feed_id in enumerate(created_feed_ids, start=1)
        ]

    async def execute(self, statement, params=None):
        sql = str(statement)
        if "INSERT INTO feeds" in sql:
            if self.insert_index >= len(self.created_feed_ids):
                return MappingResult(row=None)
            expected_id = self.created_feed_ids[self.insert_index]
            self.insert_index += 1
            assert params["tenant_id"] == self.tenant_id
            return MappingResult(row={"id": expected_id})
        if "SELECT f.*" in sql:
            assert params["tenant_id"] == self.tenant_id
            return MappingResult(rows=self.feed_rows)
        raise AssertionError(f"Unexpected SQL: {sql}")

    async def commit(self):
        return None


class FakeFeedTaskSession:
    def __init__(self, stale_rows: list[SimpleNamespace], jobs: dict[uuid.UUID, Job]) -> None:
        self.stale_rows = stale_rows
        self.jobs = jobs

    async def execute(self, statement, params=None):
        sql = str(statement)
        assert "SELECT j.id" in sql
        return RowsResult(self.stale_rows)

    async def get(self, model, key):
        assert model in {Job, Item}
        return self.jobs.get(key)

    async def commit(self):
        return None


class FakeEmbedItemSession:
    def __init__(self, item: Item) -> None:
        self.item = item
        self.execute_sql: list[str] = []

    async def execute(self, statement, params=None):
        self.execute_sql.append(str(statement))
        return RowsResult([])

    async def get(self, model, key):
        assert model is Item
        if key == self.item.id:
            return self.item
        return None


class FakeMemoryArtifactSession:
    def __init__(self, job: Job, item: Item) -> None:
        self.job = job
        self.item = item

    async def get(self, model, key):
        if model is Job and key == self.job.id:
            return self.job
        if model is Item and key == self.item.id:
            return self.item
        return None


class FakeBackfillSession:
    def __init__(self, item_ids: list[uuid.UUID], *, lock_acquired: bool = True) -> None:
        self.item_ids = item_ids
        self.lock_acquired = lock_acquired
        self.execute_calls: list[tuple[str, dict | None]] = []

    async def execute(self, statement, params=None):
        self.execute_calls.append((str(statement), params))
        if "pg_try_advisory_xact_lock" in str(statement):
            return ScalarResult(self.lock_acquired)
        return RowsResult([SimpleNamespace(id=item_id) for item_id in self.item_ids])


class FakeImageFailureSession:
    def __init__(self, job: Job, item: Item) -> None:
        self.job = job
        self.item = item
        self.added: list[object] = []
        self.committed = False

    async def get(self, model, key):
        if model is Job and key == self.job.id:
            return self.job
        if model is Item and key == self.item.id:
            return self.item
        return None

    def add(self, value) -> None:
        self.added.append(value)

    async def execute(self, statement, params=None):
        class EmptyScalarRows:
            def scalars(self):
                return self

            def all(self):
                return []

        return EmptyScalarRows()

    async def commit(self):
        self.committed = True


class SessionFactory:
    def __init__(self, session) -> None:
        self.session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _items_client(session, *, tenant_id: str = "tenant-a") -> TestClient:
    app = FastAPI()
    app.include_router(items_router, prefix="/api/v1")

    async def override_get_db():
        yield session

    async def override_verify(request: Request):
        request.state.auth_context = AuthContext(
            tenant_id=tenant_id,
            auth_mode="api_key",
            token_hash_reference="key-hash",
        )
        request.state.tenant_id = tenant_id
        request.state.key_hash = "key-hash"
        request.state.auth_mode = "api_key"
        return "raw-key"

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[verify_memory_auth] = override_verify
    return TestClient(app)


def _feeds_client(session, *, tenant_id: str = "tenant-a") -> TestClient:
    app = FastAPI()
    app.include_router(feeds_router, prefix="/api/v1")
    app.state.arq_pool = FakeArqPool()

    async def override_get_db():
        yield session

    async def override_verify(request: Request):
        request.state.auth_context = AuthContext(
            tenant_id=tenant_id,
            auth_mode="api_key",
            token_hash_reference="key-hash",
        )
        request.state.tenant_id = tenant_id
        request.state.key_hash = "key-hash"
        request.state.auth_mode = "api_key"
        return "raw-key"

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[verify_memory_auth] = override_verify
    return TestClient(app)


@pytest.mark.asyncio
async def test_relationship_service_scopes_ready_count_and_candidates_by_tenant() -> None:
    item_id = uuid.uuid4()
    candidate_id = uuid.uuid4()
    session = FakeRelationshipSession(
        Item(
            id=item_id,
            tenant_id="tenant-a",
            title="Origin",
            source_type="note",
            status="ready",
            summary="Origin summary",
        ),
        [
            SimpleNamespace(
                id=candidate_id,
                title="Candidate",
                summary="Candidate summary",
            )
        ],
    )
    service = RelationshipService(session, embedder=object(), llm=FakeLlm())

    await service.find_relationships(item_id, tenant_id="tenant-a")

    count_sql, count_params = session.statements[0]
    assert "tenant_id = :tenant_id" in count_sql
    assert count_params == {"tenant_id": "tenant-a"}

    candidate_sql, candidate_params = session.statements[1]
    assert "i.tenant_id = :tenant_id" in candidate_sql
    assert candidate_params["tenant_id"] == "tenant-a"
    assert session.committed is True


def test_related_items_endpoint_filters_joined_items_by_tenant() -> None:
    item_id = uuid.uuid4()
    client = _items_client(
        FakeItemsSession(
            Item(
                id=item_id,
                tenant_id="tenant-a",
                title="Origin",
                source_type="note",
                status="ready",
            ),
            [
                SimpleNamespace(
                    item_id=uuid.uuid4(),
                    title="Visible related item",
                    source_type="note",
                    relationship="related_to",
                    confidence=0.91,
                )
            ],
        )
    )

    response = client.get(f"/api/v1/items/{item_id}/related")

    assert response.status_code == 200
    assert response.json()["relationships"][0]["title"] == "Visible related item"


def test_import_opml_propagates_tenant_id_when_enqueuing_polls() -> None:
    created_feed_ids = [uuid.uuid4(), uuid.uuid4()]
    client = _feeds_client(FakeFeedsSession("tenant-a", created_feed_ids))

    response = client.post(
        "/api/v1/feeds/import_opml",
        files={
            "file": (
                "feeds.opml",
                b"""<?xml version="1.0"?><opml version="2.0"><body>
                <outline text="Feed 1" type="rss" xmlUrl="https://example.com/feed-1.xml" />
                <outline text="Feed 2" type="rss" xmlUrl="https://example.com/feed-2.xml" />
                </body></opml>""",
                "text/xml",
            )
        },
    )

    assert response.status_code == 202
    assert client.app.state.arq_pool.enqueued == [
        ("poll_feed", {"feed_id": str(created_feed_ids[0]), "tenant_id": "tenant-a"}),
        ("poll_feed", {"feed_id": str(created_feed_ids[1]), "tenant_id": "tenant-a"}),
    ]


@pytest.mark.asyncio
async def test_requeue_stale_pdf_jobs_rehydrates_current_pdf_contract_and_tenant(monkeypatch) -> None:
    job_id = uuid.uuid4()
    stale_job = Job(
        id=job_id,
        job_type="pdf",
        tenant_id="tenant-a",
        status="processing",
        progress=35,
        created_at=datetime.now(timezone.utc),
    )
    session = FakeFeedTaskSession(
        [
            SimpleNamespace(
                id=job_id,
                job_type="pdf",
                status="processing",
                item_id=None,
                tenant_id="tenant-a",
                payload=None,
                source_url=None,
                title="Legacy PDF",
                raw_content=None,
            )
        ],
        {job_id: stale_job},
    )
    redis = FakeArqPool()

    monkeypatch.setattr(feed_tasks, "async_session", SessionFactory(session))
    async def _requeueable(_redis, _job_id, **_kwargs):
        return True

    monkeypatch.setattr(feed_tasks, "_stale_job_is_requeueable", _requeueable)
    monkeypatch.setattr(feed_tasks.os.path, "exists", lambda path: str(path) == f"/tmp/palaceoftruth/{job_id}.pdf")
    monkeypatch.setattr(
        feed_tasks,
        "_extract_pdf_retry_payload",
        lambda path: ("Recovered PDF text", {"page_count": 3}),
    )

    await feed_tasks.requeue_stale_jobs({"redis": redis})

    assert redis.enqueued == [
        (
            "process_pdf",
            {
                "_job_id": str(job_id),
                "job_id": str(job_id),
                "tenant_id": "tenant-a",
                "extracted_text": "Recovered PDF text",
                "pdf_metadata": {"page_count": 3},
            },
        )
    ]
    assert stale_job.status == "queued"
    assert stale_job.progress == 0
    assert stale_job.error_message is None


@pytest.mark.asyncio
async def test_recover_stale_memory_jobs_requeues_memory_job_and_preserves_tenant(monkeypatch) -> None:
    job_id = uuid.uuid4()
    item_id = uuid.uuid4()
    stale_job = Job(
        id=job_id,
        item_id=item_id,
        job_type="memory_artifact",
        tenant_id="tenant-a",
        status="processing",
        progress=45,
        created_at=datetime.now(timezone.utc),
    )
    stale_item = Item(
        id=item_id,
        tenant_id="tenant-a",
        title="Recovered note",
        source_type="note",
        status="ready",
        raw_content="Recovered memory note content.",
    )
    session = FakeFeedTaskSession(
        [
            SimpleNamespace(
                id=job_id,
                job_type="memory_artifact",
                status="processing",
                item_id=item_id,
                tenant_id="tenant-a",
            )
        ],
        {
            job_id: stale_job,
            item_id: stale_item,
        },
    )
    redis = FakeArqPool()

    monkeypatch.setattr(tasks, "async_session", SessionFactory(session))

    await tasks.recover_stale_memory_jobs({"redis": redis})

    assert redis.enqueued == [
        (
            "memory_artifact",
            {
                "job_id": str(job_id),
            },
        )
    ]
    assert stale_job.status == "queued"
    assert stale_job.progress == 0
    assert stale_job.error_message is None
    assert stale_item.status == "processing"


@pytest.mark.asyncio
async def test_image_pipeline_analyzes_persisted_artifact_and_returns_completed_metadata(tmp_path) -> None:
    image_path = tmp_path / "pixel.png"
    image_path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00"
        b"\x1f\x15\xc4\x89"
    )
    calls: list[dict] = []

    class FakeVisionLlm:
        async def analyze_image(self, image_b64: str, media_type: str, filename: str) -> str:
            calls.append({"image_b64": image_b64, "media_type": media_type, "filename": filename})
            return "Single transparent pixel"

    pipeline = ImagePipeline(db=object(), embedder=object(), llm=FakeVisionLlm())

    description, metadata = await pipeline.extract(
        image_metadata={
            "filename": "pixel.png",
            "media_type": "image/png",
            "image_analysis": {
                "status": "queued",
                "artifact": {
                    "filename": "pixel.png",
                    "media_type": "image/png",
                    "extension": ".png",
                    "storage_path": str(image_path),
                },
            },
        }
    )

    assert description == "Single transparent pixel"
    assert calls and calls[0]["media_type"] == "image/png"
    analysis = metadata["image_analysis"]
    assert analysis["status"] == "completed"
    assert analysis["caption"] == "Single transparent pixel"
    assert analysis["artifact"]["storage_path"] == str(image_path)
    assert analysis["vision"]["error"] is None


@pytest.mark.asyncio
async def test_process_image_marks_provider_4xx_failure_non_retryable(monkeypatch) -> None:
    job_id = uuid.uuid4()
    item_id = uuid.uuid4()
    image_metadata = {
        "filename": "blocked.png",
        "media_type": "image/png",
        "image_analysis": {
            "status": "queued",
            "vision": {"provider": "openai", "model": "gpt-4o-mini", "error": None},
        },
    }
    job = Job(
        id=job_id,
        item_id=item_id,
        job_type="image",
        tenant_id="tenant-a",
        status="queued",
        progress=0,
        payload={"retry_task": {"name": "process_image", "kwargs": {"image_metadata": image_metadata}}},
    )
    item = Item(id=item_id, tenant_id="tenant-a", title="blocked.png", source_type="image", status="processing")
    session = FakeImageFailureSession(job, item)
    redis = FakeArqPool()

    async def fail_permanently(self, *_args, **_kwargs):
        raise ImageAnalysisError("Vision provider rejected the image", retryable=False, provider_status_code=400)

    monkeypatch.setattr(tasks, "async_session", SessionFactory(session))
    monkeypatch.setattr(ImagePipeline, "process", fail_permanently)

    await tasks.process_image({"redis": redis, "embedder": object(), "llm": object()}, job_id=str(job_id), tenant_id="tenant-a")

    assert job.status == "failed"
    assert job.progress == 100
    assert item.status == "failed"
    error = item.metadata_["image_analysis"]["vision"]["error"]
    assert error == {
        "message": "Vision provider rejected the image",
        "retryable": False,
        "provider_status_code": 400,
    }
    assert redis.enqueued == []


@pytest.mark.asyncio
async def test_process_image_marks_transient_provider_failure_retryable(monkeypatch) -> None:
    job_id = uuid.uuid4()
    item_id = uuid.uuid4()
    image_metadata = {
        "filename": "rate-limited.png",
        "media_type": "image/png",
        "image_analysis": {
            "status": "queued",
            "vision": {"provider": "openai", "model": "gpt-4o-mini", "error": None},
        },
    }
    job = Job(
        id=job_id,
        item_id=item_id,
        job_type="image",
        tenant_id="tenant-a",
        status="queued",
        progress=0,
        payload={"retry_task": {"name": "process_image", "kwargs": {"image_metadata": image_metadata}}},
    )
    item = Item(id=item_id, tenant_id="tenant-a", title="rate-limited.png", source_type="image", status="processing")
    session = FakeImageFailureSession(job, item)
    redis = FakeArqPool()

    async def fail_transiently(self, *_args, **_kwargs):
        raise ImageAnalysisError("Vision analysis failed transiently", retryable=True, provider_status_code=429)

    monkeypatch.setattr(tasks, "async_session", SessionFactory(session))
    monkeypatch.setattr(ImagePipeline, "process", fail_transiently)

    with pytest.raises(ImageAnalysisError):
        await tasks.process_image(
            {"redis": redis, "embedder": object(), "llm": object()},
            job_id=str(job_id),
            tenant_id="tenant-a",
        )

    assert job.status == "failed"
    assert item.metadata_["image_analysis"]["vision"]["error"]["retryable"] is True
    assert item.metadata_["image_analysis"]["vision"]["error"]["provider_status_code"] == 429


@pytest.mark.asyncio
async def test_embed_item_refreshes_relationships_after_reindex(monkeypatch) -> None:
    item_id = uuid.uuid4()
    item = Item(
        id=item_id,
        tenant_id="tenant-a",
        title="Fresh note",
        source_type="note",
        status="ready",
        raw_content="Latest memory body",
        summary="Previous summary",
        content_chunks=[{"index": 0, "text": "stale chunk"}],
        content_hash="old-hash",
    )
    session = FakeEmbedItemSession(item)
    redis = FakeArqPool()
    process_calls: list[dict] = []

    async def fake_process_prebuilt_item(db, *, item, embedder, llm, tenant_id, job=None, enable_ai_enrichment=False):
        process_calls.append(
            {
                "tenant_id": tenant_id,
                "enable_ai_enrichment": enable_ai_enrichment,
                "item_id": item.id,
            }
        )
        return SimpleNamespace(status="completed", item_id=item.id)

    monkeypatch.setattr(tasks, "async_session", SessionFactory(session))
    monkeypatch.setattr(tasks, "process_prebuilt_item", fake_process_prebuilt_item)

    await tasks.embed_item(
        {"redis": redis, "embedder": object(), "llm": object()},
        item_id=str(item_id),
        tenant_id="tenant-a",
    )

    assert any("DELETE FROM embeddings" in sql for sql in session.execute_sql)
    assert item.content_chunks is None
    assert item.content_hash is None
    assert item.status == "processing"
    assert process_calls == [
        {
            "tenant_id": "tenant-a",
            "enable_ai_enrichment": True,
            "item_id": item_id,
        }
    ]
    assert redis.enqueued == [
        ("extract_relationships", {"item_id": str(item_id), "tenant_id": "tenant-a"}),
        (
            "mark_item_dirty_and_schedule",
            {
                "_queue_name": PALACE_WORKER_QUEUE,
                "item_id": str(item_id),
                "tenant_id": "tenant-a",
                "reason": "ingest",
            },
        ),
    ]


@pytest.mark.asyncio
async def test_memory_artifact_defers_relationships_when_job_policy_requests_it(monkeypatch) -> None:
    item_id = uuid.uuid4()
    job_id = uuid.uuid4()
    item = Item(
        id=item_id,
        tenant_id="tenant-a",
        title="Shared launch brief",
        source_type="note",
        raw_content="Launch notes",
        status="processing",
    )
    job = Job(
        id=job_id,
        item_id=item_id,
        job_type="memory_artifact",
        tenant_id="tenant-a",
        status="queued",
        progress=0,
        payload={"relationship_policy": "deferred", "enable_ai_enrichment": False},
    )
    redis = FakeArqPool()
    process_calls: list[dict] = []

    async def fake_process_prebuilt_item(db, *, item, embedder, llm, tenant_id, job=None, enable_ai_enrichment=False):
        process_calls.append(
            {
                "tenant_id": tenant_id,
                "job_id": job.id,
                "enable_ai_enrichment": enable_ai_enrichment,
            }
        )
        return SimpleNamespace(status="completed", item_id=item.id)

    monkeypatch.setattr(tasks, "async_session", SessionFactory(FakeMemoryArtifactSession(job, item)))
    monkeypatch.setattr(tasks, "process_prebuilt_item", fake_process_prebuilt_item)

    await tasks.memory_artifact({"redis": redis, "embedder": object(), "llm": object()}, job_id=str(job_id))

    assert process_calls == [
        {
            "tenant_id": "tenant-a",
            "job_id": job_id,
            "enable_ai_enrichment": False,
        }
    ]
    assert redis.enqueued == [
        (
            "mark_item_dirty_and_schedule",
            {
                "_queue_name": PALACE_WORKER_QUEUE,
                "item_id": str(item_id),
                "tenant_id": "tenant-a",
                "reason": "memory-write",
            },
        )
    ]


@pytest.mark.asyncio
async def test_backfill_deferred_relationships_throttles_memory_items(monkeypatch) -> None:
    item_ids = [uuid.uuid4(), uuid.uuid4()]
    session = FakeBackfillSession(item_ids)
    redis = FakeArqPool()

    monkeypatch.setattr(tasks, "async_session", SessionFactory(session))

    queued = await tasks.backfill_deferred_relationships(
        {"redis": redis},
        tenant_id="tenant-a",
        limit=2,
        defer_seconds=9,
    )

    assert queued == 2
    lock_sql, lock_params = session.execute_calls[0]
    assert "pg_try_advisory_xact_lock" in lock_sql
    assert lock_params == {"lock_key": "relationship-backfill:tenant-a"}
    backfill_sql, backfill_params = session.execute_calls[1]
    assert "i.metadata ? 'memory_entry'" in backfill_sql
    assert "NOT EXISTS" in backfill_sql
    assert backfill_params == {"tenant_id": "tenant-a", "limit": 2}
    assert redis.enqueued == [
        ("extract_relationships", {"item_id": str(item_ids[0]), "tenant_id": "tenant-a"}),
        ("extract_relationships", {"item_id": str(item_ids[1]), "tenant_id": "tenant-a", "_defer_by": 9}),
    ]


@pytest.mark.asyncio
async def test_backfill_deferred_relationships_skips_when_tenant_lock_is_active(monkeypatch) -> None:
    session = FakeBackfillSession([uuid.uuid4()], lock_acquired=False)
    redis = FakeArqPool()

    monkeypatch.setattr(tasks, "async_session", SessionFactory(session))

    queued = await tasks.backfill_deferred_relationships(
        {"redis": redis},
        tenant_id="tenant-a",
        limit=2,
        defer_seconds=9,
    )

    assert queued == 0
    assert len(session.execute_calls) == 1
    assert "pg_try_advisory_xact_lock" in session.execute_calls[0][0]
    assert redis.enqueued == []
