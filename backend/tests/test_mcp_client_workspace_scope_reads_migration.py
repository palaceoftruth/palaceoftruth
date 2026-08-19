from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "069_mcp_client_workspace_scope_reads.py"
)


def test_mcp_client_workspace_scope_reads_migration_is_fail_closed_and_reversible() -> None:
    source = MIGRATION.read_text()

    # Chained onto the real head, not an older sibling that would fork the tree.
    assert 'down_revision = "068_curation_principals"' in source
    assert '"allow_workspace_scope_reads"' in source
    # Existing clients must not gain reach when the column appears.
    assert "server_default=sa.false()" in source
    # Reach is only meaningful for a client bound to a canonical agent scope.
    assert '"ck_mcp_clients_workspace_scope_read_binding"' in source
    assert "allow_workspace_scope_reads = false OR agent_scope_key IS NOT NULL" in source
    assert 'op.drop_column("mcp_clients", "allow_workspace_scope_reads")' in source
