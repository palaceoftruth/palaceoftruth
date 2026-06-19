from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.search import router
from app.auth import verify_api_key
from app.database import get_db
from app.schemas.search import SearchResult


class FakeSession:
    pass


class RecordingSearchService:
    instances: list["RecordingSearchService"] = []

    def __init__(self, db, embedder, tenant_id: str) -> None:
        self.db = db
        self.embedder = embedder
        self.tenant_id = tenant_id
        self.calls: list[dict] = []
        self.last_ranking_trace = {
            "ranking_features_version": 1,
            "source_ranking_enabled": False,
            "results": [{"item_id": "00000000-0000-0000-0000-000000000001", "adjustments": {}}],
        }
        RecordingSearchService.instances.append(self)

    async def vector_search(self, **kwargs):
        self.calls.append(kwargs)
        return [
            SearchResult(
                item_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
                title="Launch brief",
                summary="Stored summary",
                source_type="note",
                source_url="https://example.test/brief",
                tags=["alpha"],
                source_project="palaceoftruth",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                chunk_text="Sensitive launch content",
                chunk_index=2,
                score=0.91,
            )
        ]


class ExplodingSearchService:
    init_calls = 0

    def __init__(self, *_args, **_kwargs) -> None:
        ExplodingSearchService.init_calls += 1
        raise AssertionError("search service should not be constructed on request validation failures")


def _client(monkeypatch, service_cls=RecordingSearchService) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.embedder = object()

    async def override_get_db():
        yield FakeSession()

    async def override_verify(request: Request):
        request.state.tenant_id = "tenant-a"
        request.state.key_hash = "key-hash"
        return "raw-key"

    monkeypatch.setattr("app.api.search.SearchService", service_cls)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[verify_api_key] = override_verify
    return TestClient(app)


def test_search_get_passes_valid_tags_mode_and_splits_csv(monkeypatch) -> None:
    RecordingSearchService.instances.clear()
    client = _client(monkeypatch)

    response = client.get(
        "/api/v1/search",
        params={
            "q": "launch brief",
            "limit": 3,
            "candidate_limit": 37,
            "tags": "alpha, beta, ,gamma ",
            "tags_mode": "all",
        },
    )

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["results"][0]["source_project"] == "palaceoftruth"
    assert "context_chunks" not in response.json()["results"][0]
    assert len(RecordingSearchService.instances) == 1

    service = RecordingSearchService.instances[0]
    assert service.tenant_id == "tenant-a"
    assert service.calls == [
        {
            "query": "launch brief",
            "limit": 3,
            "candidate_limit": 37,
            "include_neighbor_chunks": False,
            "neighbor_chunk_window": 1,
            "context_budget_chars": None,
            "source_type": None,
            "retrieval_lens": None,
            "tags": ["alpha", "beta", "gamma"],
            "tags_mode": "all",
            "date_from": None,
            "date_to": None,
            "min_score": None,
        }
    ]


def test_search_get_accepts_any_and_all_tags_mode_values(monkeypatch) -> None:
    for tags_mode in ("any", "all"):
        RecordingSearchService.instances.clear()
        client = _client(monkeypatch)

        response = client.get(
            "/api/v1/search",
            params={
                "q": "launch brief",
                "tags_mode": tags_mode,
            },
        )

        assert response.status_code == 200
        assert len(RecordingSearchService.instances) == 1
        assert RecordingSearchService.instances[0].calls[0]["tags_mode"] == tags_mode


def test_search_get_rejects_invalid_tags_mode_before_service_execution(monkeypatch) -> None:
    ExplodingSearchService.init_calls = 0
    client = _client(monkeypatch, service_cls=ExplodingSearchService)

    response = client.get(
        "/api/v1/search",
        params={
            "q": "launch brief",
            "tags_mode": "bogus",
        },
    )

    assert response.status_code == 422
    assert ExplodingSearchService.init_calls == 0


def test_search_capture_is_disabled_by_default(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("app.services.retrieval_capture.settings.retrieval_capture_enabled", False)
    monkeypatch.setattr("app.services.retrieval_capture.settings.retrieval_capture_path", str(tmp_path / "capture.ndjson"))
    client = _client(monkeypatch)

    response = client.post("/api/v1/search", json={"query": "secret launch brief", "top_k": 3})

    assert response.status_code == 200
    assert not (tmp_path / "capture.ndjson").exists()


def test_search_capture_writes_sanitized_replay_record(monkeypatch, tmp_path) -> None:
    capture_path = tmp_path / "capture.ndjson"
    monkeypatch.setattr("app.services.retrieval_capture.settings.retrieval_capture_enabled", True)
    monkeypatch.setattr("app.services.retrieval_capture.settings.retrieval_capture_path", str(capture_path))
    monkeypatch.setattr("app.services.retrieval_capture.settings.retrieval_capture_query_mode", "fingerprint")
    monkeypatch.setattr("app.services.retrieval_capture.settings.app_version", "test-sha")
    client = _client(monkeypatch)

    response = client.get("/api/v1/search", params={"q": "secret launch brief", "limit": 3, "tags": "alpha"})

    assert response.status_code == 200
    assert response.json()["trace"]["ranking_features_version"] == 1
    record = json.loads(capture_path.read_text(encoding="utf-8"))
    assert record["endpoint"] == "/api/v1/search"
    assert record["tenant_id"] == "tenant-a"
    assert record["app_version"] == "test-sha"
    assert record["request"]["query_mode"] == "fingerprint"
    assert "query_text" not in record["request"]
    assert record["request"]["tags"] == ["alpha"]
    assert record["results"] == [
        {
            "rank": 1,
            "item_id": "00000000-0000-0000-0000-000000000001",
            "chunk_index": 2,
            "score": 0.91,
            "source_type": "note",
            "source_project": "palaceoftruth",
            "tags": ["alpha"],
        }
    ]
    assert record["trace"]["ranking_features_version"] == 1
    assert record["trace"]["results"][0]["adjustments"] == {}
    assert "Sensitive launch content" not in capture_path.read_text(encoding="utf-8")
    assert "Launch brief" not in capture_path.read_text(encoding="utf-8")


def test_retrieval_capture_record_can_carry_labeled_expectations(monkeypatch) -> None:
    from app.services.retrieval_capture import build_capture_record

    monkeypatch.setattr("app.services.retrieval_capture.settings.retrieval_capture_query_mode", "fingerprint")
    record = build_capture_record(
        endpoint="/api/v1/search",
        tenant_id="tenant-a",
        query="secret launch brief",
        request_params={"limit": 3},
        results=[
            {
                "item_id": "item-a",
                "chunk_index": 0,
                "score": 0.99,
                "source_type": "note",
                "tags": ["alpha"],
                "retrieved_scope_label": "workspace/nist",
            }
        ],
        latency_ms=12.3456,
        expected_item_ids=["item-a"],
        forbidden_item_ids=["item-z"],
        query_type="known_item",
        expected_scope_label="workspace/nist",
        expected_route="Standards",
        expected_top_rank="item-a",
    )

    assert record["request"]["query_mode"] == "fingerprint"
    assert "query_text" not in record["request"]
    assert record["expectations"] == {
        "expected_item_ids": ["item-a"],
        "forbidden_item_ids": ["item-z"],
        "query_type": "known_item",
        "expected_scope_label": "workspace/nist",
        "expected_route": "Standards",
        "expected_top_rank": "item-a",
    }
    assert record["results"][0]["retrieved_scope_label"] == "workspace/nist"
