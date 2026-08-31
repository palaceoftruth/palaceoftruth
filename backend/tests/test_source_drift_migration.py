from pathlib import Path


def test_source_drift_migration_preserves_provenance_and_idempotency() -> None:
    migration = Path(__file__).parents[1] / "alembic" / "versions" / "071_source_drift_curation_artifacts.py"
    source = migration.read_text()
    source_resource_migration = (
        Path(__file__).parents[1] / "alembic" / "versions" / "041_source_resources.py"
    ).read_text()

    assert 'down_revision = "070_item_governance"' in source
    assert "candidate_source_drift" in source
    assert "previous_source_record_id" in source
    assert "current_source_record_id" in source
    assert "affected_item_ids" in source
    assert "affected_claim_ids" in source
    assert "evidence_diff" in source
    assert "uq_candidate_curation_tenant_dedupe" in source
    assert "uq_source_records_tenant_id_id" in source_resource_migration
    assert '["tenant_id", "source_resource_id"]' in source
    assert '["tenant_id", "previous_source_record_id"]' in source
    assert '["tenant_id", "current_source_record_id"]' in source
    assert "legacy_source_drift" in source
    assert "artifact_kind = 'candidate_memory_reflection'" in source
