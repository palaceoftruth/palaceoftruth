"""Database schema and index health checks for Palace deployments."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import app.models  # noqa: F401 - importing registers model tables on Base.metadata
from app.config import settings
from app.database import Base
from app.embedding_profile import EMBEDDING_DIMENSIONS, SUPPORTED_PROFILE_VECTOR_DIMENSIONS, resolve_embedding_profile


@dataclass(frozen=True)
class HealthCheck:
    name: str
    ok: bool
    detail: str

    @property
    def status(self) -> str:
        return "pass" if self.ok else "fail"


@dataclass(frozen=True)
class HealthReport:
    mode: str
    checks: tuple[HealthCheck, ...]
    database_url: str | None = None

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "mode": self.mode,
            "ok": self.ok,
            "checks": [
                {"name": check.name, "status": check.status, "detail": check.detail}
                for check in self.checks
            ],
        }
        if self.database_url:
            payload["database_url"] = self.database_url
        return payload


@dataclass(frozen=True)
class MigrationExpectation:
    name: str
    patterns: tuple[str, ...]


EXPECTED_TABLES = frozenset(
    {
        "items",
        "embeddings",
        "embedding_profile_vectors",
        "item_relationships",
        "jobs",
        "job_progress_events",
        "api_keys",
        "api_key_audit_events",
        "mcp_clients",
        "mcp_request_audit_events",
        "mcp_oauth_access_tokens",
        "rooms",
        "room_memberships",
        "room_snapshots",
        "room_tunnels",
        "palace_runs",
        "palace_dirty_items",
        "source_subscriptions",
        "source_subscription_entries",
    }
)

EXPECTED_INDEXES = frozenset(
    {
        "idx_embeddings_halfvec_hnsw",
        "idx_embedding_profile_vectors_halfvec_384_hnsw",
        "idx_embedding_profile_vectors_halfvec_768_hnsw",
        "idx_embedding_profile_vectors_halfvec_1024_hnsw",
        "idx_embedding_profile_vectors_halfvec_1536_hnsw",
        "idx_items_search_vector",
        "idx_items_tenant_status",
        "idx_jobs_tenant_status",
        "idx_api_keys_key_hash",
        "ix_mcp_clients_tenant_last_seen",
        "ix_mcp_request_audit_events_tenant_created_at",
        "ix_mcp_request_audit_events_client_id",
        "ix_mcp_oauth_access_tokens_tenant_expires",
        "ix_job_progress_events_job_created",
        "ix_job_progress_events_tenant_created",
        "ix_items_tenant_deleted_status",
        "ix_feeds_tenant_deleted",
        "ix_room_memberships_tenant_item_id",
        "ix_palace_dirty_items_tenant_generation",
        "ix_source_subscriptions_tenant_status",
        "ix_source_subscriptions_tenant_deleted",
        "ix_source_subscription_entries_subscription_status",
        "ix_source_subscription_entries_tenant_status",
    }
)

EXPECTED_CONSTRAINTS = frozenset(
    {
        "uq_mcp_clients_tenant_client_key",
        "uq_mcp_oauth_access_tokens_token_hash",
        "uq_room_memberships_room_item_source",
        "uq_room_snapshots_room_generation",
        "uq_palace_dirty_items_tenant_item",
        "uq_embedding_profile_vectors_item_chunk_profile",
        "ck_embedding_profile_vectors_profile_kind",
        "ck_embedding_profile_vectors_input_modality",
    }
)

MIGRATION_EXPECTATIONS = (
    MigrationExpectation("pgcrypto extension", ('CREATE EXTENSION IF NOT EXISTS "pgcrypto"',)),
    MigrationExpectation("pgvector extension", ('CREATE EXTENSION IF NOT EXISTS "vector"',)),
    MigrationExpectation("embedding vector dimensions", (f"vector({EMBEDDING_DIMENSIONS})",)),
    MigrationExpectation("embedding halfvec dimensions", (f"halfvec({EMBEDDING_DIMENSIONS})",)),
    MigrationExpectation(
        "embedding profile vector table",
        (
            "embedding_profile_vectors",
            "profile_name text not null",
            "vector(384)",
            "halfvec(384)",
            "vector(768)",
            "halfvec(768)",
            "vector(1024)",
            "halfvec(1024)",
            "uq_embedding_profile_vectors_item_chunk_profile",
            "profile_kind text not null",
            "input_modality text not null",
            "profile_metadata jsonb not null",
            "ck_embedding_profile_vectors_profile_kind",
            "'native_image'",
            "ck_embedding_profile_vectors_input_modality",
            "'multilingual_text'",
        ),
    ),
    MigrationExpectation(
        "halfvec HNSW cosine index",
        ("idx_embeddings_halfvec_hnsw", "USING hnsw", "halfvec_cosine_ops"),
    ),
    MigrationExpectation(
        "hybrid search vector index",
        ("search_vector tsvector", "idx_items_search_vector", "USING GIN"),
    ),
    MigrationExpectation("tenant API key table", ("api_keys", "key_hash", "idx_api_keys_key_hash")),
    MigrationExpectation(
        "tenant-scoped memory idempotency indexes",
        ("idx_items_content_hash_tenant_unique", "idx_items_idempotency_key_tenant_unique"),
    ),
    MigrationExpectation(
        "relationship table uniqueness",
        ("item_relationships", "source_item_id", "target_item_id", "relationship"),
    ),
    MigrationExpectation(
        "MCP client audit tables",
        ("mcp_clients", "mcp_request_audit_events", "uq_mcp_clients_tenant_client_key"),
    ),
    MigrationExpectation(
        "MCP OAuth token table",
        ("mcp_oauth_access_tokens", "uq_mcp_oauth_access_tokens_token_hash"),
    ),
    MigrationExpectation(
        "job progress event indexes",
        ("job_progress_events", "ix_job_progress_events_job_created", "ix_job_progress_events_tenant_created"),
    ),
    MigrationExpectation(
        "soft delete readiness indexes",
        ("deleted_at", "ix_items_tenant_deleted_status", "ix_feeds_tenant_deleted"),
    ),
    MigrationExpectation(
        "source subscription foundation",
        (
            "source_subscriptions",
            "source_subscription_entries",
            "uq_source_subscriptions_active_external",
            "uq_source_subscription_entries_provider_entry",
            "ix_source_subscription_entries_subscription_status",
        ),
    ),
)


def redact_database_url(database_url: str) -> str:
    parts = urlsplit(database_url)
    if not parts.netloc or "@" not in parts.netloc:
        return database_url
    userinfo, host = parts.netloc.rsplit("@", 1)
    username = userinfo.split(":", 1)[0]
    redacted_userinfo = f"{username}:***" if username else "***"
    return urlunsplit((parts.scheme, f"{redacted_userinfo}@{host}", parts.path, parts.query, parts.fragment))


def run_static_health(repo_root: Path) -> HealthReport:
    repo_root = repo_root.resolve()
    versions_dir = repo_root / "backend" / "alembic" / "versions"
    checks = [
        _check_alembic_chain(repo_root),
        _check_embedding_profile_config(),
        _check_model_expectations(),
        _check_migration_expectations(versions_dir),
        _check_chart_pgvector_bootstrap(repo_root),
    ]
    return HealthReport(mode="static", checks=tuple(checks))


async def run_live_health(repo_root: Path, database_url: str) -> HealthReport:
    static_report = run_static_health(repo_root)
    live_checks = await _inspect_live_database(database_url, _local_alembic_head(repo_root))
    return HealthReport(
        mode="live",
        checks=static_report.checks + tuple(live_checks),
        database_url=redact_database_url(database_url),
    )


def render_report(report: HealthReport) -> str:
    lines = [
        f"database-health mode={report.mode} status={'pass' if report.ok else 'fail'}",
    ]
    if report.database_url:
        lines.append(f"database={report.database_url}")
    for check in report.checks:
        lines.append(f"[{check.status}] {check.name}: {check.detail}")
    return "\n".join(lines)


def report_to_json(report: HealthReport) -> str:
    return json.dumps(report.to_dict(), indent=2, sort_keys=True)


def _check_alembic_chain(repo_root: Path) -> HealthCheck:
    try:
        script = _alembic_script(repo_root)
        heads = script.get_heads()
        bases = script.get_bases()
        revisions = list(script.walk_revisions())
    except Exception as exc:
        return HealthCheck("alembic migration chain", False, f"failed to load Alembic scripts: {exc}")

    if len(heads) != 1:
        return HealthCheck("alembic migration chain", False, f"expected one Alembic head, found {heads}")
    if len(bases) != 1:
        return HealthCheck("alembic migration chain", False, f"expected one Alembic base, found {bases}")
    if not revisions:
        return HealthCheck("alembic migration chain", False, "no migration revisions were found")
    return HealthCheck(
        "alembic migration chain",
        True,
        f"single head {heads[0]} across {len(revisions)} revisions",
    )


def _check_migration_expectations(versions_dir: Path) -> HealthCheck:
    if not versions_dir.exists():
        return HealthCheck("source-controlled database expectations", False, f"missing {versions_dir}")
    migration_text = "\n".join(path.read_text() for path in sorted(versions_dir.glob("*.py")))
    normalized = _normalize_sql(migration_text)
    missing = [
        expectation.name
        for expectation in MIGRATION_EXPECTATIONS
        if not all(_normalize_sql(pattern) in normalized for pattern in expectation.patterns)
    ]
    if missing:
        return HealthCheck(
            "source-controlled database expectations",
            False,
            "missing migration evidence for " + ", ".join(missing),
        )
    return HealthCheck(
        "source-controlled database expectations",
        True,
        f"{len(MIGRATION_EXPECTATIONS)} Palace schema/index expectations present",
    )


def _check_model_expectations() -> HealthCheck:
    tables = Base.metadata.tables
    missing_tables = sorted(EXPECTED_TABLES - set(tables))
    failures = [f"missing model table {table}" for table in missing_tables]

    embeddings = tables.get("embeddings")
    if embeddings is not None:
        expected_embedding_types = {
            "embedding": f"VECTOR({EMBEDDING_DIMENSIONS})",
            "embedding_half": f"HALFVEC({EMBEDDING_DIMENSIONS})",
        }
        for column_name, expected_type in expected_embedding_types.items():
            column = embeddings.columns.get(column_name)
            actual_type = None if column is None else str(column.type)
            if actual_type != expected_type:
                failures.append(f"embeddings.{column_name} type {actual_type!r}, expected {expected_type}")
        profile_columns = {
            "profile_name": "TEXT",
            "provider": "TEXT",
            "model": "TEXT",
            "dimensions": "INTEGER",
        }
        for column_name, expected_type in profile_columns.items():
            column = embeddings.columns.get(column_name)
            actual_type = None if column is None else str(column.type)
            if actual_type != expected_type:
                failures.append(f"embeddings.{column_name} type {actual_type!r}, expected {expected_type}")

    profile_vectors = tables.get("embedding_profile_vectors")
    if profile_vectors is not None:
        profile_metadata_columns = {
            "profile_kind": "TEXT",
            "input_modality": "TEXT",
            "profile_metadata": "JSONB",
        }
        for column_name, expected_type in profile_metadata_columns.items():
            column = profile_vectors.columns.get(column_name)
            actual_type = None if column is None else str(column.type)
            if actual_type != expected_type:
                failures.append(
                    f"embedding_profile_vectors.{column_name} type {actual_type!r}, expected {expected_type}"
                )
        for dimensions in sorted(SUPPORTED_PROFILE_VECTOR_DIMENSIONS):
            vector_column = profile_vectors.columns.get(f"embedding_{dimensions}")
            half_column = profile_vectors.columns.get(f"embedding_half_{dimensions}")
            expected_vector = f"VECTOR({dimensions})"
            expected_half = f"HALFVEC({dimensions})"
            actual_vector = None if vector_column is None else str(vector_column.type)
            actual_half = None if half_column is None else str(half_column.type)
            if actual_vector != expected_vector:
                failures.append(
                    f"embedding_profile_vectors.embedding_{dimensions} type {actual_vector!r}, "
                    f"expected {expected_vector}"
                )
            if actual_half != expected_half:
                failures.append(
                    f"embedding_profile_vectors.embedding_half_{dimensions} type {actual_half!r}, "
                    f"expected {expected_half}"
                )

    items = tables.get("items")
    if items is not None:
        search_vector = items.columns.get("search_vector")
        if search_vector is None or str(search_vector.type).upper() != "TSVECTOR":
            failures.append("items.search_vector model column is missing or not TSVECTOR")

    job_progress = tables.get("job_progress_events")
    if job_progress is not None:
        model_indexes = {index.name for index in job_progress.indexes}
        required = {"ix_job_progress_events_job_created", "ix_job_progress_events_tenant_created"}
        missing = sorted(required - model_indexes)
        if missing:
            failures.append("job_progress_events model missing indexes " + ", ".join(missing))

    if failures:
        return HealthCheck("SQLAlchemy model expectations", False, "; ".join(failures))
    return HealthCheck(
        "SQLAlchemy model expectations",
        True,
        f"{len(EXPECTED_TABLES)} critical tables and vector/search columns registered",
    )


def _check_embedding_profile_config() -> HealthCheck:
    try:
        profile = resolve_embedding_profile(
            provider=settings.embedding_provider,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            profile_name=settings.embedding_profile_name,
            experimental_profiles_enabled=settings.embedding_experimental_profiles_enabled,
        )
    except Exception as exc:
        return HealthCheck("embedding profile config", False, f"invalid embedding profile config: {exc}")

    if profile.dimensions not in SUPPORTED_PROFILE_VECTOR_DIMENSIONS:
        return HealthCheck(
            "embedding profile config",
            False,
            f"profile {profile.profile_name!r} uses {profile.dimensions} dimensions, "
            "but no matching embedding profile vector column exists",
        )

    rollout_state = "default-enabled" if profile.enabled_by_default else "experimental-report-only"
    return HealthCheck(
        "embedding profile config",
        True,
        (
            f"profile {profile.profile_name!r} provider={profile.provider} "
            f"model={profile.model} dimensions={profile.dimensions} "
            f"profile_kind={profile.profile_kind} input_modality={profile.input_modality} "
            f"rollout_state={rollout_state}"
        ),
    )


def _check_chart_pgvector_bootstrap(repo_root: Path) -> HealthCheck:
    chart_path = repo_root / "chart" / "templates" / "postgres-cluster.yaml"
    if not chart_path.exists():
        return HealthCheck("chart pgvector bootstrap", False, f"missing {chart_path}")
    text = chart_path.read_text()
    required = ('CREATE EXTENSION IF NOT EXISTS "vector"', "postInitApplicationSQL")
    missing = [pattern for pattern in required if pattern not in text]
    if missing:
        return HealthCheck("chart pgvector bootstrap", False, "missing " + ", ".join(missing))
    return HealthCheck("chart pgvector bootstrap", True, "CNPG initdb creates vector extension")


async def _inspect_live_database(database_url: str, local_head: str) -> list[HealthCheck]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            version = await conn.scalar(text("select version_num from alembic_version"))
            extensions = set((await conn.execute(text("select extname from pg_extension"))).scalars().all())
            tables = set(
                (await conn.execute(
                    text(
                        "select table_name from information_schema.tables "
                        "where table_schema = 'public' and table_type = 'BASE TABLE'"
                    )
                )).scalars().all()
            )
            indexes = set(
                (await conn.execute(
                    text("select indexname from pg_indexes where schemaname = 'public'")
                )).scalars().all()
            )
            constraints = set(
                (await conn.execute(
                    text("select conname from pg_constraint where connamespace = 'public'::regnamespace")
                )).scalars().all()
            )
            type_rows = (
                await conn.execute(
                    text(
                        """
                        select c.relname as table_name, a.attname as column_name,
                               format_type(a.atttypid, a.atttypmod) as formatted_type
                        from pg_attribute a
                        join pg_class c on c.oid = a.attrelid
                        join pg_namespace n on n.oid = c.relnamespace
                        where n.nspname = 'public'
                          and c.relkind = 'r'
                          and c.relname in ('embeddings', 'items', 'embedding_profile_vectors')
                          and a.attname in (
                              'embedding', 'embedding_half', 'search_vector',
                              'embedding_384', 'embedding_half_384',
                              'embedding_768', 'embedding_half_768',
                              'embedding_1024', 'embedding_half_1024',
                              'embedding_1536', 'embedding_half_1536'
                          )
                          and a.attnum > 0
                          and not a.attisdropped
                        """
                    )
                )
            ).mappings().all()
            types = {
                (row["table_name"], row["column_name"]): row["formatted_type"]
                for row in type_rows
            }
    except Exception as exc:
        return [HealthCheck("live database inspection", False, f"connection or inspection failed: {exc}")]
    finally:
        await engine.dispose()

    checks = [
        HealthCheck(
            "live Alembic head",
            version == local_head,
            f"database head {version!r}, local head {local_head!r}",
        ),
        _presence_check("live required extensions", {"pgcrypto", "vector"}, extensions),
        _presence_check("live required tables", EXPECTED_TABLES, tables),
        _presence_check("live required indexes", EXPECTED_INDEXES, indexes),
        _presence_check("live required constraints", EXPECTED_CONSTRAINTS, constraints),
    ]

    expected_types = {
        ("embeddings", "embedding"): f"vector({EMBEDDING_DIMENSIONS})",
        ("embeddings", "embedding_half"): f"halfvec({EMBEDDING_DIMENSIONS})",
        ("items", "search_vector"): "tsvector",
    }
    for dimensions in sorted(SUPPORTED_PROFILE_VECTOR_DIMENSIONS):
        expected_types[("embedding_profile_vectors", f"embedding_{dimensions}")] = f"vector({dimensions})"
        expected_types[("embedding_profile_vectors", f"embedding_half_{dimensions}")] = f"halfvec({dimensions})"
    type_mismatches = [
        f"{table}.{column}={types.get((table, column))!r}, expected {expected}"
        for (table, column), expected in expected_types.items()
        if types.get((table, column)) != expected
    ]
    checks.append(
        HealthCheck(
            "live vector and search column types",
            not type_mismatches,
            "all critical column types match" if not type_mismatches else "; ".join(type_mismatches),
        )
    )
    return checks


def _presence_check(name: str, expected: Iterable[str], observed: set[str]) -> HealthCheck:
    expected_set = set(expected)
    missing = sorted(expected_set - observed)
    if missing:
        return HealthCheck(name, False, "missing " + ", ".join(missing))
    return HealthCheck(name, True, f"{len(expected_set)} expected objects present")


def _alembic_script(repo_root: Path) -> ScriptDirectory:
    config = Config()
    config.set_main_option("script_location", str(repo_root / "backend" / "alembic"))
    return ScriptDirectory.from_config(config)


def _local_alembic_head(repo_root: Path) -> str:
    heads = _alembic_script(repo_root).get_heads()
    if len(heads) != 1:
        raise RuntimeError(f"expected one local Alembic head, found {heads}")
    return heads[0]


def _normalize_sql(value: str) -> str:
    return re.sub(r"\s+", " ", value).lower()
