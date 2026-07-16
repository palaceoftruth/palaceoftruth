#!/usr/bin/env python3
"""Exercise Palace migrations against an explicitly disposable pgvector database."""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

import asyncpg


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
SAFE_DATABASE_PREFIX = "palace_migration_"
SAFE_DATABASE_HOSTS = {"localhost", "127.0.0.1", "::1"}
# Earlier data-shape migrations intentionally reject a destructive downgrade.
# This target exercises every currently reversible migration without bypassing
# that safety contract.
ROUNDTRIP_DOWNGRADE_TARGET = "016_temporal_fact_registry"
TENANT_QUALIFIED_FOREIGN_KEYS = {
    "fk_source_resources_current_record_tenant",
    "fk_source_resources_last_success_record_tenant",
    "fk_source_resource_aliases_resource_tenant",
    "fk_source_resource_audit_resource_tenant",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("MIGRATION_TEST_DATABASE_URL"),
        help="Disposable PostgreSQL URL; defaults to MIGRATION_TEST_DATABASE_URL.",
    )
    parser.add_argument(
        "--allow-destructive",
        action="store_true",
        default=os.environ.get("MIGRATION_TEST_ALLOW_DESTRUCTIVE") == "1",
        help="Acknowledge the harness will run reversible migration downgrades on this local database.",
    )
    return parser.parse_args()


def validate_disposable_database_url(database_url: str | None, *, allow_destructive: bool) -> str:
    if not database_url:
        raise ValueError("--database-url or MIGRATION_TEST_DATABASE_URL is required")

    parsed = urlparse(database_url.replace("postgresql+asyncpg", "postgresql", 1))
    database_name = parsed.path.lstrip("/")
    if parsed.scheme not in {"postgresql", "postgres"}:
        raise ValueError("migration harness requires a PostgreSQL URL")
    if not database_name.startswith(SAFE_DATABASE_PREFIX):
        raise ValueError(
            f"migration harness only accepts databases beginning with {SAFE_DATABASE_PREFIX!r}"
        )
    if parsed.hostname not in SAFE_DATABASE_HOSTS:
        raise ValueError(
            "migration harness only accepts a loopback PostgreSQL host; use a local disposable container"
        )
    if not allow_destructive:
        raise ValueError(
            "migration harness requires --allow-destructive or MIGRATION_TEST_ALLOW_DESTRUCTIVE=1"
        )
    return database_url


def run_alembic(database_url: str, *arguments: str) -> None:
    # Alembic imports application settings even though no application service is
    # started. Provide inert values so this database-only harness needs no secret.
    environment = os.environ | {
        "DATABASE_URL": database_url.replace("postgresql://", "postgresql+asyncpg://", 1),
        "API_KEY": "migration-harness-unused",
        "OPENAI_API_KEY": "migration-harness-unused",
        "OPENROUTER_API_KEY": "migration-harness-unused",
    }
    subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=BACKEND_ROOT,
        env=environment,
        check=True,
    )


async def assert_pgvector_and_tenant_foreign_key_probes(database_url: str) -> None:
    connection = await asyncpg.connect(database_url.replace("postgresql+asyncpg", "postgresql", 1))
    try:
        extension_present = await connection.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')"
        )
        if not extension_present:
            raise RuntimeError("pgvector extension is not installed after migration")

        # Require the tenant-qualified constraints introduced by migration 041.
        # The functional probes below also reject cross-tenant references, so a
        # future single-column replacement with the same constraint name fails.
        present_constraints = set(await connection.fetch(
            """
            SELECT conname
            FROM pg_constraint
            WHERE contype = 'f' AND conname = ANY($1::text[])
            """
            , list(TENANT_QUALIFIED_FOREIGN_KEYS)
        ))
        found_constraint_names = {row["conname"] for row in present_constraints}
        missing_constraints = TENANT_QUALIFIED_FOREIGN_KEYS - found_constraint_names
        if missing_constraints:
            raise RuntimeError(f"missing tenant-qualified foreign keys: {sorted(missing_constraints)}")

        source_item_id = await connection.fetchval(
            """
            INSERT INTO items (tenant_id, source_type, title)
            VALUES ('migration-tenant-a', 'migration_probe', 'tenant foreign key probe')
            RETURNING id
            """
        )
        source_record_id = await connection.fetchval(
            """
            INSERT INTO source_records (tenant_id, item_id, source_kind, source_version, content_hash)
            VALUES ('migration-tenant-a', $1, 'migration_probe', 'v1', 'migration-probe')
            RETURNING id
            """,
            source_item_id,
        )
        async def expect_cross_tenant_rejection(statement: str, *arguments: object) -> None:
            try:
                await connection.execute(statement, *arguments)
            except asyncpg.ForeignKeyViolationError:
                return
            raise RuntimeError("cross-tenant source resource reference was accepted")

        await expect_cross_tenant_rejection(
            """
            INSERT INTO source_resources (tenant_id, canonical_url, canonical_identity, current_source_record_id)
            VALUES ('migration-tenant-b', 'https://example.test/current', 'migration-current', $1)
            """,
            source_record_id,
        )
        await expect_cross_tenant_rejection(
            """
            INSERT INTO source_resources (tenant_id, canonical_url, canonical_identity, last_successful_source_record_id)
            VALUES ('migration-tenant-b', 'https://example.test/last-success', 'migration-last-success', $1)
            """,
            source_record_id,
        )
        resource_id = await connection.fetchval(
            """
            INSERT INTO source_resources (tenant_id, canonical_url, canonical_identity)
            VALUES ('migration-tenant-a', 'https://example.test/resource', 'migration-resource')
            RETURNING id
            """
        )
        await expect_cross_tenant_rejection(
            """
            INSERT INTO source_resource_aliases (tenant_id, resource_id, submitted_url, normalized_url, signal, decision)
            VALUES ('migration-tenant-b', $1, 'https://example.test/alias', 'https://example.test/alias', 'submitted', 'accepted')
            """,
            resource_id,
        )
        await expect_cross_tenant_rejection(
            """
            INSERT INTO source_resource_audit_snapshots (tenant_id, resource_id, event_kind, next_snapshot)
            VALUES ('migration-tenant-b', $1, 'migration_probe', '{}'::jsonb)
            """,
            resource_id,
        )
        print(f"pgvector=present tenant_qualified_foreign_key_probes={len(found_constraint_names)}")
    finally:
        await connection.close()


async def main() -> int:
    try:
        args = parse_args()
        database_url = validate_disposable_database_url(
            args.database_url, allow_destructive=args.allow_destructive
        )
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2

    run_alembic(database_url, "upgrade", "head")
    await assert_pgvector_and_tenant_foreign_key_probes(database_url)
    run_alembic(database_url, "downgrade", ROUNDTRIP_DOWNGRADE_TARGET)
    run_alembic(database_url, "upgrade", "head")
    await assert_pgvector_and_tenant_foreign_key_probes(database_url)
    print(
        "migration round-trip passed: "
        f"upgrade -> downgrade {ROUNDTRIP_DOWNGRADE_TARGET} -> upgrade"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
