"""Migration 072 must carry the schema the canon service and RLS rely on.

House convention for this repository: migration tests assert against the
migration source, because a live Postgres is not available to the unit suite.
Each assertion below names the guarantee it protects, so a later edit that drops
one is caught by name rather than by a diff nobody reads.
"""

from pathlib import Path

_VERSIONS = Path(__file__).parents[1] / "alembic" / "versions"


def _migration_source() -> str:
    return (_VERSIONS / "072_tenant_canon_pages.py").read_text()


def test_migration_chains_from_071() -> None:
    source = _migration_source()
    assert 'revision = "072_tenant_canon_pages"' in source
    assert 'down_revision = "071_source_drift_artifacts"' in source


def test_migration_creates_the_three_tables() -> None:
    source = _migration_source()
    assert 'op.create_table(\n        "canon_pages"' in source
    assert '"canon_page_revisions"' in source
    assert '"canon_digest_cache"' in source
    assert 'op.drop_table("canon_digest_cache")' in source
    assert 'op.drop_table("canon_page_revisions")' in source
    assert 'op.drop_table("canon_pages")' in source


def test_one_published_charter_per_room() -> None:
    source = _migration_source()
    assert '"uq_canon_pages_tenant_room_charter"' in source
    assert "section = 'charter' AND status = 'published' AND room_id IS NOT NULL" in source
    assert "unique=True" in source


def test_check_constraints_cover_section_status_and_char_count() -> None:
    source = _migration_source()
    assert 'name="ck_canon_pages_section"' in source
    assert "'identity', 'rules', 'glossary', 'runbook', 'charter', 'faq', 'reference'" in source
    assert 'name="ck_canon_pages_status"' in source
    assert "'draft', 'published', 'archived'" in source
    assert 'name="ck_canon_pages_char_count"' in source
    assert "char_count >= 0 AND char_count <= 20000" in source


def test_foreign_keys_cascade_and_null_out() -> None:
    source = _migration_source()
    assert 'sa.ForeignKey("items.id", ondelete="CASCADE")' in source
    assert 'sa.ForeignKey("rooms.id", ondelete="SET NULL")' in source
    assert 'sa.ForeignKey("canon_pages.id", ondelete="CASCADE")' in source


def test_rls_follows_the_067_policy_body() -> None:
    source = _migration_source()
    assert "ENABLE ROW LEVEL SECURITY" in source
    assert "FORCE ROW LEVEL SECURITY" in source
    assert "CREATE POLICY tenant_isolation" in source
    assert "current_setting('app.system_access', true) = 'true'" in source
    assert "current_setting('app.tenant_id', true)" in source
    assert "tenant_erasure_states AS erasure" in source
    assert "SET LOCAL lock_timeout = '5s'" in source
    # All three new tables are covered, not just the first.
    assert 'CANON_TABLES = ("canon_pages", "canon_page_revisions", "canon_digest_cache")' in source


def test_canon_edit_artifact_kind_is_added_and_reverted() -> None:
    source = _migration_source()
    assert "'candidate_canon_edit'" in source
    # The five pre-existing kinds must survive the re-create, or 071 rows break.
    for kind in (
        "candidate_skill",
        "candidate_routing_manifest",
        "candidate_prompt_guardrail",
        "candidate_memory_reflection",
        "candidate_source_drift",
    ):
        assert kind in source
    assert source.count("op.drop_constraint(") >= 2, "downgrade must restore the narrower constraint"
    assert "legacy_canon_edit" in source
