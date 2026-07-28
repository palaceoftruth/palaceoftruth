from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "053_mcp_client_tenant_shared_reads.py"
)


def test_mcp_client_tenant_shared_reads_migration_is_fail_closed_and_reversible() -> None:
    source = MIGRATION.read_text()

    assert 'down_revision = "052_source_resource_source_class"' in source
    assert '"allow_tenant_shared_reads"' in source
    assert "server_default=sa.false()" in source
    assert '"ck_mcp_clients_tenant_shared_read_binding"' in source
    assert 'op.drop_column("mcp_clients", "allow_tenant_shared_reads")' in source
