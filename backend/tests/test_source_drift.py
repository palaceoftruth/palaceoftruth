import uuid
from datetime import datetime, timezone

import pytest

from app.models.item import Item
from app.models.palace import (
    CandidateCurationArtifact,
    CandidateCurationArtifactEvent,
    SourceChunk,
    SourceRecord,
)
from app.models.source_resource import SourceResource
from app.services.source_drift import (
    SourceDriftError,
    build_readable_source_diff,
    create_source_drift_proposal,
    source_drift_dedupe_key,
)
from app.services.source_resources import canonical_http_identity


class _ScalarRows:
    def __init__(self, rows) -> None:
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    def __init__(self, *, scalar_values, scalar_rows=()) -> None:
        self._scalar_values = iter(scalar_values)
        self._scalar_rows = iter(scalar_rows)
        self.added = []

    async def scalar(self, _statement):
        return next(self._scalar_values)

    async def scalars(self, _statement):
        return _ScalarRows(next(self._scalar_rows))

    def add(self, value) -> None:
        if getattr(value, "id", None) is None:
            value.id = uuid.uuid4()
        self.added.append(value)

    async def flush(self) -> None:
        now = datetime.now(timezone.utc)
        for value in self.added:
            if getattr(value, "created_at", None) is None:
                value.created_at = now
            if hasattr(value, "updated_at") and getattr(value, "updated_at", None) is None:
                value.updated_at = now


def _resource(previous_record_id: uuid.UUID) -> SourceResource:
    url = "https://example.test/policy"
    return SourceResource(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        kind="http",
        source_class="webpage",
        canonical_url=url,
        canonical_identity=canonical_http_identity(url),
        refresh_policy="interval",
        refresh_slo_seconds=3600,
        status="active",
        consecutive_failures=0,
        last_successful_source_record_id=previous_record_id,
    )


def _record(
    *,
    record_id: uuid.UUID,
    item_id: uuid.UUID,
    version: str,
    content_hash: str,
    status: str,
) -> SourceRecord:
    return SourceRecord(
        id=record_id,
        tenant_id="tenant-a",
        item_id=item_id,
        source_kind="webpage",
        source_uri="https://example.test/policy",
        source_version=version,
        content_hash=content_hash,
        status=status,
        metadata_={},
    )


def _chunk(record: SourceRecord, text: str) -> SourceChunk:
    return SourceChunk(
        id=uuid.uuid4(),
        tenant_id=record.tenant_id,
        source_record_id=record.id,
        item_id=record.item_id,
        chunk_index=0,
        chunk_text=text,
        chunk_digest=f"digest:{record.source_version}",
        span={},
    )


def test_readable_diff_is_deterministic_and_redacts_secret_assignments() -> None:
    item_id = uuid.uuid4()
    old = _record(record_id=uuid.uuid4(), item_id=item_id, version="v1", content_hash="old", status="stale")
    new = _record(record_id=uuid.uuid4(), item_id=item_id, version="v2", content_hash="new", status="active")

    diff, truncated = build_readable_source_diff(
        [_chunk(old, "Limit: 10\npassword=old-secret")],
        [_chunk(new, "Limit: 20\npassword=new-secret")],
    )

    assert "-Limit: 10" in diff
    assert "+Limit: 20" in diff
    assert "old-secret" not in diff
    assert "new-secret" not in diff
    assert truncated is False


@pytest.mark.asyncio
async def test_changed_source_creates_one_tenant_scoped_evidence_proposal() -> None:
    item_id = uuid.uuid4()
    claim_id = uuid.uuid4()
    previous = _record(record_id=uuid.uuid4(), item_id=item_id, version="v1", content_hash="hash-1", status="stale")
    current = _record(record_id=uuid.uuid4(), item_id=item_id, version="v2", content_hash="hash-2", status="active")
    resource = _resource(previous.id)
    item = Item(
        id=item_id,
        tenant_id="tenant-a",
        source_type="webpage",
        source_url=resource.canonical_url,
        title="Policy",
        status="ready",
        metadata_={},
        tags=[],
        categories=[],
        governance_owner_subject="owner-a",
    )
    session = _Session(
        scalar_values=(None, resource, previous, current),
        scalar_rows=(
            [item],
            [_chunk(previous, "Retention: 30 days")],
            [_chunk(current, "Retention: 90 days")],
            [claim_id],
        ),
    )

    result = await create_source_drift_proposal(
        session,  # type: ignore[arg-type]
        tenant_id="tenant-a",
        resource_id=resource.id,
        previous_source_record_id=previous.id,
        current_source_record_id=current.id,
    )

    assert result.outcome == "created"
    artifact = result.artifact
    assert artifact is not None
    assert artifact.artifact_kind == "candidate_source_drift"
    assert artifact.status == "reviewable"
    assert artifact.previous_source_record_id == previous.id
    assert artifact.current_source_record_id == current.id
    assert artifact.affected_item_ids == [str(item_id)]
    assert artifact.affected_claim_ids == [str(claim_id)]
    assert "-Retention: 30 days" in artifact.evidence_diff["diff"]
    assert "+Retention: 90 days" in artifact.evidence_diff["diff"]
    assert artifact.metadata_["owner_routes"] == ["owner-a"]
    assert artifact.dedupe_key == source_drift_dedupe_key(resource.id, previous.id, current.id)
    assert len([value for value in session.added if isinstance(value, CandidateCurationArtifact)]) == 1
    event = next(value for value in session.added if isinstance(value, CandidateCurationArtifactEvent))
    assert event.event_type == "source_drift_created"
    assert event.next_snapshot["previous_source_record_id"] == str(previous.id)


@pytest.mark.asyncio
async def test_source_drift_retry_returns_existing_artifact_without_duplicate_write() -> None:
    artifact = CandidateCurationArtifact(id=uuid.uuid4(), tenant_id="tenant-a")
    session = _Session(scalar_values=(artifact,))

    result = await create_source_drift_proposal(
        session,  # type: ignore[arg-type]
        tenant_id="tenant-a",
        resource_id=uuid.uuid4(),
        previous_source_record_id=uuid.uuid4(),
        current_source_record_id=uuid.uuid4(),
    )

    assert result == type(result)(artifact=artifact, outcome="deduplicated")
    assert session.added == []


@pytest.mark.asyncio
async def test_same_content_hash_creates_no_review_item() -> None:
    item_id = uuid.uuid4()
    previous = _record(record_id=uuid.uuid4(), item_id=item_id, version="v1", content_hash="same", status="stale")
    current = _record(record_id=uuid.uuid4(), item_id=item_id, version="v2", content_hash="same", status="active")
    resource = _resource(previous.id)
    session = _Session(scalar_values=(None, resource, previous, current))

    result = await create_source_drift_proposal(
        session,  # type: ignore[arg-type]
        tenant_id="tenant-a",
        resource_id=resource.id,
        previous_source_record_id=previous.id,
        current_source_record_id=current.id,
    )

    assert result.artifact is None
    assert result.outcome == "unchanged"
    assert session.added == []


@pytest.mark.asyncio
async def test_source_drift_fails_closed_without_tenant_scope() -> None:
    session = _Session(scalar_values=())

    with pytest.raises(SourceDriftError, match="tenant scope"):
        await create_source_drift_proposal(
            session,  # type: ignore[arg-type]
            tenant_id="",
            resource_id=uuid.uuid4(),
            previous_source_record_id=uuid.uuid4(),
            current_source_record_id=uuid.uuid4(),
        )


@pytest.mark.asyncio
async def test_source_drift_hides_cross_tenant_resource_as_missing() -> None:
    session = _Session(scalar_values=(None, None))

    with pytest.raises(SourceDriftError, match="outside tenant scope"):
        await create_source_drift_proposal(
            session,  # type: ignore[arg-type]
            tenant_id="tenant-a",
            resource_id=uuid.uuid4(),
            previous_source_record_id=uuid.uuid4(),
            current_source_record_id=uuid.uuid4(),
        )


@pytest.mark.asyncio
async def test_source_drift_rejects_uri_mismatch_before_reading_source_text() -> None:
    item_id = uuid.uuid4()
    previous = _record(record_id=uuid.uuid4(), item_id=item_id, version="v1", content_hash="old", status="stale")
    current = _record(record_id=uuid.uuid4(), item_id=item_id, version="v2", content_hash="new", status="active")
    current.source_uri = "https://other.example.test/policy"
    resource = _resource(previous.id)
    session = _Session(scalar_values=(None, resource, previous, current))

    with pytest.raises(SourceDriftError, match="does not match"):
        await create_source_drift_proposal(
            session,  # type: ignore[arg-type]
            tenant_id="tenant-a",
            resource_id=resource.id,
            previous_source_record_id=previous.id,
            current_source_record_id=current.id,
        )


def test_readable_diff_redacts_private_markers_and_caps_large_evidence() -> None:
    item_id = uuid.uuid4()
    old = _record(record_id=uuid.uuid4(), item_id=item_id, version="v1", content_hash="old", status="stale")
    new = _record(record_id=uuid.uuid4(), item_id=item_id, version="v2", content_hash="new", status="active")

    diff, truncated = build_readable_source_diff(
        [_chunk(old, "-----BEGIN PRIVATE KEY-----\n" + "A" * 30_000)],
        [_chunk(new, "-----BEGIN PRIVATE KEY-----\n" + "B" * 30_000)],
    )

    assert "PRIVATE KEY" not in diff
    assert len(diff) <= 24_050
    assert diff.endswith("... diff truncated for review safety")
    assert truncated is True
