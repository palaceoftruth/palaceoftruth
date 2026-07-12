from __future__ import annotations

import json
from pathlib import Path

from app.embedding_profile import EMBEDDING_DIMENSIONS
from app.services.database_health import (
    MIGRATION_EXPECTATIONS,
    EXPECTED_INDEXES,
    EXPECTED_TABLES,
    redact_database_url,
    report_to_json,
    run_static_health,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_static_database_health_gate_passes_for_source_tree() -> None:
    report = run_static_health(REPO_ROOT)

    assert report.ok, report_to_json(report)
    assert {check.name for check in report.checks} == {
        "alembic migration chain",
        "embedding profile config",
        "SQLAlchemy model expectations",
        "source-controlled database expectations",
        "chart pgvector bootstrap",
    }


def test_database_health_expectations_track_palace_critical_surfaces() -> None:
    expectation_patterns = {
        pattern
        for expectation in MIGRATION_EXPECTATIONS
        for pattern in expectation.patterns
    }

    assert f"vector({EMBEDDING_DIMENSIONS})" in expectation_patterns
    assert f"halfvec({EMBEDDING_DIMENSIONS})" in expectation_patterns
    assert "embedding_profile_vectors" in EXPECTED_TABLES
    assert {
        "items",
        "embeddings",
        "api_keys",
        "mcp_clients",
        "job_progress_events",
        "source_subscriptions",
        "source_subscription_entries",
    } <= EXPECTED_TABLES
    assert {
        "idx_embeddings_halfvec_hnsw",
        "ix_embeddings_item_chunk",
        "idx_embedding_profile_vectors_halfvec_384_hnsw",
        "idx_embedding_profile_vectors_halfvec_768_hnsw",
        "idx_embedding_profile_vectors_halfvec_1024_hnsw",
        "idx_embedding_profile_vectors_halfvec_1536_hnsw",
        "idx_items_search_vector",
    } <= EXPECTED_INDEXES


def test_static_database_health_accepts_side_by_side_embedding_profile_dimension(monkeypatch) -> None:
    from app.services import database_health

    monkeypatch.setattr(database_health.settings, "embedding_provider", "local-http")
    monkeypatch.setattr(database_health.settings, "embedding_model", "gte-modernbert-base")
    monkeypatch.setattr(database_health.settings, "embedding_dimensions", 768)
    monkeypatch.setattr(database_health.settings, "embedding_profile_name", "local-http-gte-modernbert-base")

    report = run_static_health(REPO_ROOT)

    profile_check = next(check for check in report.checks if check.name == "embedding profile config")
    assert profile_check.ok is True
    assert "dimensions=768" in profile_check.detail
    assert "rollout_state=default-enabled" in profile_check.detail


def test_static_database_health_reports_native_image_profile_as_experimental(monkeypatch) -> None:
    from app.services import database_health

    monkeypatch.setattr(database_health.settings, "embedding_provider", "local-http")
    monkeypatch.setattr(database_health.settings, "embedding_model", "")
    monkeypatch.setattr(database_health.settings, "embedding_dimensions", 1536)
    monkeypatch.setattr(database_health.settings, "embedding_profile_name", "local-http-clip-native-image-768")
    monkeypatch.setattr(database_health.settings, "embedding_experimental_profiles_enabled", True)

    report = run_static_health(REPO_ROOT)

    profile_check = next(check for check in report.checks if check.name == "embedding profile config")
    assert profile_check.ok is True
    assert "profile_kind=native_image" in profile_check.detail
    assert "input_modality=image" in profile_check.detail
    assert "rollout_state=experimental-report-only" in profile_check.detail


def test_database_health_json_reports_failures() -> None:
    report = run_static_health(REPO_ROOT / "missing")
    payload = json.loads(report_to_json(report))

    assert payload["ok"] is False
    assert any(check["status"] == "fail" for check in payload["checks"])


def test_live_database_url_is_redacted() -> None:
    assert (
        redact_database_url("postgresql+asyncpg://palace:secret@example.test:5432/palace")
        == "postgresql+asyncpg://palace:***@example.test:5432/palace"
    )
    assert redact_database_url("postgresql+asyncpg://example.test/palace") == (
        "postgresql+asyncpg://example.test/palace"
    )


def test_live_database_health_only_accepts_valid_ready_indexes() -> None:
    source = (REPO_ROOT / "backend/app/services/database_health.py").read_text()

    assert "index_state.indisvalid and index_state.indisready" in source
