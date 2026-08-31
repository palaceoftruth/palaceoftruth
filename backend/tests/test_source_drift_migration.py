from pathlib import Path


def test_source_drift_migration_preserves_provenance_and_idempotency() -> None:
    migration = Path(__file__).parents[1] / "alembic" / "versions" / "071_source_drift_curation_artifacts.py"
    source = migration.read_text()

    assert 'down_revision = "070_item_governance"' in source
    assert "candidate_source_drift" in source
    assert "previous_source_record_id" in source
    assert "current_source_record_id" in source
    assert "affected_item_ids" in source
    assert "affected_claim_ids" in source
    assert "evidence_diff" in source
    assert "uq_candidate_curation_tenant_dedupe" in source
    assert "legacy_source_drift" in source
    assert "artifact_kind = 'candidate_memory_reflection'" in source
