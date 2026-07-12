from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.system import router
from app.auth import AuthContext, verify_memory_auth
from app.database import get_db
from app.services.memory_telemetry import (
    record_embedding_request,
    record_retrieval,
    record_retention_extraction,
    record_scope_guard_violation,
    record_semantic_recall,
    reset_memory_telemetry_for_tests,
)
from app.services.prometheus_metrics import HttpMetricsRecorder
from app.services.relationship_telemetry import (
    record_relationship_extraction,
    reset_relationship_telemetry_for_tests,
)


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
        if "from job_attempts" in sql:
            return _MappingsResult(
                [
                    {"job_type": "memory_artifact", "status": "completed", "trigger": "initial", "failure_kind": None, "count": 4},
                    {"job_type": "memory_artifact", "status": "dead_lettered", "trigger": "manual_retry", "failure_kind": "non_retryable", "count": 1},
                    {"job_type": "private-job", "status": "failed", "trigger": "private-trigger", "failure_kind": "private-error", "count": 2},
                ]
            )
        if "from jobs" in sql and "where job_type = 'memory_artifact'" in sql:
            return _MappingsResult([{"status": "queued", "count": 3}, {"status": "failed", "count": 1}])
        if "from jobs" in sql and "where webhook_url is not null" in sql:
            return _MappingsResult([{"status": "failed", "count": 2}, {"status": "completed", "count": 5}])
        if "min(created_at)" in sql and "from jobs" in sql:
            return _MappingsResult(
                [
                    {"job_type": "memory_artifact", "status": "queued", "age_seconds": 91},
                    {"job_type": "private-a", "status": "queued", "age_seconds": 50},
                    {"job_type": "private-b", "status": "queued", "age_seconds": 80},
                ]
            )
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
        if "from source_resources" in sql:
            return _MappingsResult(
                [
                    {
                        "kind": "http",
                        "status": "active",
                        "count": 8,
                        "oldest_success_age_seconds": 7200,
                        "never_success_count": 2,
                        "due_count": 3,
                        "oldest_due_age_seconds": 600,
                    }
                ]
            )
        raise AssertionError(f"Unexpected SQL: {sql}")


class BrokenMetricsSession:
    async def execute(self, _statement):
        raise RuntimeError("database unavailable")


class NeverSuccessfulSourceMetricsSession(MetricsSession):
    async def execute(self, statement):
        sql = str(statement).lower()
        if "from source_resources" in sql:
            return _MappingsResult(
                [
                    {
                        "kind": "http",
                        "status": "active",
                        "count": 2,
                        "oldest_success_age_seconds": None,
                        "never_success_count": 2,
                        "due_count": 1,
                        "oldest_due_age_seconds": 30,
                    }
                ]
            )
        return await super().execute(statement)


class EmptyJobAttemptMetricsSession(MetricsSession):
    async def execute(self, statement):
        if "from job_attempts" in str(statement).lower():
            return _MappingsResult([])
        return await super().execute(statement)


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
    reset_memory_telemetry_for_tests()
    reset_relationship_telemetry_for_tests()
    record_semantic_recall(status="empty", scope_type="agent")
    record_retention_extraction(status="written", mode="extracted_write")
    record_scope_guard_violation(reason="agent_scope_not_allowlisted")
    record_embedding_request(
        status="retry",
        failure_kind="timeout",
        retryable=True,
        provider="openai",
        input_type="query",
        duration_seconds=0.2,
        batch_size=2,
        input_tokens=128,
    )
    record_retrieval(
        endpoint="retrieve_agent",
        outcome="success",
        intent="latest_status",
        route_confidence="high",
        fallback_used=True,
        empty=False,
        budget_truncated=True,
        stage_seconds={"scoped_search": 0.5, "total": 0.75},
        results=[SimpleNamespace(freshness="stale", trust_class="curated_memory", source_support_state="unknown")],
    )
    record_retrieval(
        endpoint="tenant-a/private/query",
        outcome="secret-outcome",
        intent="raw user query",
        route_confidence="customer-123",
        results=[SimpleNamespace(freshness="https://private.example", trust_class="item-123", source_support_state="query-fingerprint")],
    )
    record_relationship_extraction(
        provider="openrouter",
        retry_provider="openrouter",
        validation_outcome="malformed",
        fallback_used=True,
        retry_count=2,
        duration_seconds=0.25,
        edges_extracted=0,
    )
    client = _metrics_client(MetricsSession())

    response = client.get("/api/v1/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    body = response.text
    assert 'palace_http_requests_total{method="GET",route="/api/v1/health",status_code="200"} 1' in body
    assert 'palace_jobs{job_type="memory_artifact",status="queued"} 3' in body
    assert (
        'palace_embedding_requests_total{failure_kind="timeout",retryable="true",status="retry"} 1'
        in body
    )
    assert 'palace_memory_jobs{status="failed"} 1' in body
    assert 'palace_jobs_oldest_age_seconds{job_type="memory_artifact",status="queued"} 91' in body
    assert body.count('palace_jobs_oldest_age_seconds{job_type="other",status="queued"}') == 1
    assert 'palace_jobs_oldest_age_seconds{job_type="other",status="queued"} 80' in body
    assert 'palace_items{source_type="note",status="ready"} 7' in body
    assert 'palace_items{source_type="other",status="ready"} 3' in body
    assert "palace_indexed_items 6" in body
    assert "palace_embedding_chunks 42" in body
    assert "palace_dirty_backlog_items 5" in body
    assert "palace_dirty_backlog_generation 9" in body
    assert 'palace_webhook_jobs{status="failed"} 2' in body
    assert 'palace_semantic_recall_total{scope_type="agent",status="empty"} 1' in body
    assert 'palace_retention_extraction_total{mode="extracted_write",status="written"} 1' in body
    assert 'palace_memory_scope_guard_violations_total{reason="agent_scope_not_allowlisted"} 1' in body
    assert (
        'palace_relationship_extractions_total{fallback_used="true",provider="openrouter",validation_outcome="malformed"} 1'
        in body
    )
    assert 'palace_relationship_extraction_retries_total{provider="openrouter"} 2' in body
    assert (
        'palace_relationship_extraction_duration_seconds_count{provider="openrouter",validation_outcome="malformed"} 1'
        in body
    )
    assert 'palace_relationship_edges_extracted_total{provider="openrouter"} 0' in body
    assert (
        'palace_retrieval_requests_total{endpoint="retrieve_agent",outcome="success"} 1'
        in body
    )
    assert (
        'palace_retrieval_classifications_total{dimension="intent",endpoint="retrieve_agent",value="latest_status"} 1'
        in body
    )
    assert (
        'palace_retrieval_classifications_total{dimension="budget_truncated",endpoint="retrieve_agent",value="true"} 1'
        in body
    )
    assert 'palace_retrieval_stage_duration_seconds_bucket{endpoint="retrieve_agent",le="0.5",stage="scoped_search"} 1' in body
    assert 'palace_retrieval_results_total{endpoint="retrieve_agent",freshness="stale",rank_band="1",source_support_state="unknown",trust_class="curated_memory"} 1' in body
    assert 'palace_embedding_duration_seconds_bucket{failure_kind="timeout",input_type="query",le="0.25",provider="openai",status="retry"} 1' in body
    assert 'palace_source_refresh_due{kind="http",status="active"} 3' in body
    assert 'palace_source_never_succeeded{kind="http",status="active"} 2' in body
    assert 'palace_arq_queue_depth{key="memory",queue="arq:queue"} 0' in body
    assert 'palace_arq_worker_available{key="memory",queue="arq:queue"} 0' in body
    assert 'palace_arq_worker_heartbeat_age_seconds{key="memory",queue="arq:queue"}' not in body
    assert "tenant-a" not in body
    assert "query_fingerprint" not in body
    assert "item_id" not in body
    assert "private.example" not in body
    assert "customer-123" not in body
    assert "raw user query" not in body


def test_metrics_degrades_to_error_gauge_when_database_scrape_fails() -> None:
    client = _metrics_client(BrokenMetricsSession())

    response = client.get("/api/v1/metrics")

    assert response.status_code == 200
    body = response.text
    assert "palace_metrics_scrape 1" in body
    assert "palace_metrics_database_scrape_error 1" in body


def test_metrics_do_not_treat_source_creation_as_a_successful_refresh() -> None:
    response = _metrics_client(NeverSuccessfulSourceMetricsSession()).get("/api/v1/metrics")

    assert response.status_code == 200
    body = response.text
    assert 'palace_source_never_succeeded{kind="http",status="active"} 2' in body
    assert 'palace_source_last_success_age_seconds{kind="http",status="active"}' not in body


def test_metrics_emit_bounded_zero_job_lineage_series_when_attempts_are_empty() -> None:
    response = _metrics_client(EmptyJobAttemptMetricsSession()).get("/api/v1/metrics")

    assert response.status_code == 200
    body = response.text
    assert 'palace_job_attempts{job_type="other",status="other",trigger="other"} 0' in body
    assert 'palace_job_recoveries{job_type="other",outcome="other"} 0' in body
    assert 'palace_job_dead_letters{failure_kind="other",job_type="other"} 0' in body
