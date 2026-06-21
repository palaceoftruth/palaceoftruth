from pathlib import Path


MIGRATION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "035_source_records_and_chunks.py"
CLAIMS_MIGRATION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "036_claims_and_claim_sources.py"


def test_source_compiler_migration_declares_required_constraints() -> None:
    contents = MIGRATION.read_text()

    assert "source_records" in contents
    assert "source_chunks" in contents
    assert "uq_source_records_tenant_item_version" in contents
    assert "uq_source_chunks_tenant_record_index" in contents
    assert "uq_source_chunks_tenant_record_digest" in contents
    assert "ck_source_records_status" in contents
    assert "ondelete=\"CASCADE\"" in contents


def test_claim_compiler_migration_declares_required_constraints() -> None:
    contents = CLAIMS_MIGRATION.read_text()

    assert "claims" in contents
    assert "claim_sources" in contents
    assert "uq_claims_tenant_claim_key" in contents
    assert "uq_claim_sources_support" in contents
    assert "ck_claims_status" in contents
    assert "ck_claim_sources_support_role" in contents
    assert "ck_claim_sources_status" in contents
    assert "ix_claim_sources_tenant_source_record" in contents
    assert "ondelete=\"CASCADE\"" in contents
