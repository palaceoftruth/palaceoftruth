"""Candidate SQL failures must retain useful timing without logging bindings."""

import logging

import pytest

from app.services.search import SearchService


class _Embedder:
    async def embed_single(self, _query):
        return [0.1] * 1536


class _FailingDatabase:
    async def execute(self, _statement, _params):
        raise RuntimeError("private-fixture-query binding: synthetic-sensitive-value")


@pytest.mark.asyncio
async def test_candidate_sql_error_has_sanitized_timing(caplog):
    service = SearchService(_FailingDatabase(), _Embedder(), tenant_id="synthetic-tenant")
    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError):
        await service.vector_search("private-fixture-query")
    assert service.last_sql_timings_ms["hybrid_query"] >= 0
    failure = next(record for record in caplog.records if record.msg == "retrieval_sql_failed")
    assert failure.stage == "hybrid_query"
    assert failure.status == "error"
    assert failure.duration_ms >= 0
    assert failure.exc_info is None
    assert "private-fixture-query" not in caplog.text
    assert "synthetic-sensitive-value" not in caplog.text
    assert "synthetic-tenant" not in caplog.text
