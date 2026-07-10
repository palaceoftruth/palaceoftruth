import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.source_resource import SourceResource
from app.services.source_resources import (
    RefreshObservation,
    apply_refresh_observation,
    build_alias,
    canonical_http_identity,
    compute_freshness,
    decide_alias,
    normalize_http_url,
)


NOW = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)


def make_resource(**overrides) -> SourceResource:
    values = {
        "id": uuid.uuid4(),
        "tenant_id": "tenant-a",
        "kind": "http",
        "canonical_url": "https://example.com/story?a=1&b=2",
        "canonical_identity": canonical_http_identity("https://example.com/story?a=1&b=2"),
        "refresh_policy": "interval",
        "refresh_slo_seconds": 3600,
        "status": "active",
        "consecutive_failures": 0,
        "robots_allowed": None,
    }
    values.update(overrides)
    return SourceResource(**values)


def test_http_normalization_is_conservative_and_stable() -> None:
    assert normalize_http_url("HTTPS://Example.COM:443") == "https://example.com/"
    assert normalize_http_url("https://example.com/a?b=2&a=1#section") == "https://example.com/a?b=2&a=1"
    assert canonical_http_identity("HTTPS://Example.COM:443") == canonical_http_identity("https://example.com/")

    with pytest.raises(ValueError, match="credentials"):
        normalize_http_url("https://user:password@example.com/")
    with pytest.raises(ValueError, match="scheme"):
        normalize_http_url("file:///tmp/story")


def test_alias_decisions_retain_signal_provenance_and_reject_conflicts() -> None:
    accepted = decide_alias(
        canonical_url="https://example.com/story",
        observed_url="https://EXAMPLE.com:443/story?view=full",
        signal="final",
    )
    assert (accepted.decision, accepted.reason) == ("accepted", "same_origin_signal")

    cross_origin = decide_alias(
        canonical_url="https://example.com/story",
        observed_url="https://cdn.example.com/story",
        signal="final",
    )
    assert (cross_origin.decision, cross_origin.reason) == ("conflict", "cross_origin_signal")

    conflicting_canonical = decide_alias(
        canonical_url="https://example.com/story",
        observed_url="https://example.com/other",
        signal="canonical",
        accepted_canonical_urls=["https://example.com/story"],
    )
    assert conflicting_canonical.decision == "conflict"
    assert conflicting_canonical.reason == "conflicting_canonical_signal"


def test_alias_builder_enforces_tenant_isolation() -> None:
    resource = make_resource()
    alias = build_alias(
        resource=resource,
        tenant_id="tenant-a",
        observed_url="https://example.com/final",
        signal="final",
    )
    assert alias.tenant_id == "tenant-a"
    assert alias.signal == "final"
    assert alias.decision == "accepted"
    assert alias.resource is resource

    with pytest.raises(ValueError, match="does not belong"):
        build_alias(
            resource=resource,
            tenant_id="tenant-b",
            observed_url="https://example.com/final",
            signal="final",
        )


@pytest.mark.parametrize(
    ("resource", "at", "expected"),
    [
        (make_resource(last_success_at=None, next_due_at=None), NOW, "unknown"),
        (make_resource(last_success_at=NOW, next_due_at=NOW + timedelta(hours=1)), NOW, "current"),
        (make_resource(last_success_at=NOW, next_due_at=NOW), NOW, "due"),
        (make_resource(last_success_at=NOW, next_due_at=NOW, refresh_slo_seconds=60), NOW + timedelta(seconds=61), "stale"),
        (make_resource(status="unreachable", last_success_at=NOW), NOW, "unreachable"),
        (make_resource(status="gone", last_success_at=NOW), NOW, "gone"),
    ],
)
def test_freshness_states(resource: SourceResource, at: datetime, expected: str) -> None:
    assert compute_freshness(resource, now=at) == expected


def test_success_and_failure_transitions_preserve_last_successful_version() -> None:
    resource = make_resource()
    record_id = uuid.uuid4()
    success_audit = apply_refresh_observation(
        resource,
        RefreshObservation(
            outcome="success",
            http_status=200,
            source_record_id=record_id,
            content_digest="sha256:first",
            validator_etag='"v1"',
            robots_allowed=True,
            published_at=NOW - timedelta(days=2),
            captured_at=NOW,
        ),
        checked_at=NOW,
    )

    assert resource.current_source_record_id == record_id
    assert resource.last_successful_source_record_id == record_id
    assert resource.published_at == NOW - timedelta(days=2)
    assert resource.captured_at == NOW
    assert resource.last_verified_at == NOW
    assert resource.content_changed_at == NOW
    assert resource.last_success_at == NOW
    assert success_audit.next_snapshot["current_source_record_id"] == str(record_id)

    failed_at = NOW + timedelta(hours=1)
    failure_audit = apply_refresh_observation(
        resource,
        RefreshObservation(
            outcome="failure",
            http_status=503,
            failure_reason="upstream_unavailable",
            robots_allowed=True,
        ),
        checked_at=failed_at,
    )

    assert resource.status == "unreachable"
    assert resource.current_source_record_id == record_id
    assert resource.last_successful_source_record_id == record_id
    assert resource.last_success_at == NOW
    assert resource.consecutive_failures == 1
    assert resource.backoff_until == failed_at + timedelta(seconds=60)
    assert failure_audit.next_snapshot["last_successful_source_record_id"] == str(record_id)
    # Earlier audit payloads are value snapshots, not mutable views of the resource.
    assert success_audit.next_snapshot["status"] == "active"
    assert success_audit.next_snapshot["last_failure_reason"] is None
    assert failure_audit.previous_snapshot["status"] == "active"


def test_not_modified_verifies_without_collapsing_temporal_meanings() -> None:
    record_id = uuid.uuid4()
    resource = make_resource(
        content_digest="sha256:first",
        current_source_record_id=record_id,
        last_successful_source_record_id=record_id,
        published_at=NOW - timedelta(days=5),
        captured_at=NOW - timedelta(days=1),
        content_changed_at=NOW - timedelta(days=1),
        last_success_at=NOW - timedelta(hours=2),
    )
    checked_at = NOW + timedelta(hours=1)
    apply_refresh_observation(
        resource,
        RefreshObservation(outcome="not_modified", http_status=304, validator_etag='"v1"'),
        checked_at=checked_at,
    )

    assert resource.published_at == NOW - timedelta(days=5)
    assert resource.captured_at == NOW - timedelta(days=1)
    assert resource.content_changed_at == NOW - timedelta(days=1)
    assert resource.last_verified_at == checked_at
    assert resource.last_success_at == checked_at
    assert resource.current_source_record_id == record_id


def test_changed_content_requires_matching_append_only_version() -> None:
    resource = make_resource(content_digest="sha256:first")
    with pytest.raises(ValueError, match="requires a source record"):
        apply_refresh_observation(
            resource,
            RefreshObservation(outcome="success", http_status=200, content_digest="sha256:changed"),
            checked_at=NOW,
        )
    with pytest.raises(ValueError, match="requires a content digest"):
        apply_refresh_observation(
            resource,
            RefreshObservation(outcome="success", http_status=200, source_record_id=uuid.uuid4()),
            checked_at=NOW,
        )

    same_record = uuid.uuid4()
    resource.current_source_record_id = same_record
    resource.last_successful_source_record_id = same_record
    unchanged = apply_refresh_observation(
        resource,
        RefreshObservation(
            outcome="success", http_status=200, source_record_id=same_record, content_digest="sha256:first"
        ),
        checked_at=NOW,
    )
    assert resource.content_changed_at is None
    assert unchanged.next_snapshot["content_changed_at"] is None


def test_transition_validation_rejects_ambiguous_outcomes() -> None:
    with pytest.raises(ValueError, match="requires failure_reason"):
        apply_refresh_observation(make_resource(), RefreshObservation(outcome="failure"), checked_at=NOW)
    with pytest.raises(ValueError, match="cannot create"):
        apply_refresh_observation(
            make_resource(),
            RefreshObservation(outcome="not_modified", source_record_id=uuid.uuid4()),
            checked_at=NOW,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        apply_refresh_observation(
            make_resource(), RefreshObservation(outcome="not_modified"), checked_at=NOW.replace(tzinfo=None)
        )


def test_model_identity_constraint_is_tenant_and_kind_scoped() -> None:
    constraint_names = {constraint.name for constraint in SourceResource.__table__.constraints}
    assert "uq_source_resources_tenant_kind_identity" in constraint_names
    unique = next(
        constraint
        for constraint in SourceResource.__table__.constraints
        if constraint.name == "uq_source_resources_tenant_kind_identity"
    )
    assert [column.name for column in unique.columns] == ["tenant_id", "kind", "canonical_identity"]


def test_model_uses_tenant_qualified_foreign_keys() -> None:
    constraint_names = {constraint.name for constraint in SourceResource.__table__.constraints}
    assert "fk_source_resources_current_record_tenant" in constraint_names
    assert "fk_source_resources_last_success_record_tenant" in constraint_names
