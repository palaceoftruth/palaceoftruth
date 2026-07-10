from pathlib import Path


MIGRATION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "042_source_resource_refresh_leases.py"


def test_refresh_lease_migration_is_reversible_and_indexed() -> None:
    contents = MIGRATION.read_text()

    assert 'revision = "042_source_resource_refresh_leases"' in contents
    assert 'down_revision = "041_source_resources"' in contents
    assert "refresh_lease_token" in contents
    assert "refresh_lease_expires_at" in contents
    assert "ix_source_resources_due_lease" in contents
    assert "op.drop_index" in contents
    assert "op.drop_column" in contents
