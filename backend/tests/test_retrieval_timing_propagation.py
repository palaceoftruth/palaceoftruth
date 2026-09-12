from types import SimpleNamespace

import pytest

from app.api.memory import _record_retrieval_metrics
from app.schemas.memory import MemoryRetrieveRequest
from app.schemas.palace import PalaceRetrieveResponse, PalaceRetrieveTrace, PalaceSearchAttemptTiming
from app.schemas.search import SearchResult
from app.services import memory as memory_service


@pytest.mark.asyncio
async def test_retrieve_memory_preserves_palace_timing_trace(monkeypatch) -> None:
    attempt = PalaceSearchAttemptTiming(reason="room_scoped", status="success", duration_ms=12.5)
    trace = PalaceRetrieveTrace(
        stage_timings_ms={"routing": 2.0, "room_loading": 3.0, "result_handling": 1.0, "search": 9.0},
        search_attempts=[attempt],
    )

    async def fake_retrieve_palace(*args, **kwargs):
        return PalaceRetrieveResponse(trace=trace, results=[], total=0)

    monkeypatch.setattr(memory_service, "retrieve_palace", fake_retrieve_palace)
    response = await memory_service.retrieve_memory(
        SimpleNamespace(),
        embedder=SimpleNamespace(),
        tenant_id="tenant-private-id",
        body=MemoryRetrieveRequest(query="caller query", scope={"type": "workspace", "key": "project-private-id"}),
    )

    assert response.trace.stage_timings_ms["room_loading"] == 3.0
    assert response.trace.stage_timings_ms["result_handling"] == 1.0
    assert response.trace.search_attempts[0].duration_ms == 12.5


def test_retrieval_metrics_use_fixed_stage_labels_only(monkeypatch) -> None:
    captured = {}

    def fake_record_retrieval(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("app.api.memory.record_retrieval", fake_record_retrieval)
    trace = SimpleNamespace(
        stage_timings_ms={"routing": 4.0, "caller query": 999.0},
        search_attempts=[SimpleNamespace(sql_timings_ms={"hybrid_query": 3.0, "private-id": 8.0}), SimpleNamespace(sql_timings_ms={"hybrid_query": 4.0})],
        search_ranking_trace={},
        fallback_used=False,
        route_confidence="none",
        route_abstain_reason=None,
        budget_truncated=False,
        context_budget_truncated=False,
        embedding_unavailable=False,
    )
    _record_retrieval_metrics(
        endpoint="retrieve_agent",
        outcome="success",
        latency_ms=10.0,
        response=SimpleNamespace(trace=trace, results=[]),
    )

    stages = captured["stage_seconds"]
    assert set(stages) == {"total", "routing", "hybrid_query"}
    assert "caller query" not in stages
    assert "private-id" not in stages
    assert stages["hybrid_query"] == 0.007
    assert stages["total"] == 0.01



@pytest.mark.asyncio
async def test_agent_lookup_keeps_inner_stages_and_measures_outer_embedding(monkeypatch):
    from app.schemas.memory import AgentMemoryRetrieveRequest, MemoryRetrieveResponse

    clock = [0.0]
    monkeypatch.setattr(memory_service, "perf_counter", lambda: clock[0])
    calls = []

    class Embedder:
        async def embed_single(self, query):
            calls.append(query)
            clock[0] += 0.020
            return [0.1] * 1536

    async def scoped_lookup(db, *, body, **kwargs):
        clock[0] += 0.050
        return MemoryRetrieveResponse(
            scope=body.scope,
            trace=PalaceRetrieveTrace(
                stage_timings_ms={
                    "room_loading": 3.0, "routing": 5.0, "embedding": 0.5,
                    "result_handling": 1.0, "total": 50.0, "untrusted-label": 10.0,
                },
                search_attempts=[PalaceSearchAttemptTiming(
                    reason="room_scoped", status="empty", duration_ms=40.0,
                    sql_timings_ms={"hybrid_query": 30.0}, candidate_strategy="selective_room",
                )],
            ), results=[], total=0,
        )

    monkeypatch.setattr(memory_service, "retrieve_memory", scoped_lookup)
    result = await memory_service.retrieve_agent_memory(
        SimpleNamespace(), tenant_id="synthetic-tenant", embedder=Embedder(),
        body=AgentMemoryRetrieveRequest(
            query="synthetic query", agent_scope_key="fixture-agent",
            workspace_scope_keys=["fixture-project"], workspace_strict=True,
            tenant_shared_policy="never", include_broad_corpus=False,
        ),
    )
    assert calls == ["synthetic query"]
    assert result.trace.stage_timings_ms["embedding"] == 20.5
    assert result.trace.stage_timings_ms["room_loading"] == 3.0
    assert result.trace.stage_timings_ms["result_handling"] == 1.0
    assert result.trace.stage_timings_ms["total"] == result.trace.total_duration_ms == 70
    assert "untrusted-label" not in result.trace.stage_timings_ms
    assert result.trace.search_attempts[0].sql_timings_ms == {"hybrid_query": 30.0}
