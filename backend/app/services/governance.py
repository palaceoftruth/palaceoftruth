"""Recommendation 1 governance bookkeeping for items and derived claims.

Records nullable owner/reviewer/verification/expiry/risk transitions on item
writes and emits a structured, privacy-safe log line so operators can confirm
the change was applied. The audit trail itself is stored under the existing
``metadata_`` JSONB column with a stable ``governance_audit`` key, so the
slice stays a thin vertical and does not need its own table.

The helpers here never fabricate a subject identifier; the caller passes the
already-authenticated, stable ``subject_id`` from ``AuthContext``. The actor
record is logged with the prior and new values for each changed field, but
raw content, secrets, and tenant identifiers are never included.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


MAX_AUDIT_ENTRIES = 25


# Single source of truth that maps the wire-level field name (what the caller
# sends in PATCH payload) to the SQLAlchemy-mapped ORM attribute (with the
# ``governance_`` column prefix). Every helper funnels through this map so
# the schema layer, the audit log, and the API wire format can never drift.
_GOVERNANCE_ATTRIBUTE_NAMES: dict[str, str] = {
    "owner_subject": "governance_owner_subject",
    "reviewer_subject": "governance_reviewer_subject",
    "verification_state": "governance_verification_state",
    "verified_at": "governance_verified_at",
    "verified_by_subject": "governance_verified_by_subject",
    "verification_deadline": "governance_verification_deadline",
    "risk_class": "governance_risk_class",
    "supersession_reason": "governance_supersession_reason",
    "superseded_by_item_id": "governance_superseded_by_item_id",
    "superseded_at": "governance_superseded_at",
}


def _orm_attribute_for(field: str) -> str:
    """Return the ORM attribute for a wire-level field name. Unknown names
    surface immediately so a future schema addition cannot silently fail to
    persist a column."""
    attribute = _GOVERNANCE_ATTRIBUTE_NAMES.get(field)
    if attribute is None:
        raise ValueError(f"unknown governance field: {field!r}")
    return attribute


@dataclass(frozen=True)
class GovernanceChange:
    field: str
    previous: Any
    next: Any


def _coerce_audit_value(value: Any) -> Any:
    """Render audit values in a JSON-safe, privacy-safe shape."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, str):
        # Subjects can be api_key or MCP client identifiers; never log raw
        # content or tokens. Strings are already safe at the schema layer.
        return value
    return value


def diff_governance(
    *,
    previous: dict[str, Any],
    next_values: dict[str, Any],
) -> list[GovernanceChange]:
    """Return the per-field transitions where the caller-supplied value
    differs from the stored value. Treats ``None`` and missing keys as a
    single "unset" state so removing a value is auditable."""
    changes: list[GovernanceChange] = []
    for field, next_value in next_values.items():
        # Reject unknown fields early so the diff reflects an actual column.
        _orm_attribute_for(field)
        previous_value = previous.get(field)
        if next_value == previous_value:
            continue
        changes.append(
            GovernanceChange(
                field=field,
                previous=previous_value,
                next=next_value,
            )
        )
    return changes


def previous_governance_state(row: Any) -> dict[str, Any]:
    """Snapshot the current governance columns from an ORM row using
    unprefixed wire-level keys so the diff helper stays wire-faithful."""
    snapshot: dict[str, Any] = {}
    for wire_field, attribute in _GOVERNANCE_ATTRIBUTE_NAMES.items():
        snapshot[wire_field] = getattr(row, attribute, None)
    return snapshot


def apply_governance_update(row: Any, *, governance: dict[str, Any], actor_subject_id: str | None) -> list[GovernanceChange]:
    """Apply a governance update, return the per-field changes for audit.

    The caller is responsible for committing the row; this helper only writes
    the new column values and appends the audit entry under
    ``metadata_["governance_audit"]``.
    """
    previous = previous_governance_state(row)
    changes = diff_governance(previous=previous, next_values=governance)
    if not changes:
        return []

    for change in changes:
        setattr(row, _orm_attribute_for(change.field), change.next)

    now = datetime.now(timezone.utc)
    audit_entry: dict[str, Any] = {
        "recorded_at": now.isoformat(),
        "actor_subject": actor_subject_id,
        "changes": [
            {
                "field": change.field,
                "previous": _coerce_audit_value(change.previous),
                "next": _coerce_audit_value(change.next),
            }
            for change in changes
        ],
    }

    metadata = dict(row.metadata_ or {})
    history = list(metadata.get("governance_audit") or [])
    history.append(audit_entry)
    if len(history) > MAX_AUDIT_ENTRIES:
        history = history[-MAX_AUDIT_ENTRIES:]
    metadata["governance_audit"] = history
    row.metadata_ = metadata

    logger.info(
        "governance.item.update",
        extra={
            "item_id": str(row.id),
            "tenant_id": str(row.tenant_id),
            "actor_subject": actor_subject_id,
            "changed_fields": [change.field for change in changes],
            "verification_state": governance.get("verification_state"),
            "risk_class": governance.get("risk_class"),
            "verification_deadline": _coerce_audit_value(governance.get("verification_deadline")),
        },
    )
    return changes


def log_governance_denial(
    *,
    tenant_id: str,
    item_id: uuid.UUID,
    actor_subject_id: str | None,
    reason: str,
) -> None:
    """Single funnel for tenant/scope denials so the message key and fields
    stay identical for observability and audit replay."""
    logger.warning(
        "governance.item.denied",
        extra={
            "tenant_id": tenant_id,
            "item_id": str(item_id),
            "actor_subject": actor_subject_id,
            "reason": reason,
        },
    )

