from pathlib import Path


MIGRATION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "041_source_resources.py"


def test_source_resources_migration_contract_and_safe_downgrade() -> None:
    contents = MIGRATION.read_text()

    assert 'revision = "041_source_resources"' in contents
    assert 'down_revision = "040_semantic_memory_reflection"' in contents
    assert "uq_source_resources_tenant_kind_identity" in contents
    assert "source_resource_aliases" in contents
    assert "submitted', 'final', 'canonical" in contents
    assert "accepted', 'rejected', 'conflict" in contents
    assert "current_source_record_id" in contents
    assert "last_successful_source_record_id" in contents
    assert "uq_source_records_tenant_id_id" in contents
    assert "fk_source_resources_current_record_tenant" in contents
    assert "fk_source_resource_aliases_resource_tenant" in contents
    assert "last_verified_at" in contents
    assert "robots_cached_at" in contents
    assert "canonical_signal_url" in contents
    assert "provenance" in contents
    assert "previous_snapshot" in contents
    assert "next_snapshot" in contents
    assert 'ondelete="RESTRICT"' in contents
    assert "source_resource_audit_append_only" in contents
    assert "BEFORE UPDATE OR DELETE" in contents
    assert "DROP TRIGGER IF EXISTS" in contents
    assert 'op.drop_table("source_resources")' in contents
