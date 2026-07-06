import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path

import app.api.ingest as ingest_api
import httpx
import app.api.capture as capture_api
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.capture import router as capture_router
from app.api.ingest import router
from app.auth import AuthContext, verify_capture_write_auth, verify_memory_auth
from app.database import get_db
from app.models.item import Item
from app.models.job import Job
from app.models.web_save import WebSave
from app.workers.queues import DEFAULT_WORKER_QUEUE, MEDIA_FAIR_DISPATCH_TASK_NAME, singleton_job_id


PNG_1X1_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89"
)
_REAL_HTTPX_ASYNC_CLIENT = httpx.AsyncClient


class _ScalarResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def scalars(self):
        return self

    def all(self) -> list[object]:
        return self._rows

    def first(self) -> object | None:
        return self._rows[0] if self._rows else None


class _RowResult:
    def __init__(self, rows: list[tuple[object, object | None]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[object, object | None]]:
        return self._rows


class FakeSession:
    def __init__(
        self,
        existing_items: list[Item] | None = None,
        existing_web_saves: list[WebSave] | None = None,
    ) -> None:
        self.existing_items = existing_items or []
        self.existing_web_saves = existing_web_saves or []
        self.added_items: list[Item] = []
        self.added_jobs: list[Job] = []
        self.added_web_saves: list[WebSave] = []
        self.deleted: list[object] = []
        self.commits = 0
        self.flushes = 0
        self.rollbacks = 0
        self.scalar_values: list[object | None] = []

    def _assign_missing_ids(self) -> None:
        for value in [*self.added_items, *self.added_jobs, *self.added_web_saves]:
            if getattr(value, "id", None) is None:
                value.id = uuid.uuid4()

    async def execute(self, statement):
        statement_text = str(statement)
        if "web_saves" in statement_text:
            if "LEFT OUTER JOIN items" in statement_text:
                rows = [
                    (
                        web_save,
                        next((item for item in self.existing_items if item.id == web_save.item_id), None),
                    )
                    for web_save in self.existing_web_saves
                ]
                return _RowResult(rows)
            if "JOIN items" in statement_text:
                active_web_saves = []
                for web_save in self.existing_web_saves:
                    item = next((item for item in self.existing_items if item.id == web_save.item_id), None)
                    if item is not None and item.deleted_at is None and item.status != "deleted":
                        active_web_saves.append(web_save)
                return _ScalarResult(active_web_saves)
            return _ScalarResult(self.existing_web_saves)
        return _ScalarResult(self.existing_items)

    async def scalar(self, statement):
        if self.scalar_values:
            return self.scalar_values.pop(0)
        return None

    def add(self, value) -> None:
        if isinstance(value, Item):
            self.added_items.append(value)
            return
        if isinstance(value, Job):
            self.added_jobs.append(value)
            return
        if isinstance(value, WebSave):
            self.added_web_saves.append(value)
            return
        raise AssertionError(f"Unexpected added model: {type(value)!r}")

    async def delete(self, value) -> None:
        self.deleted.append(value)

    async def flush(self) -> None:
        self.flushes += 1
        self._assign_missing_ids()

    async def commit(self) -> None:
        self.commits += 1
        self._assign_missing_ids()

    async def refresh(self, value) -> None:
        if getattr(value, "id", None) is None:
            value.id = uuid.uuid4()

    async def rollback(self) -> None:
        self.rollbacks += 1


class FakeArqPool:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        fail_on_calls: set[int] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.enqueued: list[tuple[str, dict]] = []
        self.error = error
        self.fail_on_calls = set(fail_on_calls or ())

    async def enqueue_job(self, name: str, **kwargs) -> None:
        call = (name, kwargs)
        self.calls.append(call)
        if self.error is not None and (
            not self.fail_on_calls or len(self.calls) in self.fail_on_calls
        ):
            raise self.error
        self.enqueued.append((name, kwargs))


def _client(
    session: FakeSession,
    *,
    arq_pool: FakeArqPool | None = None,
    raise_server_exceptions: bool = True,
) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.include_router(capture_router, prefix="/api/v1")
    app.state.arq_pool = arq_pool or FakeArqPool()

    async def override_get_db():
        yield session

    async def override_verify(request: Request):
        request.state.auth_context = AuthContext(tenant_id="tenant-a", auth_mode="api_key", token_hash_reference="key-hash")
        request.state.tenant_id = "tenant-a"
        request.state.key_hash = "key-hash"
        request.state.auth_mode = "api_key"
        return "raw-key"

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[verify_memory_auth] = override_verify
    app.dependency_overrides[verify_capture_write_auth] = override_verify
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def _assert_failed_enqueue_state(item: Item, job: Job, *, message_substring: str) -> None:
    assert item.status == "failed"
    assert job.status == "failed"
    assert job.completed_at is not None
    assert isinstance(job.completed_at, datetime)
    assert job.error_message is not None
    assert message_substring in job.error_message


class _MockImageCandidateClient:
    def __init__(self, handler):
        self._client = _REAL_HTTPX_ASYNC_CLIENT(transport=httpx.MockTransport(handler))

    async def __aenter__(self):
        return self._client

    async def __aexit__(self, exc_type, exc, tb):
        await self._client.aclose()


def _allow_public_image_candidate_dns(monkeypatch) -> None:
    monkeypatch.setattr(capture_api.socket, "getaddrinfo", lambda *_args, **_kwargs: [(None, None, None, None, ("93.184.216.34", 0))])


def _mock_image_candidate_downloads(monkeypatch, handler) -> None:
    monkeypatch.setattr(capture_api.httpx, "AsyncClient", lambda **_kwargs: _MockImageCandidateClient(handler))


def test_ingest_webpage_replaces_failed_item_for_same_source_url() -> None:
    failed_item = Item(
        id=uuid.uuid4(),
        source_type="webpage",
        source_url="https://x.com/Zephyr_hg/status/2051708305819435445",
        title="Failed post",
        tenant_id="tenant-a",
        status="failed",
    )
    session = FakeSession(existing_items=[failed_item])
    arq_pool = FakeArqPool()
    client = _client(session, arq_pool=arq_pool)

    response = client.post(
        "/api/v1/ingest/webpage",
        json={"url": "https://x.com/Zephyr_hg/status/2051708305819435445"},
    )

    assert response.status_code == 202
    assert session.deleted == [failed_item]
    assert len(session.added_items) == 1
    assert session.added_items[0].source_url == "https://x.com/Zephyr_hg/status/2051708305819435445"
    assert arq_pool.enqueued[0][0] == "process_webpage"


def test_ingest_media_replaces_failed_item_for_same_source_url() -> None:
    failed_item = Item(
        id=uuid.uuid4(),
        source_type="media",
        source_url="https://example.com/watch?v=media",
        title="Failed media",
        tenant_id="tenant-a",
        status="failed",
    )
    session = FakeSession(existing_items=[failed_item])
    arq_pool = FakeArqPool()
    client = _client(session, arq_pool=arq_pool)

    response = client.post(
        "/api/v1/ingest/media",
        json={"url": "https://example.com/watch?v=media", "model": "media-model"},
    )

    assert response.status_code == 202
    assert session.deleted == [failed_item]
    assert len(session.added_items) == 1
    assert session.added_items[0].source_type == "media"
    assert session.added_items[0].source_url == "https://example.com/watch?v=media"
    assert arq_pool.enqueued == [
        (
            MEDIA_FAIR_DISPATCH_TASK_NAME,
            {
                "_queue_name": DEFAULT_WORKER_QUEUE,
                "_job_id": singleton_job_id(MEDIA_FAIR_DISPATCH_TASK_NAME, "media"),
            },
        )
    ]


def test_ingest_webpage_rejects_active_item_for_same_source_url() -> None:
    ready_item = Item(
        id=uuid.uuid4(),
        source_type="webpage",
        source_url="https://example.com/story",
        title="Existing story",
        tenant_id="tenant-a",
        status="ready",
    )
    session = FakeSession(existing_items=[ready_item])
    arq_pool = FakeArqPool()
    client = _client(session, arq_pool=arq_pool)

    response = client.post(
        "/api/v1/ingest/webpage",
        json={"url": "https://example.com/story"},
    )

    assert response.status_code == 409
    assert "URL already ingested" in response.json()["detail"]
    assert session.deleted == []
    assert session.added_items == []
    assert arq_pool.enqueued == []


def test_ingest_webpage_allows_deleted_item_for_same_source_url() -> None:
    deleted_item = Item(
        id=uuid.uuid4(),
        source_type="webpage",
        source_url="https://x.com/user/status/1234567890",
        title="Deleted post",
        tenant_id="tenant-a",
        status="deleted",
        deleted_at=datetime.now(timezone.utc),
    )
    session = FakeSession(existing_items=[deleted_item])
    arq_pool = FakeArqPool()
    client = _client(session, arq_pool=arq_pool)

    response = client.post(
        "/api/v1/ingest/webpage",
        json={"url": "https://x.com/user/status/1234567890"},
    )

    assert response.status_code == 202
    assert session.deleted == []
    assert len(session.added_items) == 1
    assert session.added_items[0].source_url == "https://x.com/user/status/1234567890"
    assert arq_pool.enqueued[0][0] == "process_webpage"


def test_browser_capture_routes_media_to_media_queue_and_preserves_metadata() -> None:
    session = FakeSession()
    arq_pool = FakeArqPool()
    client = _client(session, arq_pool=arq_pool)

    response = client.post(
        "/api/v1/capture/browser",
        json={
            "url": "https://youtube.com/watch?v=abc123",
            "page_title": "Launch demo",
            "detected_kind": "webpage",
            "tags": [" demo ", "demo", "video"],
            "browser_extension_version": "0.2.0",
            "extension_metadata": {"tab_id": 42},
            "model": "media-model",
        },
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["kind"] == "media"
    assert payload["route"] == "media"
    assert payload["source_url"] == "https://youtube.com/watch?v=abc123"

    item = session.added_items[0]
    job = session.added_jobs[0]
    assert item.source_type == "media"
    assert item.source_url == "https://youtube.com/watch?v=abc123"
    assert item.title == "Launch demo"
    assert item.tags == ["demo", "video"]
    assert item.metadata_["browser_capture"] == {
        "source_url": "https://youtube.com/watch?v=abc123",
        "source_title": "Launch demo",
        "capture_kind": "media",
        "client_detected_kind": "webpage",
        "route": "media",
        "browser_extension_version": "0.2.0",
        "tags": ["demo", "video"],
        "extension_metadata": {"tab_id": 42},
    }
    assert job.payload == {
        "retry_task": {
            "name": "process_media",
            "kwargs": {"url": "https://youtube.com/watch?v=abc123", "model": "media-model"},
        }
    }
    assert arq_pool.enqueued == [
        (
            MEDIA_FAIR_DISPATCH_TASK_NAME,
            {
                "_queue_name": DEFAULT_WORKER_QUEUE,
                "_job_id": singleton_job_id(MEDIA_FAIR_DISPATCH_TASK_NAME, "media"),
            },
        )
    ]


def test_browser_capture_routes_webpage_to_process_webpage_and_preserves_metadata() -> None:
    session = FakeSession()
    arq_pool = FakeArqPool()
    client = _client(session, arq_pool=arq_pool)

    response = client.post(
        "/api/v1/capture/browser",
        json={
            "url": "HTTPS://Example.COM/story?utm=1#section",
            "page_title": "Example Story",
            "tags": ["research"],
        },
        headers={"X-Palace-Extension-Version": "0.1.9"},
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["kind"] == "webpage"
    assert payload["route"] == "webpage"
    assert payload["source_url"] == "https://example.com/story?utm=1#section"

    item = session.added_items[0]
    assert item.source_type == "webpage"
    assert item.source_url == "https://example.com/story?utm=1#section"
    assert item.metadata_["browser_capture"]["source_title"] == "Example Story"
    assert item.metadata_["browser_capture"]["browser_extension_version"] == "0.1.9"
    web_save = session.added_web_saves[0]
    assert web_save.item_id == item.id
    assert web_save.original_url == "HTTPS://Example.COM/story?utm=1#section"
    assert web_save.normalized_url == "https://example.com/story?utm=1#section"
    assert web_save.source_title == "Example Story"
    assert web_save.source_domain == "example.com"
    assert web_save.capture_kind == "webpage"
    assert web_save.user_tags == ["research"]
    assert web_save.extension_version == "0.1.9"
    assert arq_pool.enqueued[0] == (
        "process_webpage",
        {
            "job_id": str(session.added_jobs[0].id),
            "url": "https://example.com/story?utm=1#section",
            "tenant_id": "tenant-a",
            "model": None,
        },
    )


def test_browser_capture_routes_social_post_to_webpage_ingest() -> None:
    session = FakeSession()
    arq_pool = FakeArqPool()
    client = _client(session, arq_pool=arq_pool)

    response = client.post(
        "/api/v1/capture/browser",
        json={
            "url": "https://x.com/Zephyr_hg/status/2051708305819435445",
            "detected_kind": "social_post",
        },
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["kind"] == "social_post"
    assert payload["route"] == "webpage"
    assert session.added_items[0].source_type == "webpage"
    assert session.added_items[0].metadata_["browser_capture"]["capture_kind"] == "social_post"
    assert session.added_web_saves[0].capture_kind == "social_post"
    assert arq_pool.enqueued[0][0] == "process_webpage"


def test_browser_capture_social_post_accepts_valid_image_candidates(monkeypatch) -> None:
    _allow_public_image_candidate_dns(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://pbs.twimg.com/media/post-image.jpg"
        return httpx.Response(
            200,
            headers={"content-type": "image/jpeg", "content-length": str(len(PNG_1X1_BYTES))},
            content=PNG_1X1_BYTES,
            request=request,
        )

    _mock_image_candidate_downloads(monkeypatch, handler)
    session = FakeSession()
    arq_pool = FakeArqPool()
    client = _client(session, arq_pool=arq_pool)

    response = client.post(
        "/api/v1/capture/browser",
        json={
            "url": "https://x.com/Zephyr_hg/status/2051708305819435445",
            "detected_kind": "social_post",
            "image_candidates": [
                {
                    "url": "https://pbs.twimg.com/media/post-image.jpg",
                    "source_post_url": "https://x.com/Zephyr_hg/status/2051708305819435445",
                    "alt_text": "diagram from the post",
                    "width": 1200,
                    "height": 675,
                    "role": "post_image",
                    "order": 0,
                }
            ],
        },
    )

    assert response.status_code == 202
    assert len(session.added_items) == 2
    parent_item, child_item = session.added_items
    image_hash = hashlib.sha256(PNG_1X1_BYTES).hexdigest()
    linked_candidates = parent_item.metadata_["browser_capture"]["image_candidates"]
    assert linked_candidates == [
        {
            "item_id": str(child_item.id),
            "candidate_url": "https://pbs.twimg.com/media/post-image.jpg",
            "final_url": "https://pbs.twimg.com/media/post-image.jpg",
            "media_type": "image/jpeg",
            "byte_hash": image_hash,
            "byte_size": len(PNG_1X1_BYTES),
            "order": 0,
        }
    ]
    assert child_item.source_type == "image_candidate"
    assert child_item.source_url is None
    assert child_item.title == "diagram from the post"
    assert child_item.status == "captured"
    assert child_item.content_hash is None
    assert child_item.metadata_["browser_capture_image"] == {
        "source": "browser_image_candidate",
        "status": "captured_not_processed",
        "parent_item_id": str(parent_item.id),
        "source_post_url": "https://x.com/Zephyr_hg/status/2051708305819435445",
        "candidate_url": "https://pbs.twimg.com/media/post-image.jpg",
        "final_url": "https://pbs.twimg.com/media/post-image.jpg",
        "media_type": "image/jpeg",
        "byte_hash": image_hash,
        "byte_size": len(PNG_1X1_BYTES),
        "order": 0,
        "alt_text": "diagram from the post",
        "role": "post_image",
        "dimensions": {"width": 1200, "height": 675},
    }
    assert session.added_web_saves[0].metadata_["browser_capture"]["preview_media"] == linked_candidates
    assert arq_pool.enqueued[0][0] == "process_webpage"


def test_browser_capture_rejects_private_network_image_candidate_without_creating_item() -> None:
    session = FakeSession()
    arq_pool = FakeArqPool()
    client = _client(session, arq_pool=arq_pool)

    response = client.post(
        "/api/v1/capture/browser",
        json={
            "url": "https://x.com/Zephyr_hg/status/2051708305819435445",
            "detected_kind": "social_post",
            "image_candidates": [{"url": "http://127.0.0.1/private.png"}],
        },
    )

    assert response.status_code == 422
    assert "not allowed" in response.json()["detail"]
    assert session.added_items == []
    assert session.added_jobs == []
    assert arq_pool.enqueued == []


def test_browser_capture_rejects_redirect_to_private_image_candidate(monkeypatch) -> None:
    _allow_public_image_candidate_dns(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "http://127.0.0.1/private.png"},
            request=request,
        )

    _mock_image_candidate_downloads(monkeypatch, handler)
    session = FakeSession()
    arq_pool = FakeArqPool()
    client = _client(session, arq_pool=arq_pool)

    response = client.post(
        "/api/v1/capture/browser",
        json={
            "url": "https://x.com/Zephyr_hg/status/2051708305819435445",
            "detected_kind": "social_post",
            "image_candidates": [{"url": "https://pbs.twimg.com/media/post-image.jpg"}],
        },
    )

    assert response.status_code == 422
    assert "not allowed" in response.json()["detail"]
    assert session.added_items == []
    assert session.added_jobs == []
    assert arq_pool.enqueued == []


def test_browser_capture_rejects_untrusted_image_candidate_metadata(monkeypatch) -> None:
    _allow_public_image_candidate_dns(monkeypatch)
    session = FakeSession()
    arq_pool = FakeArqPool()
    client = _client(session, arq_pool=arq_pool)

    response = client.post(
        "/api/v1/capture/browser",
        json={
            "url": "https://x.com/Zephyr_hg/status/2051708305819435445",
            "detected_kind": "social_post",
            "image_candidates": [
                {
                    "url": "https://example.com/not-allowed.jpg",
                    "source_post_url": "https://x.com/other/status/1",
                }
            ],
        },
    )

    assert response.status_code == 422
    assert session.added_items == []
    assert session.added_jobs == []
    assert arq_pool.enqueued == []


def test_browser_capture_rejects_non_image_candidate_content_type(monkeypatch) -> None:
    _allow_public_image_candidate_dns(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<html></html>",
            request=request,
        )

    _mock_image_candidate_downloads(monkeypatch, handler)
    session = FakeSession()
    arq_pool = FakeArqPool()
    client = _client(session, arq_pool=arq_pool)

    response = client.post(
        "/api/v1/capture/browser",
        json={
            "url": "https://x.com/Zephyr_hg/status/2051708305819435445",
            "detected_kind": "social_post",
            "image_candidates": [{"url": "https://pbs.twimg.com/media/post-image.jpg"}],
        },
    )

    assert response.status_code == 422
    assert "content type" in response.json()["detail"]
    assert session.added_items == []
    assert session.added_jobs == []
    assert arq_pool.enqueued == []


def test_browser_capture_rejects_oversized_streamed_image_candidate(monkeypatch) -> None:
    _allow_public_image_candidate_dns(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "image/jpeg"},
            content=b"x" * (capture_api._CANDIDATE_IMAGE_SIZE_LIMIT + 1),
            request=request,
        )

    _mock_image_candidate_downloads(monkeypatch, handler)
    session = FakeSession()
    arq_pool = FakeArqPool()
    client = _client(session, arq_pool=arq_pool)

    response = client.post(
        "/api/v1/capture/browser",
        json={
            "url": "https://x.com/Zephyr_hg/status/2051708305819435445",
            "detected_kind": "social_post",
            "image_candidates": [{"url": "https://pbs.twimg.com/media/post-image.jpg"}],
        },
    )

    assert response.status_code == 413
    assert session.added_items == []
    assert session.added_jobs == []
    assert arq_pool.enqueued == []


def test_browser_capture_dedupes_duplicate_image_candidates_before_insert(monkeypatch) -> None:
    _allow_public_image_candidate_dns(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "image/jpeg"},
            content=PNG_1X1_BYTES,
            request=request,
        )

    _mock_image_candidate_downloads(monkeypatch, handler)
    session = FakeSession()
    arq_pool = FakeArqPool()
    client = _client(session, arq_pool=arq_pool)

    response = client.post(
        "/api/v1/capture/browser",
        json={
            "url": "https://x.com/Zephyr_hg/status/2051708305819435445",
            "detected_kind": "social_post",
            "image_candidates": [
                {"url": "https://pbs.twimg.com/media/post-image.jpg", "order": 0},
                {"url": "https://pbs.twimg.com/media/post-image.jpg", "order": 1},
            ],
        },
    )

    assert response.status_code == 202
    assert len(session.added_items) == 2
    linked_candidates = session.added_items[0].metadata_["browser_capture"]["image_candidates"]
    assert len(linked_candidates) == 1
    assert linked_candidates[0]["item_id"] == str(session.added_items[1].id)
    assert linked_candidates[0]["order"] == 0
    assert arq_pool.enqueued[0][0] == "process_webpage"


def test_browser_capture_rejects_too_many_image_candidates() -> None:
    session = FakeSession()
    arq_pool = FakeArqPool()
    client = _client(session, arq_pool=arq_pool)

    response = client.post(
        "/api/v1/capture/browser",
        json={
            "url": "https://x.com/Zephyr_hg/status/2051708305819435445",
            "detected_kind": "social_post",
            "image_candidates": [
                {"url": f"https://pbs.twimg.com/media/post-image-{index}.jpg"}
                for index in range(5)
            ],
        },
    )

    assert response.status_code == 422
    assert session.added_items == []
    assert session.added_jobs == []
    assert arq_pool.enqueued == []


def test_browser_capture_marks_linked_image_candidates_failed_when_enqueue_fails(monkeypatch) -> None:
    _allow_public_image_candidate_dns(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "image/jpeg"},
            content=PNG_1X1_BYTES,
            request=request,
        )

    _mock_image_candidate_downloads(monkeypatch, handler)
    session = FakeSession()
    arq_pool = FakeArqPool(error=RuntimeError("redis unavailable"))
    client = _client(session, arq_pool=arq_pool, raise_server_exceptions=False)

    response = client.post(
        "/api/v1/capture/browser",
        json={
            "url": "https://x.com/Zephyr_hg/status/2051708305819435445",
            "detected_kind": "social_post",
            "image_candidates": [{"url": "https://pbs.twimg.com/media/post-image.jpg"}],
        },
    )

    assert response.status_code == 503
    parent_item, child_item = session.added_items
    assert parent_item.status == "failed"
    assert child_item.status == "failed"
    assert session.added_web_saves[0].archived_at is not None
    assert arq_pool.enqueued == []


def test_browser_capture_routes_selection_to_note_with_tags_and_source_metadata() -> None:
    source_item_id = uuid.uuid4()
    source_web_save_id = uuid.uuid4()
    session = FakeSession(
        existing_items=[
            Item(
                id=source_item_id,
                source_type="webpage",
                source_url="https://example.com/brief",
                title="Source brief",
                tenant_id="tenant-a",
                status="ready",
            )
        ],
        existing_web_saves=[
            WebSave(
                id=source_web_save_id,
                tenant_id="tenant-a",
                item_id=source_item_id,
                original_url="https://example.com/brief",
                normalized_url="https://example.com/brief",
                capture_kind="webpage",
            )
        ]
    )
    arq_pool = FakeArqPool()
    client = _client(session, arq_pool=arq_pool)

    response = client.post(
        "/api/v1/capture/browser",
        json={
            "url": "https://example.com/brief",
            "page_title": "Source brief",
            "selection_text": "Important selected passage for Palace.",
            "tags": ["clip", "clip", "priority"],
            "detected_kind": "webpage",
        },
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["kind"] == "selection_note"
    assert payload["route"] == "note"
    assert payload["source_url"] == "https://example.com/brief"

    item = session.added_items[0]
    job = session.added_jobs[0]
    assert item.source_type == "note"
    assert item.source_url is None
    assert item.title == "Source brief"
    assert item.tags == ["clip", "priority"]
    assert item.metadata_["browser_capture"] == {
        "source_url": "https://example.com/brief",
        "source_title": "Source brief",
        "capture_kind": "selection_note",
        "client_detected_kind": "webpage",
        "route": "note",
        "browser_extension_version": None,
        "tags": ["clip", "priority"],
        "extension_metadata": {},
        "source_web_save_id": str(source_web_save_id),
        "source_item_id": str(source_item_id),
        "captured_selection": {
            "char_count": 38,
            "summary": "Important selected passage for Palace.",
        },
    }
    assert session.added_web_saves == []
    assert arq_pool.enqueued == [
        (
            "process_note",
            {
                "job_id": str(job.id),
                "title": "Source brief",
                "content": "Important selected passage for Palace.",
                "tags": ["clip", "priority"],
                "tenant_id": "tenant-a",
                "model": None,
            },
        )
    ]


def test_browser_capture_rejects_invalid_or_missing_url_without_creating_item() -> None:
    session = FakeSession()
    arq_pool = FakeArqPool()
    client = _client(session, arq_pool=arq_pool)

    invalid_response = client.post("/api/v1/capture/browser", json={"url": "ftp://example.com/file"})
    missing_response = client.post("/api/v1/capture/browser", json={})

    assert invalid_response.status_code == 422
    assert missing_response.status_code == 422
    assert session.added_items == []
    assert session.added_jobs == []
    assert arq_pool.enqueued == []


def test_browser_capture_returns_duplicate_no_op_for_existing_active_web_save() -> None:
    ready_item_id = uuid.uuid4()
    existing_web_save_id = uuid.uuid4()
    ready_item = Item(
        id=ready_item_id,
        source_type="webpage",
        source_url="https://example.com/story",
        title="Existing story",
        tenant_id="tenant-a",
        status="ready",
    )
    existing_web_save = WebSave(
        id=existing_web_save_id,
        tenant_id="tenant-a",
        item_id=ready_item_id,
        original_url="https://example.com/story",
        normalized_url="https://example.com/story",
        capture_kind="webpage",
    )
    session = FakeSession(existing_items=[ready_item], existing_web_saves=[existing_web_save])
    arq_pool = FakeArqPool()
    client = _client(session, arq_pool=arq_pool)

    response = client.post("/api/v1/capture/browser", json={"url": "https://example.com/story"})

    assert response.status_code == 202
    assert response.json() == {
        "job_id": None,
        "item_id": str(ready_item_id),
        "status": "duplicate",
        "kind": "webpage",
        "route": "webpage",
        "source_url": "https://example.com/story",
        "duplicate_of": str(ready_item_id),
        "web_save_id": str(existing_web_save_id),
    }
    assert session.added_items == []
    assert session.added_jobs == []
    assert session.added_web_saves == []
    assert arq_pool.enqueued == []


def test_browser_capture_allows_recapture_when_existing_web_save_item_is_deleted() -> None:
    deleted_item_id = uuid.uuid4()
    deleted_at = datetime.now(timezone.utc)
    deleted_item = Item(
        id=deleted_item_id,
        source_type="webpage",
        source_url="https://example.com/story",
        title="Deleted story",
        tenant_id="tenant-a",
        status="deleted",
        deleted_at=deleted_at,
    )
    stale_web_save = WebSave(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        item_id=deleted_item_id,
        original_url="https://example.com/story",
        normalized_url="https://example.com/story",
        capture_kind="webpage",
    )
    session = FakeSession(existing_items=[deleted_item], existing_web_saves=[stale_web_save])
    arq_pool = FakeArqPool()
    client = _client(session, arq_pool=arq_pool)

    response = client.post("/api/v1/capture/browser", json={"url": "https://example.com/story"})

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert stale_web_save.archived_at is not None
    assert session.added_items[0].source_url == "https://example.com/story"
    assert session.added_web_saves[0].normalized_url == "https://example.com/story"
    assert arq_pool.enqueued[0][0] == "process_webpage"


def test_browser_capture_keeps_legacy_item_duplicate_conflict_without_web_save() -> None:
    ready_item = Item(
        id=uuid.uuid4(),
        source_type="webpage",
        source_url="https://example.com/story",
        title="Existing story",
        tenant_id="tenant-a",
        status="ready",
    )
    session = FakeSession(existing_items=[ready_item])
    arq_pool = FakeArqPool()
    client = _client(session, arq_pool=arq_pool)

    response = client.post("/api/v1/capture/browser", json={"url": "https://example.com/story"})

    assert response.status_code == 409
    assert "URL already ingested" in response.json()["detail"]
    assert session.added_items == []
    assert session.added_jobs == []
    assert arq_pool.enqueued == []


def test_browser_capture_marks_item_and_job_failed_when_enqueue_fails() -> None:
    session = FakeSession()
    arq_pool = FakeArqPool(error=RuntimeError("redis unavailable"))
    client = _client(session, arq_pool=arq_pool, raise_server_exceptions=False)

    response = client.post(
        "/api/v1/capture/browser",
        json={"url": "https://example.com/story", "page_title": "Story"},
    )

    assert response.status_code == 503
    item = session.added_items[0]
    job = session.added_jobs[0]
    web_save = session.added_web_saves[0]
    _assert_failed_enqueue_state(item, job, message_substring="redis unavailable")
    assert item.metadata_["browser_capture"]["source_url"] == "https://example.com/story"
    assert web_save.archived_at is not None
    assert arq_pool.enqueued == []


def test_ingest_batch_rejects_invalid_entries_before_creating_jobs_or_items() -> None:
    session = FakeSession()
    arq_pool = FakeArqPool()
    client = _client(session, arq_pool=arq_pool)

    response = client.post(
        "/api/v1/ingest/batch",
        json={
            "items": [
                {
                    "type": "note",
                    "title": "Valid note that should not be processed",
                    "content": "This batch should fail before any inserts or enqueues.",
                    "model": "note-model",
                },
                {
                    "type": "webpage",
                    "model": "page-model",
                },
            ]
        },
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "url required for type webpage"}
    assert session.added_items == []
    assert session.added_jobs == []
    assert session.commits == 0
    assert arq_pool.enqueued == []


def test_ingest_batch_rejects_invalid_urls_before_creating_jobs_or_items() -> None:
    session = FakeSession()
    arq_pool = FakeArqPool()
    client = _client(session, arq_pool=arq_pool)

    response = client.post(
        "/api/v1/ingest/batch",
        json={
            "items": [
                {
                    "type": "media",
                    "url": "ftp://example.com/audio",
                    "model": "media-model",
                }
            ]
        },
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid URL"}
    assert session.added_items == []
    assert session.added_jobs == []
    assert session.commits == 0
    assert arq_pool.enqueued == []


def test_ingest_media_routes_to_bounded_media_queue() -> None:
    session = FakeSession()
    arq_pool = FakeArqPool()
    client = _client(session, arq_pool=arq_pool)

    response = client.post(
        "/api/v1/ingest/media",
        json={"url": "https://example.com/watch?v=media", "model": "media-model"},
    )

    assert response.status_code == 202
    assert len(session.added_items) == 1
    assert len(session.added_jobs) == 1

    job = session.added_jobs[0]
    assert job.job_type == "media"
    assert arq_pool.enqueued == [
        (
            MEDIA_FAIR_DISPATCH_TASK_NAME,
            {
                "_queue_name": DEFAULT_WORKER_QUEUE,
                "_job_id": singleton_job_id(MEDIA_FAIR_DISPATCH_TASK_NAME, "media"),
            },
        )
    ]


def test_ingest_batch_enqueues_note_and_webpage_with_expected_payloads() -> None:
    session = FakeSession()
    arq_pool = FakeArqPool()
    client = _client(session, arq_pool=arq_pool)

    response = client.post(
        "/api/v1/ingest/batch",
        json={
            "items": [
                {
                    "type": "note",
                    "title": "Quarterly planning",
                    "content": "Capture decisions and follow-ups.",
                    "model": "note-model",
                },
                {
                    "type": "webpage",
                    "url": "https://example.com/story",
                    "model": "page-model",
                },
            ]
        },
    )

    assert response.status_code == 202

    payload = response.json()
    assert payload["total"] == 2
    assert [result["status"] for result in payload["results"]] == ["queued", "queued"]

    assert len(session.added_items) == 2
    assert len(session.added_jobs) == 2

    note_item, webpage_item = session.added_items
    note_job, webpage_job = session.added_jobs

    assert note_item.source_type == "note"
    assert note_item.title == "Quarterly planning"
    assert note_item.source_url is None
    assert note_item.tenant_id == "tenant-a"

    assert webpage_item.source_type == "webpage"
    assert webpage_item.title == "https://example.com/story"
    assert webpage_item.source_url == "https://example.com/story"
    assert webpage_item.tenant_id == "tenant-a"

    assert note_job.item_id == note_item.id
    assert note_job.job_type == "note"
    assert note_job.tenant_id == "tenant-a"
    assert note_job.payload is not None
    assert note_job.payload == {
        "retry_task": {
            "name": "process_note",
            "kwargs": {
                "tenant_id": "tenant-a",
                "model": "note-model",
                "title": "Quarterly planning",
                "content": "Capture decisions and follow-ups.",
            },
        }
    }

    assert webpage_job.item_id == webpage_item.id
    assert webpage_job.job_type == "webpage"
    assert webpage_job.tenant_id == "tenant-a"
    assert webpage_job.payload is not None
    assert webpage_job.payload == {
        "retry_task": {
            "name": "process_webpage",
            "kwargs": {
                "tenant_id": "tenant-a",
                "model": "page-model",
                "url": "https://example.com/story",
            },
        }
    }

    assert arq_pool.enqueued == [
        (
            "process_note",
            {
                "job_id": str(note_job.id),
                "tenant_id": "tenant-a",
                "model": "note-model",
                "title": "Quarterly planning",
                "content": "Capture decisions and follow-ups.",
            },
        ),
        (
            "process_webpage",
            {
                "job_id": str(webpage_job.id),
                "tenant_id": "tenant-a",
                "model": "page-model",
                "url": "https://example.com/story",
            },
        ),
    ]


def test_ingest_batch_routes_media_without_moving_default_tasks() -> None:
    session = FakeSession()
    arq_pool = FakeArqPool()
    client = _client(session, arq_pool=arq_pool)

    response = client.post(
        "/api/v1/ingest/batch",
        json={
            "items": [
                {
                    "type": "media",
                    "url": "https://example.com/watch?v=media",
                    "model": "media-model",
                },
                {
                    "type": "note",
                    "title": "Quarterly planning",
                    "content": "Capture decisions and follow-ups.",
                    "model": "note-model",
                },
            ]
        },
    )

    assert response.status_code == 202
    media_job, note_job = session.added_jobs
    assert arq_pool.enqueued == [
        (
            MEDIA_FAIR_DISPATCH_TASK_NAME,
            {
                "_queue_name": DEFAULT_WORKER_QUEUE,
                "_job_id": singleton_job_id(MEDIA_FAIR_DISPATCH_TASK_NAME, "media"),
            },
        ),
        (
            "process_note",
            {
                "job_id": str(note_job.id),
                "tenant_id": "tenant-a",
                "model": "note-model",
                "title": "Quarterly planning",
                "content": "Capture decisions and follow-ups.",
            },
        ),
    ]


def test_ingest_note_marks_job_and_item_failed_when_enqueue_fails() -> None:
    session = FakeSession()
    arq_pool = FakeArqPool(error=RuntimeError("redis unavailable"))
    client = _client(
        session,
        arq_pool=arq_pool,
        raise_server_exceptions=False,
    )

    response = client.post(
        "/api/v1/ingest/note",
        json={
            "title": "Quarterly planning",
            "content": "Capture decisions and follow-ups.",
            "model": "note-model",
        },
    )

    assert response.status_code == 503
    assert "enqueue" in response.json()["detail"].lower()

    assert len(session.added_items) == 1
    assert len(session.added_jobs) == 1

    item = session.added_items[0]
    job = session.added_jobs[0]

    assert item.source_type == "note"
    assert item.title == "Quarterly planning"
    assert item.tenant_id == "tenant-a"

    assert job.item_id == item.id
    assert job.job_type == "note"
    assert job.tenant_id == "tenant-a"
    _assert_failed_enqueue_state(item, job, message_substring="redis unavailable")

    assert arq_pool.calls == [
        (
            "process_note",
            {
                "job_id": str(job.id),
                "title": "Quarterly planning",
                "content": "Capture decisions and follow-ups.",
                "tags": None,
                "tenant_id": "tenant-a",
                "model": "note-model",
            },
        )
    ]
    assert arq_pool.enqueued == []


def test_ingest_batch_marks_only_failed_enqueue_entries_failed() -> None:
    session = FakeSession()
    arq_pool = FakeArqPool(
        error=RuntimeError("redis unavailable"),
        fail_on_calls={2},
    )
    client = _client(
        session,
        arq_pool=arq_pool,
        raise_server_exceptions=False,
    )

    response = client.post(
        "/api/v1/ingest/batch",
        json={
            "items": [
                {
                    "type": "note",
                    "title": "Quarterly planning",
                    "content": "Capture decisions and follow-ups.",
                    "model": "note-model",
                },
                {
                    "type": "webpage",
                    "url": "https://example.com/story",
                    "model": "page-model",
                },
            ]
        },
    )

    assert response.status_code == 202

    payload = response.json()
    assert payload["total"] == 2
    assert [result["status"] for result in payload["results"]] == ["queued", "failed"]

    assert len(session.added_items) == 2
    assert len(session.added_jobs) == 2

    note_item, webpage_item = session.added_items
    note_job, webpage_job = session.added_jobs

    assert payload["results"][0]["job_id"] == str(note_job.id)
    assert payload["results"][0]["item_id"] == str(note_item.id)
    assert payload["results"][1]["job_id"] == str(webpage_job.id)
    assert payload["results"][1]["item_id"] == str(webpage_item.id)

    assert note_item.status == "processing"
    assert note_job.status == "queued"
    assert note_job.error_message is None
    assert note_job.completed_at is None

    assert webpage_item.source_type == "webpage"
    assert webpage_item.source_url == "https://example.com/story"
    assert webpage_job.item_id == webpage_item.id
    assert webpage_job.job_type == "webpage"
    _assert_failed_enqueue_state(webpage_item, webpage_job, message_substring="redis unavailable")

    assert arq_pool.calls == [
        (
            "process_note",
            {
                "job_id": str(note_job.id),
                "tenant_id": "tenant-a",
                "model": "note-model",
                "title": "Quarterly planning",
                "content": "Capture decisions and follow-ups.",
            },
        ),
        (
            "process_webpage",
            {
                "job_id": str(webpage_job.id),
                "tenant_id": "tenant-a",
                "model": "page-model",
                "url": "https://example.com/story",
            },
        ),
    ]
    assert arq_pool.enqueued == [arq_pool.calls[0]]


def test_ingest_doc_stores_upload_provenance_metadata(tmp_path: Path, monkeypatch) -> None:
    session = FakeSession()
    arq_pool = FakeArqPool()
    client = _client(session, arq_pool=arq_pool)
    doc_path = tmp_path / "brief.pdf"
    doc_path.write_text("placeholder", encoding="utf-8")
    upload_dir = tmp_path / "upload-artifacts"

    async def fake_stream_to_tmp(*_args, **_kwargs) -> str:
        return str(doc_path)

    def fake_extract_doc_from_path(path: str, filename: str) -> tuple[str, dict]:
        assert path == str(doc_path)
        assert filename == "brief.pdf"
        return "Recovered text", {"doc_title": "Recovered brief", "pages": 2}

    monkeypatch.setattr(ingest_api, "_stream_to_tmp", fake_stream_to_tmp)
    monkeypatch.setattr(ingest_api, "_extract_doc_from_path", fake_extract_doc_from_path)
    monkeypatch.setattr("app.services.bundle.settings.upload_artifact_dir", str(upload_dir))

    response = client.post(
        "/api/v1/ingest/doc",
        files={"file": ("brief.pdf", b"%PDF-1.7", "application/pdf")},
        data={"model": "doc-model"},
    )

    assert response.status_code == 202
    assert len(session.added_items) == 1
    assert len(session.added_jobs) == 1

    item = session.added_items[0]
    job = session.added_jobs[0]
    tenant_dir = hashlib.sha256(b"tenant-a").hexdigest()
    storage_path = upload_dir / tenant_dir / f"{item.id}.pdf"

    assert item.source_type == "doc"
    assert item.title == "Recovered brief"
    assert item.metadata_ == {
        "upload_artifact": {
            "source": "user_upload",
            "filename": "brief.pdf",
            "media_type": "application/pdf",
            "extension": ".pdf",
            "storage_path": str(storage_path),
        }
    }
    assert storage_path.read_text(encoding="utf-8") == "placeholder"
    assert job.payload == {
        "retry_task": {
            "name": "process_doc",
            "kwargs": {
                "extracted_text": "Recovered text",
                "doc_metadata": {"doc_title": "Recovered brief", "pages": 2},
                "model": "doc-model",
            },
        }
    }
    assert arq_pool.enqueued == [
        (
            "process_doc",
            {
                "job_id": str(job.id),
                "extracted_text": "Recovered text",
                "doc_metadata": {"doc_title": "Recovered brief", "pages": 2},
                "tenant_id": "tenant-a",
                "model": "doc-model",
            },
        )
    ]


def test_ingest_image_stores_upload_provenance_metadata(tmp_path: Path, monkeypatch) -> None:
    session = FakeSession()
    arq_pool = FakeArqPool()
    client = _client(session, arq_pool=arq_pool)
    image_path = tmp_path / "board.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    upload_dir = tmp_path / "upload-artifacts"

    async def fake_stream_to_tmp(*_args, **_kwargs) -> str:
        return str(image_path)

    monkeypatch.setattr(ingest_api, "_stream_to_tmp", fake_stream_to_tmp)
    monkeypatch.setattr("app.services.bundle.settings.upload_artifact_dir", str(upload_dir))

    response = client.post(
        "/api/v1/ingest/image",
        files={"file": ("board.png", b"png-bytes", "image/png")},
    )

    assert response.status_code == 202
    assert len(session.added_items) == 1
    assert len(session.added_jobs) == 1

    item = session.added_items[0]
    job = session.added_jobs[0]
    tenant_dir = hashlib.sha256(b"tenant-a").hexdigest()
    storage_path = upload_dir / tenant_dir / f"{item.id}.png"

    assert item.source_type == "image"
    assert item.title == "board.png"
    byte_hash = hashlib.sha256(b"\x89PNG\r\n\x1a\nfake").hexdigest()
    assert item.metadata_ == {
        "filename": "board.png",
        "media_type": "image/png",
        "upload_artifact": {
            "source": "user_upload",
            "filename": "board.png",
            "media_type": "image/png",
            "extension": ".png",
            "storage_path": str(storage_path),
        },
        "image_analysis": {
            "status": "queued",
            "caption": "",
            "visible_text": [],
            "objects": [],
            "entities": [],
            "dimensions": {"width": None, "height": None},
            "byte_hash": byte_hash,
            "byte_size": len(b"\x89PNG\r\n\x1a\nfake"),
            "artifact": {
                "source": "user_upload",
                "filename": "board.png",
                "media_type": "image/png",
                "extension": ".png",
                "storage_path": str(storage_path),
            },
            "vision": {
                "provider": "openai",
                "model": "gpt-4o-mini",
                "version": None,
                "confidence": None,
                "error": None,
            },
        },
    }
    assert storage_path.read_bytes() == b"\x89PNG\r\n\x1a\nfake"
    assert item.content_hash == byte_hash
    assert job.payload == {
        "retry_task": {
            "name": "process_image",
            "kwargs": {
                "image_metadata": {
                    "filename": "board.png",
                    "media_type": "image/png",
                    "image_analysis": item.metadata_["image_analysis"],
                },
            },
        }
    }
    assert arq_pool.enqueued == [
        (
            "process_image",
            {
                "job_id": str(job.id),
                "image_metadata": {
                    "filename": "board.png",
                    "media_type": "image/png",
                    "image_analysis": item.metadata_["image_analysis"],
                },
                "tenant_id": "tenant-a",
            },
        )
    ]


def test_ingest_image_records_dimensions_hash_and_disabled_native_compatibility(tmp_path: Path, monkeypatch) -> None:
    session = FakeSession()
    arq_pool = FakeArqPool()
    client = _client(session, arq_pool=arq_pool)
    image_path = tmp_path / "pixel.png"
    image_path.write_bytes(PNG_1X1_BYTES)
    upload_dir = tmp_path / "upload-artifacts"

    async def fake_stream_to_tmp(*_args, **_kwargs) -> str:
        return str(image_path)

    monkeypatch.setattr(ingest_api, "_stream_to_tmp", fake_stream_to_tmp)
    monkeypatch.setattr("app.services.bundle.settings.upload_artifact_dir", str(upload_dir))

    response = client.post(
        "/api/v1/ingest/image",
        files={"file": ("pixel.png", PNG_1X1_BYTES, "image/png")},
    )

    assert response.status_code == 202
    item = session.added_items[0]
    analysis = item.metadata_["image_analysis"]
    assert analysis["status"] == "queued"
    assert analysis["caption"] == ""
    assert analysis["dimensions"] == {"width": 1, "height": 1}
    assert analysis["byte_hash"] == hashlib.sha256(PNG_1X1_BYTES).hexdigest()
    assert analysis["byte_size"] == len(PNG_1X1_BYTES)
    assert analysis["visible_text"] == []
    assert analysis["objects"] == []
    assert analysis["entities"] == []
    assert analysis["vision"]["error"] is None
    assert "description" not in arq_pool.enqueued[0][1]


def test_ingest_image_duplicate_does_not_analyze_or_persist_artifact(tmp_path: Path, monkeypatch) -> None:
    existing_id = uuid.uuid4()
    session = FakeSession()
    session.scalar_values.append(existing_id)
    arq_pool = FakeArqPool()
    client = _client(session, arq_pool=arq_pool)
    image_path = tmp_path / "duplicate.png"
    image_path.write_bytes(PNG_1X1_BYTES)

    async def fake_stream_to_tmp(*_args, **_kwargs) -> str:
        return str(image_path)

    monkeypatch.setattr(ingest_api, "_stream_to_tmp", fake_stream_to_tmp)

    response = client.post(
        "/api/v1/ingest/image",
        files={"file": ("duplicate.png", PNG_1X1_BYTES, "image/png")},
    )

    assert response.status_code == 409
    assert str(existing_id) in response.json()["detail"]
    assert session.added_items == []
    assert session.added_jobs == []
    assert arq_pool.enqueued == []


def test_ingest_image_defers_vision_failure_to_worker_job(tmp_path: Path, monkeypatch) -> None:
    session = FakeSession()
    arq_pool = FakeArqPool()
    client = _client(session, arq_pool=arq_pool)
    image_path = tmp_path / "broken.png"
    image_path.write_bytes(PNG_1X1_BYTES)

    async def fake_stream_to_tmp(*_args, **_kwargs) -> str:
        return str(image_path)

    monkeypatch.setattr(ingest_api, "_stream_to_tmp", fake_stream_to_tmp)

    response = client.post(
        "/api/v1/ingest/image",
        files={"file": ("broken.png", PNG_1X1_BYTES, "image/png")},
    )

    assert response.status_code == 202
    assert len(session.added_items) == 1
    assert len(session.added_jobs) == 1
    assert session.added_items[0].metadata_["image_analysis"]["status"] == "queued"
    assert arq_pool.enqueued[0][0] == "process_image"
