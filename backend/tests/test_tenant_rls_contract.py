from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

import pytest

import app.models  # noqa: F401
from app import database, enforce_tenant_rls
from app.api import ingest
from app.api.admin import _normalize_tenant_id
from app.database import Base
from app.services.mcp_containment import (
    CONTAINMENT_HERMES_AGENT,
    CONTAINMENT_STANDARD,
    derive_containment_mode,
    normalize_containment_mode,
)
from app.workers import feed_tasks, palace_tasks, tasks
from app.workers.worker import MediaWorkerSettings, PalaceWorkerSettings, WorkerSettings


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "067_tenant_rls_enforcement.py"
PREPARATION_MIGRATION = ROOT / "alembic" / "versions" / "061_tenant_rls.py"
BACKFILL_MIGRATION = ROOT / "alembic" / "versions" / "062_tenant_backfill.py"
GUARD_MIGRATION = ROOT / "alembic" / "versions" / "063_tenant_not_null_guards.py"
VALIDATION_MIGRATION = ROOT / "alembic" / "versions" / "064_tenant_not_null_validation.py"
INDEX_MIGRATION = ROOT / "alembic" / "versions" / "065_tenant_indexes_online.py"
CONSTRAINT_MIGRATION = ROOT / "alembic" / "versions" / "066_tenant_constraints.py"
HISTORICAL_FEEDS_MIGRATION = ROOT / "alembic" / "versions" / "004_rss_feeds.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("tenant_rls_migration", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rls_inventory_matches_every_tenant_model() -> None:
    migration = _load_migration()
    tenant_models = {
        table.name for table in Base.metadata.tables.values() if "tenant_id" in table.c
    }

    assert set(migration.TENANT_TABLES) == tenant_models
    assert set(enforce_tenant_rls.TENANT_TABLES) == tenant_models


def test_rls_policy_is_forced_and_transaction_context_bound() -> None:
    source = MIGRATION.read_text()

    assert "ENABLE ROW LEVEL SECURITY" in source
    assert "FORCE ROW LEVEL SECURITY" in source
    assert "current_setting('app.tenant_id', true)" in source
    assert "WITH CHECK" in source
    assert "app.system_access" in source
    assert "tenant_erasure_states" in source
    assert "app.tenant_id', true) = '*'" not in source
    assert "ALTER COLUMN tenant_id DROP DEFAULT" in source


def test_tenant_constraints_use_online_safe_build_phases() -> None:
    preparation = PREPARATION_MIGRATION.read_text()
    backfill = BACKFILL_MIGRATION.read_text()
    guards = GUARD_MIGRATION.read_text()
    validation = VALIDATION_MIGRATION.read_text()
    indexes = INDEX_MIGRATION.read_text()
    constraints = CONSTRAINT_MIGRATION.read_text()
    enforcement = MIGRATION.read_text()

    assert "CREATE TRIGGER" in preparation
    assert backfill.count("WHERE") >= 7
    assert "UPDATE items SET" in backfill and "metadata IS NULL" in backfill
    assert "UPDATE jobs SET" in backfill and "status IS NULL" in backfill
    assert "NOT VALID" in guards
    assert "SET LOCAL lock_timeout" in guards
    assert "VALIDATE CONSTRAINT" in validation
    assert "postgresql_concurrently=True" in indexes
    assert "UNIQUE USING INDEX" in constraints
    assert "SET LOCAL lock_timeout" in constraints
    assert '"ix_items_tenant_id_id_unique"' in constraints.split("def downgrade", 1)[1]
    assert "postgresql_concurrently=True" in constraints.split("def downgrade", 1)[1]
    assert "NOT VALID" in constraints
    assert "VALIDATE CONSTRAINT" in enforcement
    assert "DEFER_TENANT_RLS_ENFORCEMENT" in enforcement
    assert "autocommit_block" not in enforcement
    assert "SET LOCAL lock_timeout" in enforcement
    assert "lock_timeout" in enforcement
    downgrade = enforcement.split("def downgrade", 1)[1]
    assert "NO FORCE ROW LEVEL SECURITY" in downgrade
    assert "SET DEFAULT" in downgrade
    assert "op.create_check_constraint" in downgrade
    assert "nullable=True" in downgrade


@pytest.mark.parametrize("tenant_id", ["*", "__unbound__", "  *  "])
def test_internal_database_context_cannot_be_registered_as_tenant(tenant_id: str) -> None:
    with pytest.raises(ValueError, match="reserved"):
        _normalize_tenant_id(tenant_id)


def test_alembic_does_not_inherit_application_statement_timeouts() -> None:
    source = (ROOT / "alembic" / "env.py").read_text()

    assert 'migration_server_settings.pop("statement_timeout", None)' in source
    assert 'migration_server_settings.pop("idle_in_transaction_session_timeout", None)' in source


def test_historical_feed_migration_fails_instead_of_deleting_duplicates() -> None:
    upgrade_source = HISTORICAL_FEEDS_MIGRATION.read_text().split("def downgrade", 1)[0]

    assert "duplicate_count" in upgrade_source
    assert "raise RuntimeError" in upgrade_source
    assert "DELETE FROM items" not in upgrade_source


def test_worker_tenant_arguments_accept_only_legacy_missing_tenant_default() -> None:
    functions = (
        tasks.process_media,
        tasks.process_webpage,
        tasks.process_pdf,
        tasks.process_doc,
        tasks.process_image,
        tasks.process_note,
        tasks.extract_relationships,
        tasks.embed_item,
        feed_tasks.poll_feed,
        feed_tasks.process_feed_item,
    )

    for function in functions:
        tenant = inspect.signature(function).parameters["tenant_id"]
        assert tenant.default is None, function.__name__


def test_unbound_session_factory_has_no_system_access() -> None:
    assert database.async_session.kw["info"] == {
        "tenant_id": "__unbound__",
        "system_access": False,
    }


@pytest.mark.asyncio
async def test_credential_discovery_rebinds_before_tenant_work() -> None:
    class Session:
        def __init__(self) -> None:
            self.info = {"tenant_id": "__unbound__", "system_access": True}
            self.rolled_back = False

        async def rollback(self) -> None:
            self.rolled_back = True

    session = Session()
    await database.bind_session_to_tenant(session, "tenant-a")

    assert session.rolled_back is True
    assert session.info == {"tenant_id": "tenant-a", "system_access": False}


def test_tenant_erasure_keeps_external_io_outside_atomic_lock_window() -> None:
    source = (ROOT / "app" / "services" / "data_lifecycle.py").read_text()
    function = source.split("async def erase_tenant_data", 1)[1].split(
        "async def hard_delete_item", 1
    )[0]

    first_queue_scan = function.index("tenant_arq_jobs = await _find_tenant_arq_jobs")
    lock = function.index("LOCK TABLE")
    marker = function.index("INSERT INTO tenant_erasure_states")
    assert first_queue_scan < lock < marker
    assert "SET LOCAL statement_timeout = 0" in function
    assert "SET LOCAL idle_in_transaction_session_timeout = 0" in function


def test_tenant_erasure_resolves_only_queued_identifier_candidates() -> None:
    source = (ROOT / "app" / "services" / "data_lifecycle.py").read_text()
    resolver = source.split("async def _tenant_identifiers", 1)[1].split(
        "async def tenant_row_counts", 1
    )[0]

    assert "if not candidates" in resolver
    assert "range(0, len(candidate_ids), 500)" in resolver
    assert "table.c.id.in_(batch)" in resolver


def test_destructive_deletion_paths_fail_closed_across_external_state() -> None:
    source = (ROOT / "app" / "services" / "data_lifecycle.py").read_text()
    tenant_erasure = source.split("async def erase_tenant_data", 1)[1].split(
        "async def hard_delete_item", 1
    )[0]
    item_erasure = source.split("async def hard_delete_item", 1)[1]

    commit = tenant_erasure.index("await db.commit()")
    verify = tenant_erasure.index("_audit_event_was_committed", commit)
    restore = tenant_erasure.index("_restore_staged_paths", verify)
    assert commit < verify < restore
    assert "leaving artifacts quarantined and queue closed" in tenant_erasure
    assert "if not commit_attempted" in tenant_erasure

    find_jobs = item_erasure.index("_find_identifier_arq_jobs")
    purge_jobs = item_erasure.index("_purge_arq_jobs", find_jobs)
    delete_jobs = item_erasure.index("delete(Job)", purge_jobs)
    delete_item = item_erasure.index("delete(Item)", delete_jobs)
    assert find_jobs < purge_jobs < delete_jobs < delete_item
    assert "_audit_event_was_committed" in item_erasure
    assert "leaving artifacts quarantined" in item_erasure


def test_every_worker_honors_tenant_erasure_abort_requests() -> None:
    assert WorkerSettings.allow_abort_jobs is True
    assert MediaWorkerSettings.allow_abort_jobs is True
    assert PalaceWorkerSettings.allow_abort_jobs is True


def test_tenant_maintenance_scripts_bind_database_sessions() -> None:
    scripts = (
        "backfill_claim_sources.py",
        "enroll_watched_sources.py",
        "reembed_tenant.py",
        "seed_watched_source_canary.py",
    )
    for name in scripts:
        source = (ROOT / "scripts" / name).read_text()
        assert "tenant_async_session(" in source, name
        assert "async with async_session()" not in source, name


def test_arq_worker_entrypoints_do_not_swallow_unknown_arguments() -> None:
    worker_modules = (feed_tasks, palace_tasks, tasks)
    for module in worker_modules:
        for function in vars(module).values():
            if not inspect.iscoroutinefunction(function):
                continue
            if function.__module__ != module.__name__:
                continue
            parameters = inspect.signature(function).parameters.values()
            assert not any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            ), f"{module.__name__}.{function.__name__}"


def test_containment_derivation_handles_adversarial_client_names() -> None:
    expectations = {
        "hermes": CONTAINMENT_HERMES_AGENT,
        "Hermes_prod": CONTAINMENT_HERMES_AGENT,
        "hermes.prod": CONTAINMENT_HERMES_AGENT,
        " hermes prod ": CONTAINMENT_HERMES_AGENT,
        "hermes--prod": CONTAINMENT_HERMES_AGENT,
        "hermesprod": CONTAINMENT_STANDARD,
        "hermesproduction": CONTAINMENT_STANDARD,
        "hermes2": CONTAINMENT_STANDARD,
        "not-hermes": CONTAINMENT_STANDARD,
    }

    for client_key, expected in expectations.items():
        assert derive_containment_mode(client_key=client_key) == expected

    containment_migration = (
        ROOT / "alembic" / "versions" / "057_mcp_client_containment_mode.py"
    ).read_text()
    assert "regexp_replace(lower(trim(client_key))" in containment_migration
    assert "LIKE 'hermes-%'" in containment_migration

    backfill = (ROOT / "alembic" / "versions" / "062_tenant_backfill.py").read_text()
    assert "UPDATE mcp_clients SET containment_mode = 'standard'" not in backfill


@pytest.mark.asyncio
async def test_document_extraction_tenant_slots_do_not_accumulate(monkeypatch) -> None:
    monkeypatch.setattr(ingest.settings, "doc_extraction_per_tenant_concurrency", 1)

    async with ingest._doc_extraction_slot("tenant-a"):
        assert "tenant-a" in ingest._doc_extraction_tenant_semaphores
        assert ingest._doc_extraction_tenant_refcounts["tenant-a"] == 1

    assert "tenant-a" not in ingest._doc_extraction_tenant_semaphores
    assert "tenant-a" not in ingest._doc_extraction_tenant_refcounts


def test_unknown_mcp_containment_mode_fails_closed_without_affecting_rest() -> None:
    assert normalize_containment_mode(None) == CONTAINMENT_STANDARD
    assert normalize_containment_mode("corrupt-mode") == CONTAINMENT_HERMES_AGENT
