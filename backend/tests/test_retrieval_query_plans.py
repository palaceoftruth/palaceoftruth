"""Real PostgreSQL gates for the production SearchService SQL and currentness."""

from __future__ import annotations

import json
import os
from typing import Any, Iterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.embedding_profile import EMBEDDING_DIMENSIONS, resolve_embedding_profile
from app.services.search import SearchService


DATABASE_URL = os.environ.get("PLAN_GATE_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="PLAN_GATE_DATABASE_URL is required outside the dedicated CI plan-gate step",
)


class _LocalEmbedder:
    profile = resolve_embedding_profile(
        provider="local-http",
        model="Alibaba-NLP/gte-modernbert-base",
        dimensions=768,
        profile_name="local-http-gte-modernbert-base",
    )

    async def embed_single(self, _query: str) -> list[float]:
        return [0.1] * self.profile.dimensions


class _DefaultEmbedder:
    profile = resolve_embedding_profile()

    async def embed_single(self, _query: str) -> list[float]:
        return [0.1] * self.profile.dimensions


class _EmptyResult:
    def fetchall(self) -> list[Any]:
        return []


class _ExplainSession:
    """Execute SearchService's exact statement as JSON EXPLAIN."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.plan: Any = None
        self.statement_text: str | None = None
        self.params: dict[str, Any] | None = None

    async def execute(self, statement: Any, params: dict[str, Any]) -> _EmptyResult:
        self.statement_text = statement.text
        self.params = params
        result = await self.session.execute(
            text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON, COSTS OFF, TIMING OFF) {statement.text}"),
            params,
        )
        self.plan = result.scalar_one()
        return _EmptyResult()


def _walk_plan_nodes(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, list):
        for item in value:
            yield from _walk_plan_nodes(item)
    elif isinstance(value, dict):
        if "Node Type" in value:
            yield value
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from _walk_plan_nodes(child)


@pytest_asyncio.fixture
async def plan_session() -> AsyncSession:
    assert DATABASE_URL is not None
    async_url = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(async_url)
    async with engine.connect() as connection:
        session = AsyncSession(bind=connection, expire_on_commit=False)
        vector = "[" + ",".join(["0.1"] * 768) + "]"
        default_vector = "[" + ",".join(["0.1"] * EMBEDDING_DIMENSIONS) + "]"
        try:
            await session.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await session.execute(text("""
                CREATE TEMP TABLE items (
                    id uuid PRIMARY KEY, tenant_id text NOT NULL, status text NOT NULL,
                    deleted_at timestamptz, source_type varchar, metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
                    tags text[] NOT NULL DEFAULT '{}', effective_date timestamptz,
                    created_at timestamptz NOT NULL DEFAULT now(), search_vector tsvector NOT NULL,
                    title text NOT NULL, summary text, source_url text,
                    effective_date_source text, effective_date_quality text
                )
            """))
            await session.execute(text("""
                CREATE TEMP TABLE embedding_profile_vectors (
                    item_id uuid NOT NULL, profile_name text NOT NULL, dimensions integer NOT NULL,
                    chunk_text text NOT NULL, chunk_index integer NOT NULL,
                    embedding_half_768 halfvec(768) NOT NULL
                )
            """))
            await session.execute(text(f"""
                CREATE TEMP TABLE embeddings (
                    item_id uuid NOT NULL, chunk_text text NOT NULL,
                    chunk_index integer NOT NULL,
                    embedding_half halfvec({EMBEDDING_DIMENSIONS}) NOT NULL
                )
            """))
            await session.execute(text("CREATE TEMP TABLE room_memberships (tenant_id text, item_id uuid, room_id uuid)"))
            await session.execute(text("""
                CREATE TEMP TABLE memory_entries (
                    tenant_id text NOT NULL,
                    item_id uuid NOT NULL,
                    valid_until timestamptz,
                    superseded_by_entry_id uuid
                )
            """))
            await session.execute(text("""
                CREATE UNIQUE INDEX sar1063_memory_entries_tenant_item
                ON memory_entries (tenant_id, item_id)
            """))
            await session.execute(text("""
                CREATE TEMP TABLE source_records (
                    id uuid PRIMARY KEY,
                    tenant_id text NOT NULL,
                    item_id uuid NOT NULL,
                    status text NOT NULL
                )
            """))
            await session.execute(text("""
                CREATE INDEX sar1063_source_records_tenant_item_status
                ON source_records (tenant_id, item_id, status)
            """))
            await session.execute(text("CREATE INDEX sar1060_items_fts ON items USING gin (search_vector)"))
            await session.execute(text("""
                CREATE INDEX sar1060_profile_item_chunk
                ON embedding_profile_vectors (item_id, chunk_index)
            """))
            await session.execute(text("""
                CREATE INDEX sar1060_default_item_chunk
                ON embeddings (item_id, chunk_index)
            """))
            await session.execute(text("""
                CREATE INDEX sar1060_profile_hnsw
                ON embedding_profile_vectors USING hnsw (embedding_half_768 halfvec_cosine_ops)
                WHERE profile_name = 'local-http-gte-modernbert-base' AND dimensions = 768
            """))
            await session.execute(text("""
                CREATE INDEX sar1060_default_hnsw
                ON embeddings USING hnsw (embedding_half halfvec_cosine_ops)
            """))
            await session.execute(
                text("""
                    INSERT INTO items (id, tenant_id, status, source_type, search_vector, title)
                    SELECT md5(value::text)::uuid, 'tenant-a', 'ready', 'doc',
                           to_tsvector('english', CASE WHEN value % 1000 = 0 THEN 'current palace retrieval' ELSE 'evergreen documentation ' || value::text END),
                           'Fixture ' || value::text
                    FROM generate_series(1, 5000) AS value
                """),
            )
            await session.execute(
                text("""
                    INSERT INTO embedding_profile_vectors
                        (item_id, profile_name, dimensions, chunk_text, chunk_index, embedding_half_768)
                    SELECT id, 'local-http-gte-modernbert-base', 768, title, 0, CAST(:vector AS halfvec(768))
                    FROM items
                """),
                {"vector": vector},
            )
            await session.execute(
                text(f"""
                    INSERT INTO embeddings (item_id, chunk_text, chunk_index, embedding_half)
                    SELECT id, title || ' chunk ' || chunk_index::text, chunk_index,
                           CAST(:vector AS halfvec({EMBEDDING_DIMENSIONS}))
                    FROM items
                    CROSS JOIN generate_series(0, 2) AS chunk_index
                """),
                {"vector": default_vector},
            )
            await session.execute(text("ANALYZE items"))
            await session.execute(text("ANALYZE embedding_profile_vectors"))
            await session.execute(text("ANALYZE embeddings"))
            yield session
        finally:
            await session.close()
    await engine.dispose()


@pytest.mark.asyncio
async def test_production_bounded_hybrid_query_uses_indexed_embedding_lookups(
    plan_session: AsyncSession,
) -> None:
    explain_session = _ExplainSession(plan_session)
    service = SearchService(explain_session, _LocalEmbedder(), tenant_id="tenant-a")

    await service.vector_search("current palace retrieval", candidate_limit=40)

    plan = json.loads(explain_session.plan) if isinstance(explain_session.plan, str) else explain_session.plan
    index_names = {node.get("Index Name") for node in _walk_plan_nodes(plan)}
    assert "sar1060_profile_hnsw" in index_names
    assert "sar1060_profile_item_chunk" in index_names
    repeated_embedding_scans = [
        node
        for node in _walk_plan_nodes(plan)
        if node.get("Node Type") == "Seq Scan"
        and node.get("Relation Name") == "embedding_profile_vectors"
    ]
    assert repeated_embedding_scans == []
    assert explain_session.statement_text is not None
    assert "LIMIT :semantic_candidate_limit" in explain_session.statement_text
    assert "LIMIT :lexical_candidate_limit" in explain_session.statement_text
    assert explain_session.params is not None
    assert explain_session.params["semantic_candidate_limit"] == 40
    assert explain_session.params["lexical_candidate_limit"] == 40
    # Semantic, lexical, per-item lexical chunk, and final output bounds must all
    # survive planning. This prevents the final display LIMIT from masking a
    # removed candidate-lane bound.
    assert sum(node.get("Node Type") == "Limit" for node in _walk_plan_nodes(plan)) >= 4


@pytest.mark.asyncio
async def test_default_embedding_query_uses_item_chunk_index_without_repeated_full_scans(
    plan_session: AsyncSession,
) -> None:
    explain_session = _ExplainSession(plan_session)
    service = SearchService(explain_session, _DefaultEmbedder(), tenant_id="tenant-a")

    await service.vector_search("current palace retrieval", candidate_limit=40)

    plan = json.loads(explain_session.plan) if isinstance(explain_session.plan, str) else explain_session.plan
    index_names = {node.get("Index Name") for node in _walk_plan_nodes(plan)}
    assert "sar1060_default_hnsw" in index_names
    assert "sar1060_default_item_chunk" in index_names
    assert [
        node
        for node in _walk_plan_nodes(plan)
        if node.get("Node Type") == "Seq Scan" and node.get("Relation Name") == "embeddings"
    ] == []


@pytest.mark.asyncio
async def test_lexical_candidate_predicate_is_gin_eligible(plan_session: AsyncSession) -> None:
    await plan_session.execute(text("SET LOCAL enable_seqscan = off"))
    result = await plan_session.execute(text("""
        EXPLAIN (FORMAT JSON, COSTS OFF)
        SELECT id
        FROM items
        WHERE search_vector @@ plainto_tsquery('english', 'current palace retrieval')
    """))
    plan = result.scalar_one()
    index_names = {node.get("Index Name") for node in _walk_plan_nodes(plan)}
    assert "sar1060_items_fts" in index_names


@pytest.mark.asyncio
async def test_production_query_preserves_strict_scope_filter(plan_session: AsyncSession) -> None:
    explain_session = _ExplainSession(plan_session)
    service = SearchService(explain_session, _LocalEmbedder(), tenant_id="tenant-a")

    await service.vector_search(
        "current palace retrieval",
        scope_type="agent",
        scope_key="codex",
        candidate_limit=40,
    )

    plan_text = json.dumps(explain_session.plan, sort_keys=True)
    assert "memory_entry" in plan_text
    assert "codex" in plan_text
