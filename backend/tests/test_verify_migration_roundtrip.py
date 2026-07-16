import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify_migration_roundtrip.py"
SPEC = importlib.util.spec_from_file_location("verify_migration_roundtrip", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_migration_harness_requires_an_explicit_disposable_database() -> None:
    assert (
        MODULE.validate_disposable_database_url(
            "postgresql://palace:palace@localhost/palace_migration_gate", allow_destructive=True
        )
        == "postgresql://palace:palace@localhost/palace_migration_gate"
    )


def test_migration_harness_rejects_non_disposable_database_names() -> None:
    try:
        MODULE.validate_disposable_database_url(
            "postgresql://palace:palace@localhost/palaceoftruth", allow_destructive=True
        )
    except ValueError as error:
        assert "palace_migration_" in str(error)
    else:
        raise AssertionError("expected non-disposable database to be rejected")


def test_migration_harness_rejects_remote_or_unacknowledged_targets() -> None:
    for database_url, allow_destructive, expected in (
        ("postgresql://palace:palace@db.example.test/palace_migration_gate", True, "loopback"),
        ("postgresql://palace:palace@localhost/palace_migration_gate", False, "allow-destructive"),
    ):
        try:
            MODULE.validate_disposable_database_url(database_url, allow_destructive=allow_destructive)
        except ValueError as error:
            assert expected in str(error)
        else:
            raise AssertionError("expected unsafe migration target to be rejected")


def test_migration_harness_requires_all_source_resource_tenant_foreign_keys() -> None:
    assert MODULE.TENANT_QUALIFIED_FOREIGN_KEYS == {
        "fk_source_resources_current_record_tenant",
        "fk_source_resources_last_success_record_tenant",
        "fk_source_resource_aliases_resource_tenant",
        "fk_source_resource_audit_resource_tenant",
    }
