from pathlib import Path


MIGRATION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "035_source_records_and_chunks.py"


def test_source_compiler_migration_declares_required_constraints() -> None:
    contents = MIGRATION.read_text()

    assert "source_records" in contents
    assert "source_chunks" in contents
    assert "uq_source_records_tenant_item_version" in contents
    assert "uq_source_chunks_tenant_record_index" in contents
    assert "uq_source_chunks_tenant_record_digest" in contents
    assert "ck_source_records_status" in contents
    assert "ondelete=\"CASCADE\"" in contents
