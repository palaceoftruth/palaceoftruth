from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.search import router
from app import auth
from app.auth import AuthContext, verify_memory_auth
from app.database import get_db
from app.schemas.search import SearchResult


class FakeSession:
    pass


class _AuthMappingResult:
    def __init__(self, row) -> None:
        self._row = row

    def one_or_none(self):
        return self._row


class _AuthResult:
    def __init__(self, row) -> None:
        self._row = row

    def mappings(self):
        return _AuthMappingResult(self._row)


class AuthSession:
    def __init__(self, row) -> None:
        self.row = row
        self.updates: list[dict] = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, statement, params=None):
        sql = str(statement).lower()
        params = params or {}
        if "from mcp_oauth_access_tokens" in sql:
            return _AuthResult(self.row)
        if "update mcp_oauth_access_tokens" in sql or "update mcp_clients" in sql:
            self.updates.append(params)
            return _AuthResult(None)
        raise AssertionError(f"Unexpected auth SQL: {sql}")

    async def commit(self) -> None:
        self.commits += 1


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
        request.state.auth_context = AuthContext(
            tenant_id="tenant-a",
            auth_mode="api_key",
            token_hash_reference="key-hash",
        )
        request.state.tenant_id = "tenant-a"
        request.state.key_hash = "key-hash"
        request.state.auth_mode = "api_key"
        return "raw-key"

    monkeypatch.setattr("app.api.search.SearchService", service_cls)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[verify_memory_auth] = override_verify
    return TestClient(app)


def _oauth_client(monkeypatch, auth_session: AuthSession, service_cls=RecordingSearchService) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.embedder = object()

    async def override_get_db():
        yield FakeSession()

    monkeypatch.setattr("app.api.search.SearchService", service_cls)
    monkeypatch.setattr(auth, "async_session", lambda: auth_session)
    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def _oauth_token_row(*, scopes: list[str], resource: str | None = "https://testserver/mcp"):
    return {
        "token_id": uuid.uuid4(),
        "tenant_id": "tenant-a",
        "token_scopes": scopes,
        "token_resource": resource,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        "token_revoked_at": None,
        "client_id": uuid.uuid4(),
        "client_key": "codex-remote",
        "allowed_scopes": ["read", "write", "admin"],
        "client_revoked_at": None,
    }


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


def test_search_get_accepts_oauth_bearer_read_scope(monkeypatch) -> None:
    RecordingSearchService.instances.clear()
    auth_session = AuthSession(_oauth_token_row(scopes=["read"]))
    client = _oauth_client(monkeypatch, auth_session)

    response = client.get(
        "/api/v1/search",
        params={"q": "launch brief"},
        headers={"Authorization": "Bearer raw-token"},
    )

    assert response.status_code == 200
    assert len(RecordingSearchService.instances) == 1
    assert RecordingSearchService.instances[0].tenant_id == "tenant-a"
    assert auth_session.updates == [
        {"token_id": auth_session.row["token_id"]},
        {"client_id": auth_session.row["client_id"]},
    ]
    assert auth_session.commits == 1


def test_search_get_rejects_oauth_bearer_missing_read_scope(monkeypatch) -> None:
    ExplodingSearchService.init_calls = 0
    auth_session = AuthSession(_oauth_token_row(scopes=["write"]))
    client = _oauth_client(monkeypatch, auth_session, service_cls=ExplodingSearchService)

    response = client.get(
        "/api/v1/search",
        params={"q": "launch brief"},
        headers={"Authorization": "Bearer raw-token"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "MCP bearer token missing read scope"
    assert ExplodingSearchService.init_calls == 0
    assert auth_session.commits == 1


def test_search_get_rejects_oauth_bearer_wrong_resource(monkeypatch) -> None:
    ExplodingSearchService.init_calls = 0
    auth_session = AuthSession(_oauth_token_row(scopes=["read"], resource="https://wrong.example/api/v1"))
    client = _oauth_client(monkeypatch, auth_session, service_cls=ExplodingSearchService)

    response = client.get(
        "/api/v1/search",
        params={"q": "launch brief"},
        headers={"Authorization": "Bearer raw-token"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "MCP bearer token resource is invalid"
    assert ExplodingSearchService.init_calls == 0
    assert auth_session.updates == []
    assert auth_session.commits == 0


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
