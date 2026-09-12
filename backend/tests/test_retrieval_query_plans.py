"""Real PostgreSQL gates for the production SearchService SQL and currentness."""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Iterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.embedding_profile import EMBEDDING_DIMENSIONS, resolve_embedding_profile
from app.services.llm_admission import TenantLlmBudgetExceeded, consume_tenant_token_budget
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
        # The selective-room strategy performs a small preflight. Forward that
        # statement normally; only the main retrieval SQL is wrapped in EXPLAIN.
        if "selective_room_probe" in statement.text:
            return await self.session.execute(statement, params)
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
                    effective_date_source text, effective_date_quality text,
                    governance_owner_subject text, governance_reviewer_subject text,
                    governance_verification_state varchar(20), governance_verified_at timestamptz,
                    governance_verified_by_subject text,
                    governance_verification_deadline timestamptz,
                    governance_risk_class varchar(20), governance_supersession_reason text,
                    governance_superseded_by_item_id uuid, governance_superseded_at timestamptz
                )
            """))
            await session.execute(text("""
                CREATE TEMP TABLE embedding_profile_vectors (
                    tenant_id text NOT NULL, item_id uuid NOT NULL, profile_name text NOT NULL, dimensions integer NOT NULL,
                    chunk_text text NOT NULL, chunk_index integer NOT NULL,
                    embedding_half_768 halfvec(768) NOT NULL
                )
            """))
            await session.execute(text(f"""
                CREATE TEMP TABLE embeddings (
                    tenant_id text NOT NULL, item_id uuid NOT NULL, chunk_text text NOT NULL,
                    chunk_index integer NOT NULL,
                    embedding_half halfvec({EMBEDDING_DIMENSIONS}) NOT NULL
                )
            """))
            await session.execute(text("CREATE TEMP TABLE room_memberships (tenant_id text, item_id uuid, room_id uuid)"))
            await session.execute(text("CREATE INDEX sar1060_room_membership_lookup ON room_memberships (tenant_id, room_id, item_id)"))
            await session.execute(text("""
                CREATE TEMP TABLE memory_entries (
                    id uuid PRIMARY KEY,
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
                CREATE TEMP TABLE item_relationships (
                    source_item_id uuid NOT NULL,
                    target_item_id uuid NOT NULL,
                    confidence double precision NOT NULL,
                    relationship text NOT NULL
                )
            """))
            await session.execute(text("""
                CREATE INDEX sar1069_relationship_source_lookup
                ON item_relationships (source_item_id, confidence DESC, target_item_id, relationship)
            """))
            await session.execute(text("""
                CREATE INDEX sar1069_relationship_target_lookup
                ON item_relationships (target_item_id, confidence DESC, source_item_id, relationship)
            """))
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
                        (tenant_id, item_id, profile_name, dimensions, chunk_text, chunk_index, embedding_half_768)
                    SELECT tenant_id, id, 'local-http-gte-modernbert-base', 768, title, 0, CAST(:vector AS halfvec(768))
                    FROM items
                """),
                {"vector": vector},
            )
            await session.execute(
                text(f"""
                    INSERT INTO embeddings (tenant_id, item_id, chunk_text, chunk_index, embedding_half)
                    SELECT tenant_id, id, title || ' chunk ' || chunk_index::text, chunk_index,
                           CAST(:vector AS halfvec({EMBEDDING_DIMENSIONS}))
                    FROM items
                    CROSS JOIN generate_series(0, 2) AS chunk_index
                """),
                {"vector": default_vector},
            )
            await session.execute(text("""
                INSERT INTO room_memberships (tenant_id, item_id, room_id)
                SELECT 'tenant-a', id, md5('selective-room')::uuid
                FROM items
                WHERE id IN (SELECT md5(value::text)::uuid FROM generate_series(1, 100) AS value)
            """))
            await session.execute(text("ANALYZE items"))
            await session.execute(text("ANALYZE embedding_profile_vectors"))
            await session.execute(text("ANALYZE embeddings"))
            await session.execute(text("ANALYZE room_memberships"))
            await session.execute(text("""
                INSERT INTO item_relationships (source_item_id, target_item_id, confidence, relationship)
                SELECT md5(seed::text)::uuid, md5(target::text)::uuid,
                       0.5 + (target % 50)::float / 100, 'related'
                FROM generate_series(1, 100) AS seed
                CROSS JOIN generate_series(101, 300) AS target
            """))
            await session.execute(text("ANALYZE item_relationships"))
            yield session
        finally:
            await session.close()
    await engine.dispose()


@pytest_asyncio.fixture
async def rls_session() -> AsyncSession:
    """Run a mixed retrieval fixture through a forced-RLS, non-bypass role."""
    assert DATABASE_URL is not None
    async_url = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(async_url)
    suffix = uuid.uuid4().hex[:10]
    role = f"palace_gate_reader_{suffix}"
    schema = f"gate_rls_{suffix}"
    async with engine.connect() as connection:
        await connection.execute(text(
            f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"
        ))
        session = AsyncSession(bind=connection, expire_on_commit=False)
        session.info["role_name"] = role
        try:
            await session.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await session.execute(text(f"CREATE SCHEMA {schema}"))
            for ddl in (
                f"""CREATE TABLE {schema}.items (
                    id uuid PRIMARY KEY, tenant_id text NOT NULL, status text NOT NULL,
                    deleted_at timestamptz, source_type varchar, metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb,
                    tags text[] NOT NULL DEFAULT '{{}}', effective_date timestamptz,
                    created_at timestamptz NOT NULL DEFAULT now(), search_vector tsvector NOT NULL,
                    title text NOT NULL, summary text, source_url text,
                    effective_date_source text, effective_date_quality text,
                    governance_owner_subject text, governance_reviewer_subject text,
                    governance_verification_state varchar(20), governance_verified_at timestamptz,
                    governance_verified_by_subject text, governance_verification_deadline timestamptz,
                    governance_risk_class varchar(20), governance_supersession_reason text,
                    governance_superseded_by_item_id uuid, governance_superseded_at timestamptz
                )""",
                f"""CREATE TABLE {schema}.embeddings (
                    tenant_id text NOT NULL, item_id uuid NOT NULL, chunk_text text NOT NULL,
                    chunk_index integer NOT NULL, embedding_half halfvec({EMBEDDING_DIMENSIONS}) NOT NULL
                )""",
                f"""CREATE TABLE {schema}.embedding_profile_vectors (
                    tenant_id text NOT NULL, item_id uuid NOT NULL, profile_name text NOT NULL,
                    dimensions integer NOT NULL, chunk_text text NOT NULL, chunk_index integer NOT NULL,
                    embedding_half_768 halfvec(768) NOT NULL
                )""",
                f"CREATE TABLE {schema}.room_memberships (tenant_id text NOT NULL, item_id uuid NOT NULL, room_id uuid NOT NULL)",
                f"CREATE TABLE {schema}.memory_entries (id uuid PRIMARY KEY, tenant_id text NOT NULL, item_id uuid NOT NULL, valid_until timestamptz, superseded_by_entry_id uuid)",
                f"CREATE TABLE {schema}.source_records (id uuid PRIMARY KEY, tenant_id text NOT NULL, item_id uuid NOT NULL, status text NOT NULL)",
                f"CREATE INDEX gate_rls_room_lookup ON {schema}.room_memberships (tenant_id, room_id, item_id)",
                f"CREATE INDEX gate_rls_item_lookup ON {schema}.items (tenant_id, id)",
                f"CREATE INDEX gate_rls_embedding_lookup ON {schema}.embeddings (tenant_id, item_id, chunk_index)",
                f"CREATE INDEX gate_rls_profile_lookup ON {schema}.embedding_profile_vectors (tenant_id, item_id, chunk_index)",
            ):
                await session.execute(text(ddl))
            vector = "[" + ",".join(["0.1"] * EMBEDDING_DIMENSIONS) + "]"
            profile_vector = "[" + ",".join(["0.1"] * 768) + "]"
            for statement in f"""
                INSERT INTO {schema}.items (id, tenant_id, status, deleted_at, source_type, metadata, tags, effective_date, search_vector, title, summary)
                VALUES
                (md5('allowed')::uuid, 'tenant-a', 'ready', NULL, 'note', '{{"memory_entry":{{"scope":{{"type":"tenant_shared"}}}}}}', '{{allowed}}', now(), to_tsvector('english','allowed deployment owner'), 'Allowed deployment owner', 'current allowed item'),
                (md5('deleted')::uuid, 'tenant-a', 'deleted', now(), 'note', '{{"memory_entry":{{"scope":{{"type":"tenant_shared"}}}}}}', '{{allowed}}', now(), to_tsvector('english','deleted deployment owner'), 'Deleted deployment owner', 'deleted'),
                (md5('expired')::uuid, 'tenant-a', 'ready', NULL, 'note', '{{"memory_entry":{{"scope":{{"type":"tenant_shared"}}}}}}', '{{allowed}}', now(), to_tsvector('english','expired deployment owner'), 'Expired deployment owner', 'expired'),
                (md5('superseded')::uuid, 'tenant-a', 'ready', NULL, 'note', '{{"memory_entry":{{"scope":{{"type":"tenant_shared"}}}}}}', '{{allowed}}', now(), to_tsvector('english','superseded deployment owner'), 'Superseded deployment owner', 'superseded'),
                (md5('private')::uuid, 'tenant-a', 'ready', NULL, 'note', '{{"memory_entry":{{"scope":{{"type":"agent","key":"private-agent"}}}}}}', '{{private}}', now(), to_tsvector('english','private deployment owner'), 'Private deployment owner', 'private'),
                (md5('stale-source')::uuid, 'tenant-a', 'ready', NULL, 'note', '{{"memory_entry":{{"scope":{{"type":"tenant_shared"}}}}}}', '{{allowed}}', now(), to_tsvector('english','stale deployment owner'), 'Stale deployment owner', 'stale'),
                (md5('old-date')::uuid, 'tenant-a', 'ready', NULL, 'note', '{{"memory_entry":{{"scope":{{"type":"tenant_shared"}}}}}}', '{{allowed}}', now()-interval '90 days', to_tsvector('english','old deployment owner'), 'Old deployment owner', 'old'),
                (md5('wrong-tag')::uuid, 'tenant-a', 'ready', NULL, 'note', '{{"memory_entry":{{"scope":{{"type":"tenant_shared"}}}}}}', '{{different}}', now(), to_tsvector('english','other deployment owner'), 'Other deployment owner', 'other tag'),
                (md5('other-tenant')::uuid, 'tenant-b', 'ready', NULL, 'note', '{{"memory_entry":{{"scope":{{"type":"tenant_shared"}}}}}}', '{{allowed}}', now(), to_tsvector('english','other tenant deployment owner'), 'Other tenant owner', 'other tenant');
                INSERT INTO {schema}.embeddings (tenant_id,item_id,chunk_text,chunk_index,embedding_half)
                SELECT tenant_id,id,title,0,CAST(:vector AS halfvec({EMBEDDING_DIMENSIONS})) FROM {schema}.items;
                INSERT INTO {schema}.embedding_profile_vectors (tenant_id,item_id,profile_name,dimensions,chunk_text,chunk_index,embedding_half_768)
                SELECT tenant_id,id,'local-http-gte-modernbert-base',768,title,0,CAST(:profile_vector AS halfvec(768)) FROM {schema}.items;
                INSERT INTO {schema}.room_memberships (tenant_id,item_id,room_id)
                SELECT tenant_id,id,md5('rls-room')::uuid FROM {schema}.items;
                INSERT INTO {schema}.memory_entries (id,tenant_id,item_id,valid_until,superseded_by_entry_id)
                VALUES (md5('expired-entry')::uuid,'tenant-a',md5('expired')::uuid,now()-interval '1 day',NULL),
                       (md5('superseded-entry')::uuid,'tenant-a',md5('superseded')::uuid,NULL,md5('allowed')::uuid);
                INSERT INTO {schema}.source_records (id,tenant_id,item_id,status)
                SELECT md5('source-'||id::text)::uuid,tenant_id,id,CASE WHEN id=md5('stale-source')::uuid THEN 'stale' ELSE 'active' END FROM {schema}.items;
            """.split(";"):
                if statement.strip():
                    await session.execute(
                        text(statement), {"vector": vector, "profile_vector": profile_vector}
                    )
            for table in ("items", "embeddings", "embedding_profile_vectors", "room_memberships", "memory_entries", "source_records"):
                for statement in (
                    f"ALTER TABLE {schema}.{table} ENABLE ROW LEVEL SECURITY",
                    f"ALTER TABLE {schema}.{table} FORCE ROW LEVEL SECURITY",
                    f"CREATE POLICY tenant_gate ON {schema}.{table} USING (tenant_id = current_setting('app.tenant_id', true))",
                    f"GRANT SELECT ON {schema}.{table} TO {role}",
                ):
                    await session.execute(text(statement))
            await session.execute(text(f"GRANT USAGE ON SCHEMA {schema} TO {role}"))
            await session.execute(text("SET LOCAL app.tenant_id = 'tenant-a'"))
            await session.execute(text(f"SET LOCAL ROLE {role}"))
            await session.execute(text(f"SET LOCAL search_path = {schema}, public"))
            yield session
        finally:
            await session.rollback()
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
@pytest.mark.parametrize(
    ("embedder", "membership_index"),
    [
        (_LocalEmbedder(), "sar1060_profile_item_chunk"),
        (_DefaultEmbedder(), "sar1060_default_item_chunk"),
    ],
)
async def test_selective_room_query_bounds_embedding_lanes(
    plan_session: AsyncSession, embedder: object, membership_index: str
) -> None:
    """Room membership eligibility must drive both bounded candidate lanes."""
    from unittest.mock import patch

    explain_session = _ExplainSession(plan_session)
    service = SearchService(explain_session, embedder, tenant_id="tenant-a")
    room_id = "md5('selective-room')::uuid"
    with patch("app.services.search.settings.retrieval_selective_room_search_enabled", True):
        await service.vector_search(
            "current palace retrieval",
            room_ids=[await plan_session.scalar(text(f"SELECT {room_id}"))],
            candidate_limit=40,
        )

    assert explain_session.statement_text is not None
    assert "eligible_room_items" in explain_session.statement_text
    assert "/* selective_room_probe */" not in explain_session.statement_text
    assert membership_index in {node.get("Index Name") for node in _walk_plan_nodes(explain_session.plan)}
    assert all(
        not (
            node.get("Node Type") == "Seq Scan"
            and node.get("Relation Name") in {"embeddings", "embedding_profile_vectors"}
        )
        for node in _walk_plan_nodes(explain_session.plan)
    )


@pytest.mark.asyncio
async def test_broad_room_probe_retains_standard_query_path(plan_session: AsyncSession) -> None:
    from unittest.mock import patch

    await plan_session.execute(text("""
        INSERT INTO room_memberships (tenant_id, item_id, room_id)
        SELECT 'tenant-a', id, md5('broad-room')::uuid
        FROM items
        WHERE id IN (SELECT md5(value::text)::uuid FROM generate_series(1, 1400) AS value)
    """))
    explain_session = _ExplainSession(plan_session)
    service = SearchService(explain_session, _DefaultEmbedder(), tenant_id="tenant-a")
    room_id = await plan_session.scalar(text("SELECT md5('broad-room')::uuid"))
    with patch("app.services.search.settings.retrieval_selective_room_search_enabled", True):
        await service.vector_search("current palace retrieval", room_ids=[room_id], candidate_limit=40)

    assert explain_session.statement_text is not None
    assert "eligible_room_items" not in explain_session.statement_text
    assert "room_memberships" in explain_session.statement_text


@pytest.mark.asyncio
@pytest.mark.parametrize("embedder", [_LocalEmbedder(), _DefaultEmbedder()])
@pytest.mark.parametrize("selective_enabled", [True, False])
async def test_forced_rls_preserves_scope_and_currentness(
    rls_session: AsyncSession, embedder: object, selective_enabled: bool
) -> None:
    from unittest.mock import patch
    from datetime import datetime, timedelta, timezone

    service = SearchService(rls_session, embedder, tenant_id="tenant-a")
    assert await rls_session.scalar(text("SELECT current_user")) == rls_session.info["role_name"]
    assert await rls_session.scalar(text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")) is False
    assert await rls_session.scalar(text("SELECT relforcerowsecurity FROM pg_class WHERE oid = 'items'::regclass")) is True
    room_id = await rls_session.scalar(text("SELECT md5('rls-room')::uuid"))
    with patch("app.services.search.settings.retrieval_selective_room_search_enabled", selective_enabled):
        results = await service.vector_search(
            "allowed deployment owner",
            room_ids=[room_id],
            scope_type="tenant_shared",
            candidate_limit=20, tags=["allowed"], tags_mode="all", source_type="note",
            date_from=datetime.now(timezone.utc)-timedelta(days=1),
            date_to=datetime.now(timezone.utc)+timedelta(days=1),
        )

    result_ids = {str(result.item_id) for result in results}
    assert str(await rls_session.scalar(text("SELECT md5('allowed')::uuid"))) in result_ids
    assert str(await rls_session.scalar(text("SELECT md5('private')::uuid"))) not in result_ids
    assert str(await rls_session.scalar(text("SELECT md5('other-tenant')::uuid"))) not in result_ids
    assert str(await rls_session.scalar(text("SELECT md5('deleted')::uuid"))) not in result_ids
    assert str(await rls_session.scalar(text("SELECT md5('expired')::uuid"))) not in result_ids
    assert str(await rls_session.scalar(text("SELECT md5('superseded')::uuid"))) not in result_ids

    assert len(result_ids) == 1
    with patch("app.services.search.settings.retrieval_selective_room_search_enabled", selective_enabled):
        historical = await service.vector_search(
            "deployment owner as of 2024", room_ids=[room_id], scope_type="tenant_shared", candidate_limit=20,
        )
        mismatched_tenant = await SearchService(rls_session, embedder, tenant_id="tenant-b").vector_search(
            "deployment owner", room_ids=[room_id], scope_type="tenant_shared", candidate_limit=20,
        )
    historical_ids = {str(result.item_id) for result in historical}
    for name in ("expired", "superseded", "stale-source", "old-date"):
        assert str(await rls_session.scalar(text("SELECT md5(:name)::uuid"), {"name": name})) in historical_ids
    for name in ("private", "other-tenant", "deleted"):
        assert str(await rls_session.scalar(text("SELECT md5(:name)::uuid"), {"name": name})) not in historical_ids
    assert mismatched_tenant == []


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
async def test_relationship_graph_candidate_lookups_use_bounded_directional_indexes(
    plan_session: AsyncSession,
) -> None:
    await plan_session.execute(text("SET LOCAL enable_seqscan = off"))
    result = await plan_session.execute(text("""
        EXPLAIN (FORMAT JSON, COSTS OFF)
        WITH seed_ids AS MATERIALIZED (
            SELECT UNNEST(ARRAY[md5('1')::uuid, md5('2')::uuid]) AS seed_item_id
        )
        SELECT seed.seed_item_id, edge.related_item_id
        FROM seed_ids seed
        CROSS JOIN LATERAL (
            SELECT ir.target_item_id AS related_item_id
            FROM item_relationships ir
            WHERE ir.source_item_id = seed.seed_item_id
              AND ir.confidence >= 0.8
            ORDER BY ir.confidence DESC, ir.target_item_id ASC, ir.relationship ASC
            LIMIT 5
        ) edge
        UNION ALL
        SELECT seed.seed_item_id, edge.related_item_id
        FROM seed_ids seed
        CROSS JOIN LATERAL (
            SELECT ir.source_item_id AS related_item_id
            FROM item_relationships ir
            WHERE ir.target_item_id = seed.seed_item_id
              AND ir.confidence >= 0.8
            ORDER BY ir.confidence DESC, ir.source_item_id ASC, ir.relationship ASC
            LIMIT 5
        ) edge
    """))
    plan = result.scalar_one()
    index_names = {node.get("Index Name") for node in _walk_plan_nodes(plan)}
    assert "sar1069_relationship_source_lookup" in index_names
    assert "sar1069_relationship_target_lookup" in index_names


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


@pytest.mark.asyncio
async def test_daily_llm_budget_statement_executes_with_asyncpg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep asyncpg bind inference aligned with the BIGINT usage columns."""

    assert DATABASE_URL is not None
    async_url = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(async_url)
    async with engine.connect() as connection:
        await connection.execute(text("""
            CREATE TEMP TABLE tenant_llm_daily_usage (
                tenant_id text NOT NULL,
                usage_day date NOT NULL,
                used_tokens bigint NOT NULL DEFAULT 0,
                updated_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (tenant_id, usage_day)
            )
        """))
        await connection.commit()

        monkeypatch.setattr(
            "app.services.llm_admission.settings.tenant_llm_daily_token_limit",
            100,
        )
        monkeypatch.setattr(
            "app.database.async_session",
            lambda **_kwargs: AsyncSession(bind=connection, expire_on_commit=False),
        )

        await consume_tenant_token_budget("tenant-a", 1)
        await consume_tenant_token_budget("tenant-a", 41)
        with pytest.raises(TenantLlmBudgetExceeded):
            await consume_tenant_token_budget("tenant-a", 59)

        used_tokens = await connection.scalar(text("""
            SELECT used_tokens
            FROM tenant_llm_daily_usage
            WHERE tenant_id = 'tenant-a' AND usage_day = CURRENT_DATE
        """))
        assert used_tokens == 42

    await engine.dispose()


@pytest.mark.asyncio
async def test_one_item_with_many_chunks_uses_standard_path(plan_session):
    from unittest.mock import patch

    item_id = await plan_session.scalar(text("SELECT md5('1')::uuid"))
    room_id = await plan_session.scalar(text("SELECT md5('chunk-room')::uuid"))
    await plan_session.execute(text("INSERT INTO room_memberships VALUES ('tenant-a', :item, :room)"),
                               {"item": item_id, "room": room_id})
    await plan_session.execute(text("""
        INSERT INTO embeddings (tenant_id,item_id,chunk_text,chunk_index,embedding_half)
        SELECT 'tenant-a',item_id,chunk_text,n,embedding_half FROM embeddings
        CROSS JOIN generate_series(3,4099) n WHERE item_id=:item AND chunk_index=0
    """), {"item": item_id})
    service = SearchService(plan_session, _DefaultEmbedder(), tenant_id="tenant-a")
    with patch("app.services.search.settings.retrieval_selective_room_search_enabled", True):
        results = await service.vector_search("evergreen documentation 1", room_ids=[room_id])
    assert service.last_candidate_strategy == "standard"
    assert {result.item_id for result in results} == {item_id}
