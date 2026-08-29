"""Unit tests for :mod:`app.services.governance`.

These tests exercise the governance bookkeeping helper without any FastAPI or
SQLAlchemy session. They construct minimal ``SimpleNamespace``-style rows so
the diff/audit/preview helpers can run in pure-Python land.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services.governance import (
    MAX_AUDIT_ENTRIES,
    GovernanceChange,
    _orm_attribute_for,
    apply_governance_update,
    diff_governance,
    log_governance_denial,
    previous_governance_state,
)


def _row(**overrides) -> SimpleNamespace:
    """Build a row that mirrors the public governance column surface.

    The ``SimpleNamespace`` is enough for ``previous_governance_state`` (which
    uses ``getattr``) and ``apply_governance_update`` (which writes through
    the same ORM attribute names).
    """
    base = {
        "id": uuid.uuid4(),
        "tenant_id": "tenant-a",
        "metadata_": {},
        "governance_owner_subject": None,
        "governance_reviewer_subject": None,
        "governance_verification_state": None,
        "governance_verified_at": None,
        "governance_verified_by_subject": None,
        "governance_verification_deadline": None,
        "governance_risk_class": None,
        "governance_supersession_reason": None,
        "governance_superseded_by_item_id": None,
        "governance_superseded_at": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_diff_governance_detects_only_changed_fields() -> None:
    previous = {
        "owner_subject": "alice",
        "verification_state": "verified",
        "risk_class": "high",
    }
    next_values = {
        "owner_subject": "alice",
        "verification_state": "verified",
        "risk_class": "critical",
    }

    changes = diff_governance(previous=previous, next_values=next_values)

    assert changes == [
        GovernanceChange(field="risk_class", previous="high", next="critical"),
    ]


def test_diff_governance_treats_missing_key_and_none_as_equivalent() -> None:
    missing_then_none = diff_governance(
        previous={"risk_class": None},
        next_values={"risk_class": None},
    )
    assert missing_then_none == []

    missing_then_value = diff_governance(
        previous={"risk_class": None},
        next_values={"risk_class": "high"},
    )
    assert missing_then_value == [
        GovernanceChange(field="risk_class", previous=None, next="high"),
    ]

    value_then_none = diff_governance(
        previous={"risk_class": "high"},
        next_values={"risk_class": None},
    )
    assert value_then_none == [
        GovernanceChange(field="risk_class", previous="high", next=None),
    ]


def test_diff_governance_rejects_unknown_field_names() -> None:
    with pytest.raises(ValueError, match="unknown governance field"):
        diff_governance(
            previous={"bogus_field": None},
            next_values={"bogus_field": "x"},
        )


def test_apply_governance_update_persists_columns_and_audit_entry() -> None:
    row = _row()
    deadline = datetime.now(timezone.utc) + timedelta(days=30)

    changes = apply_governance_update(
        row,
        governance={
            "owner_subject": "alice",
            "verification_state": "verified",
            "risk_class": "high",
            "verification_deadline": deadline,
        },
        actor_subject_id="api-key:abcdef",
    )

    assert [change.field for change in changes] == [
        "owner_subject",
        "verification_state",
        "risk_class",
        "verification_deadline",
    ]

    # The helper must write through to the column-mapped attribute, otherwise
    # a follow-up PATCH would treat the row as if nothing had been persisted
    # and the audit trail would silently desynchronize from the row.
    assert row.governance_owner_subject == "alice"
    assert row.governance_verification_state == "verified"
    assert row.governance_risk_class == "high"
    assert row.governance_verification_deadline == deadline

    snapshot = previous_governance_state(row)
    assert snapshot["owner_subject"] == "alice"
    assert snapshot["verification_state"] == "verified"
    assert snapshot["risk_class"] == "high"
    assert snapshot["verification_deadline"] == deadline

    history = row.metadata_["governance_audit"]
    assert isinstance(history, list)
    assert len(history) == 1
    entry = history[0]
    assert entry["actor_subject"] == "api-key:abcdef"
    assert "recorded_at" in entry and entry["recorded_at"].endswith("+00:00")

    change_rows = entry["changes"]
    assert {change["field"] for change in change_rows} == {
        "owner_subject",
        "verification_state",
        "risk_class",
        "verification_deadline",
    }
    by_field = {change["field"]: change for change in change_rows}
    assert by_field["owner_subject"]["previous"] is None
    assert by_field["owner_subject"]["next"] == "alice"
    assert by_field["verification_state"]["previous"] is None
    assert by_field["verification_state"]["next"] == "verified"
    assert by_field["risk_class"]["previous"] is None
    assert by_field["risk_class"]["next"] == "high"
    assert by_field["verification_deadline"]["previous"] is None
    assert by_field["verification_deadline"]["next"] == deadline.isoformat()


def test_apply_governance_update_is_idempotent_for_identical_payload() -> None:
    row = _row(governance_owner_subject="alice")

    first = apply_governance_update(
        row,
        governance={"owner_subject": "alice"},
        actor_subject_id="api-key:abcdef",
    )
    second = apply_governance_update(
        row,
        governance={"owner_subject": "alice"},
        actor_subject_id="api-key:abcdef",
    )

    assert first == []
    assert second == []
    # Idempotent calls do not append to the audit key (which is also absent
    # entirely if no change has ever been recorded on the row).
    assert row.metadata_.get("governance_audit", []) == []


def test_apply_governance_update_caps_history_at_twenty_five_entries() -> None:
    row = _row(governance_owner_subject="seed")

    # Thirty distinct payloads: each one flips ``risk_class`` so the diff sees
    # a transition. Without the cap the audit list would grow unbounded.
    for index in range(30):
        apply_governance_update(
            row,
            governance={"risk_class": f"low-{index}"},
            actor_subject_id="api-key:abcdef",
        )

    history = row.metadata_["governance_audit"]
    assert len(history) == MAX_AUDIT_ENTRIES == 25
    # First five entries should have been evicted; the oldest remaining
    # payload should be the 6th change (index 5).
    assert history[0]["changes"][0]["next"] == "low-5"
    assert history[-1]["changes"][0]["next"] == "low-29"


def test_apply_governance_update_logs_structured_event_with_field_names_and_state(
    caplog: pytest.LogCaptureFixture,
) -> None:
    row = _row()
    caplog.set_level(logging.INFO, logger="app.services.governance")

    apply_governance_update(
        row,
        governance={
            "owner_subject": "alice",
            "verification_state": "verified",
            "risk_class": "high",
        },
        actor_subject_id="api-key:abcdef",
    )

    update_records = [
        record for record in caplog.records
        if record.name == "app.services.governance" and record.getMessage() == "governance.item.update"
    ]
    assert len(update_records) == 1
    record = update_records[0]

    assert record.levelname == "INFO"
    assert record.item_id == str(row.id)
    assert record.tenant_id == "tenant-a"
    assert record.actor_subject == "api-key:abcdef"
    assert record.changed_fields == ["owner_subject", "verification_state", "risk_class"]
    assert record.verification_state == "verified"
    assert record.risk_class == "high"


def test_log_governance_denial_writes_warning_with_tenant_and_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    item_id = uuid.uuid4()
    caplog.set_level(logging.WARNING, logger="app.services.governance")

    log_governance_denial(
        tenant_id="tenant-a",
        item_id=item_id,
        actor_subject_id="api-key:abcdef",
        reason="tenant-mismatch",
    )

    denial_records = [
        record for record in caplog.records
        if record.name == "app.services.governance" and record.getMessage() == "governance.item.denied"
    ]
    assert len(denial_records) == 1
    record = denial_records[0]
    assert record.levelname == "WARNING"
    assert record.tenant_id == "tenant-a"
    assert record.item_id == str(item_id)
    assert record.actor_subject == "api-key:abcdef"
    assert record.reason == "tenant-mismatch"


def test_orm_attribute_map_covers_every_known_governance_field() -> None:
    """Adding a new governance column without extending the map would let a
    silent bug slip into ``apply_governance_update``. Pin the surface so the
    schema, the audit log, and the API wire format cannot drift."""
    expected = {
        "owner_subject",
        "reviewer_subject",
        "verification_state",
        "verified_at",
        "verified_by_subject",
        "verification_deadline",
        "risk_class",
        "supersession_reason",
        "superseded_by_item_id",
        "superseded_at",
    }
    assert set(_orm_attribute_for(field) for field in expected).issubset(
        {f"governance_{field}" for field in expected}
    )

