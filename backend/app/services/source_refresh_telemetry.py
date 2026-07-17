"""Durable, bounded telemetry for authoritative HTTP source refreshes."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.source_resource import SourceResource, SourceResourceAuditSnapshot

_OUTCOMES = {"success", "not_modified", "failure", "gone"}
_VALIDATORS = {"etag", "last_modified", "none"}
_CHANGES = {"changed", "unchanged", "unknown"}


def _bounded(value: object, allowed: set[str], default: str) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    return normalized if normalized in allowed else default


def record_source_refresh(
    db: AsyncSession,
    *,
    resource: SourceResource,
    outcome: str,
    validator: str,
    change: str,
    refresh_duration_seconds: float,
    change_to_index_seconds: float | None = None,
) -> None:
    """Persist one committed refresh in the worker transaction.

    The backend's metrics endpoint reads these audit rows from PostgreSQL, so
    observations survive worker process boundaries and restarts. Resource and
    tenant identifiers stay out of the metric payload and labels.
    """

    labels = (
        _bounded(outcome, _OUTCOMES, "failure"),
        _bounded(validator, _VALIDATORS, "none"),
        _bounded(change, _CHANGES, "unknown"),
    )
    payload = {
        "outcome": labels[0],
        "validator": labels[1],
        "change": labels[2],
        "refresh_duration_seconds": max(float(refresh_duration_seconds), 0.0),
        "change_to_index_seconds": (
            max(float(change_to_index_seconds), 0.0) if change_to_index_seconds is not None and labels[2] == "changed" else None
        ),
    }
    db.add(
        SourceResourceAuditSnapshot(
            tenant_id=resource.tenant_id,
            resource_id=resource.id,
            event_kind="refresh_telemetry",
            previous_snapshot={},
            next_snapshot=payload,
            recorded_at=datetime.now(timezone.utc),
        )
    )
