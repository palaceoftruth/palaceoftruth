from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.system import router
from app.auth import AuthContext, verify_memory_auth
from app.database import get_db
from app.services.prometheus_metrics import HttpMetricsRecorder


class _ScalarResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one(self):
        return self._value


class _RowsResult:
    def __init__(self, rows) -> None:
        self._rows = rows

    def all(self):
        return self._rows


class _MappingsResult:
    def __init__(self, rows) -> None:
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class StatsSession:
    def __init__(self) -> None:
        self.execute_sql: list[str] = []

    async def execute(self, statement):
        sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
        self.execute_sql.append(sql)
        lower = sql.lower()

        if lower.strip() == "select 1":
            return _ScalarResult(1)

        if "group by items.source_type" in lower:
            return _RowsResult(
                [
                    SimpleNamespace(source_type="note", n=2),
                    SimpleNamespace(source_type="doc", n=1),
                ]
            )
        if "count(distinct(embeddings.item_id))" in lower:
            return _ScalarResult(2)
        if "from embeddings join items" in lower and "count(*)" in lower:
            return _ScalarResult(7)
        if "from items" in lower and "item_relationships" in lower and "not (exists" in lower:
            return _ScalarResult(1)
        if "from items" in lower and "items.status != 'failed'" in lower and "count(*)" in lower:
            return _ScalarResult(3)
        if "from items" in lower and "items.status = 'ready'" in lower and "count(*)" in lower:
            return _ScalarResult(2)
        if "from jobs" in lower:
            if "job_type = 'memory_artifact'" in lower and "status = 'failed'" in lower:
                return _ScalarResult(2)
            if "job_type = 'memory_artifact'" in lower and ("status in ('queued', 'processing')" in lower or "status in (__[postcompile_status_1])" in lower):
                return _ScalarResult(3)
            return _ScalarResult(1)
        if "from feeds" in lower:
            return _ScalarResult(4)
        raise AssertionError(f"Unexpected SQL: {sql}")


class MetricsSession:
    async def execute(self, statement):
        sql = str(statement).lower()
        if "from jobs" in sql and "where job_type = 'memory_artifact'" in sql:
            return _MappingsResult([{"status": "queued", "count": 3}, {"status": "failed", "count": 1}])
        if "from jobs" in sql and "where webhook_url is not null" in sql:
            return _MappingsResult([{"status": "failed", "count": 2}, {"status": "completed", "count": 5}])
        if "from jobs" in sql and "group by job_type, status" in sql:
            return _MappingsResult(
                [
                    {"job_type": "memory_artifact", "status": "queued", "count": 3},
                    {"job_type": "media", "status": "failed", "count": 1},
                ]
            )
        if "from items" in sql and "group by source_type, status" in sql:
            return _MappingsResult(
                [
                    {"source_type": "note", "status": "ready", "count": 7},
                    {"source_type": "tenant-a-private-type", "status": "ready", "count": 3},
                    {"source_type": "pdf", "status": "processing", "count": 1},
                ]
            )
        if "from sync_runs" in sql:
            return _MappingsResult([{"status": "completed", "count": 4}])
        if "from palace_runs" in sql:
            return _MappingsResult([{"status": "queued", "count": 2}])
        if "from embeddings" in sql:
            return _MappingsResult([{"indexed_items": 6, "embedding_chunks": 42}])
        if "from palace_tenant_state" in sql:
            return _MappingsResult([{"dirty_items": 5, "backlog_generation": 9}])
        raise AssertionError(f"Unexpected SQL: {sql}")


class BrokenMetricsSession:
    async def execute(self, _statement):
        raise RuntimeError("database unavailable")


def _client(session: StatsSession) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.arq_pool = SimpleNamespace(ping=_async_ping)

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
    return TestClient(app)


def _metrics_client(session) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.arq_pool = None
    recorder = HttpMetricsRecorder()
    recorder.record(method="GET", route="/api/v1/health", status_code=200, duration_seconds=0.125)
    app.state.prometheus_http_metrics = recorder

    async def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


async def _async_ping():
    return True


def test_health_remains_probe_compatible() -> None:
    client = _client(StatsSession())

    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_version_exposes_public_app_version(monkeypatch) -> None:
    monkeypatch.setattr("app.api.system.settings.app_version", "sha-test")
    client = _client(StatsSession())

    response = client.get("/api/v1/version")

    assert response.status_code == 200
    assert response.json() == {"name": "Palace of Truth", "version": "sha-test"}


def test_ready_reports_dependency_state(monkeypatch) -> None:
    monkeypatch.setattr("app.api.system.settings.app_version", "sha-test")
    session = StatsSession()
    client = _client(session)

    response = client.get("/api/v1/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": "sha-test",
        "dependencies": {
            "database": {"status": "ok"},
            "queue": {"status": "ok", "ping": True},
        },
    }


def test_ready_degrades_when_queue_ping_unavailable(monkeypatch) -> None:
    monkeypatch.setattr("app.api.system.settings.app_version", "sha-test")
    session = StatsSession()
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    async def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    response = client.get("/api/v1/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["dependencies"]["queue"] == {
        "status": "degraded",
        "message": "ARQ pool unavailable",
    }


def test_stats_match_library_counts_and_explain_embedding_chunks() -> None:
    session = StatsSession()
    client = _client(session)

    response = client.get("/api/v1/stats")

    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "total_items": 3,
        "ready_items": 2,
        "by_source_type": {"note": 2, "doc": 1},
        "indexed_items": 2,
        "embedding_chunks": 7,
        "total_embeddings": 7,
        "orphaned_ready_items": 1,
        "active_jobs": 1,
        "failed_memory_jobs": 2,
        "active_memory_jobs": 3,
        "feed_count": 4,
    }
    assert any("items.status != 'failed'" in sql for sql in session.execute_sql)
    assert any("count(distinct(embeddings.item_id))" in sql.lower() for sql in session.execute_sql)
    assert any("item_relationships" in sql and "NOT (EXISTS" in sql for sql in session.execute_sql)


def test_metrics_exports_low_cardinality_operational_telemetry() -> None:
    client = _metrics_client(MetricsSession())

    response = client.get("/api/v1/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    body = response.text
    assert 'palace_http_requests_total{method="GET",route="/api/v1/health",status_code="200"} 1' in body
    assert 'palace_jobs{job_type="memory_artifact",status="queued"} 3' in body
    assert 'palace_memory_jobs{status="failed"} 1' in body
    assert 'palace_items{source_type="note",status="ready"} 7' in body
    assert 'palace_items{source_type="other",status="ready"} 3' in body
    assert "palace_indexed_items 6" in body
    assert "palace_embedding_chunks 42" in body
    assert "palace_dirty_backlog_items 5" in body
    assert "palace_dirty_backlog_generation 9" in body
    assert 'palace_webhook_jobs{status="failed"} 2' in body
    assert 'palace_arq_queue_depth{key="memory",queue="arq:queue"} 0' in body
    assert "tenant-a" not in body


def test_metrics_degrades_to_error_gauge_when_database_scrape_fails() -> None:
    client = _metrics_client(BrokenMetricsSession())

    response = client.get("/api/v1/metrics")

    assert response.status_code == 200
    body = response.text
    assert "palace_metrics_scrape 1" in body
    assert "palace_metrics_database_scrape_error 1" in body
