from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.system import router
from app.auth import verify_api_key
from app.database import get_db


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


def _client(session: StatsSession) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.arq_pool = SimpleNamespace(ping=_async_ping)

    async def override_get_db():
        yield session

    async def override_verify(request: Request):
        request.state.tenant_id = "tenant-a"
        request.state.key_hash = "key-hash"
        return "raw-key"

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[verify_api_key] = override_verify
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
