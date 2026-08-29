"""End-to-end tests for the ``PATCH /items/{id}`` governance surface.

Reuses the fake ``Session`` / ``ArqPool`` scaffolding from
``tests/test_items_api.py`` so each test runs entirely in-process against a
FastAPI ``TestClient``. The PATCH handler delegates to
``app.services.governance.apply_governance_update``; the tests assert both
the HTTP contract and the side effects on the row.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.items import router
from app.auth import AuthContext, verify_memory_auth
from app.database import get_db
from app.mcp_scopes import LEGACY_API_KEY_SCOPES
from app.models.item import Item


class _FakeResult:
    def __init__(self, rows) -> None:
        self._rows = rows

    def fetchall(self):
        return self._rows


class FakeSession:
    def __init__(self, item) -> None:
        self.item = item
        self.execute_calls: list[str] = []
        self.commits = 0

    async def get(self, model, key):
        if model is Item and key == self.item.id:
            return self.item
        return None

    async def execute(self, statement, params=None):
        self.execute_calls.append(str(statement))
        return _FakeResult([])

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, value) -> None:
        assert value is self.item


class FakeArqPool:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict]] = []

    async def enqueue_job(self, name: str, **kwargs) -> None:
        self.enqueued.append((name, kwargs))


def _client(session: FakeSession, *, arq_pool: FakeArqPool | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.arq_pool = arq_pool or FakeArqPool()

    async def override_get_db():
        yield session

    async def override_verify(request: Request):
        request.state.auth_context = AuthContext(
            tenant_id="tenant-a",
            auth_mode="api_key",
            subject_id="api-key:abcdef",
            token_hash_reference="key-hash",
            scopes=LEGACY_API_KEY_SCOPES,
            capabilities=frozenset(LEGACY_API_KEY_SCOPES),
        )
        request.state.tenant_id = "tenant-a"
        request.state.key_hash = "key-hash"
        request.state.auth_mode = "api_key"
        return "raw-key"

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[verify_memory_auth] = override_verify
    return TestClient(app)


def _item(**overrides) -> Item:
    base = dict(
        id=uuid.uuid4(),
        source_type="note",
        title="Origin",
        tenant_id="tenant-a",
        status="ready",
        raw_content=None,
        summary=None,
        content_chunks=None,
        content_hash=None,
        metadata_={},
        tags=[],
        categories=[],
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        effective_date=None,
        effective_date_source=None,
        effective_date_quality=None,
        governance_owner_subject=None,
        governance_reviewer_subject=None,
        governance_verification_state=None,
        governance_verified_at=None,
        governance_verified_by_subject=None,
        governance_verification_deadline=None,
        governance_risk_class=None,
        governance_supersession_reason=None,
        governance_superseded_by_item_id=None,
        governance_superseded_at=None,
    )
    base.update(overrides)
    return Item(**base)


def test_patch_governance_updates_row_and_returns_governance_block() -> None:
    item = _item()
    session = FakeSession(item)
    client = _client(session)
    deadline = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()

    response = client.patch(
        f"/api/v1/items/{item.id}",
        json={
            "governance": {
                "owner_subject": "alice",
                "verification_state": "verified",
                "risk_class": "high",
                "verification_deadline": deadline,
            }
        },
    )

    assert response.status_code == 200
    payload = response.json()

    # Audit trail is appended correctly.
    assert "governance_audit" in item.metadata_
    assert len(item.metadata_["governance_audit"]) == 1
    audit_entry = item.metadata_["governance_audit"][0]
    assert audit_entry["actor_subject"] == "api-key:abcdef"
    changed_fields = {change["field"] for change in audit_entry["changes"]}
    assert changed_fields == {
        "owner_subject",
        "verification_state",
        "risk_class",
        "verification_deadline",
    }

    # ORM columns reflect the new values so a subsequent PATCH treats them
    # as the prior state for the diff.
    assert item.governance_owner_subject == "alice"
    assert item.governance_verification_state == "verified"
    assert item.governance_risk_class == "high"
    assert item.governance_verification_deadline is not None

    # Response includes a ``governance`` block (Pydantic nests the row's
    # ``governance_*`` columns there) and the new values round-trip back.
    assert "governance" in payload
    governance_block = payload["governance"]
    assert set(governance_block) == {
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
    assert governance_block["owner_subject"] == "alice"
    assert governance_block["verification_state"] == "verified"
    assert governance_block["risk_class"] == "high"


def test_patch_governance_emits_structured_audit_log(caplog) -> None:
    item = _item()
    session = FakeSession(item)
    client = _client(session)
    caplog.set_level(logging.INFO, logger="app.services.governance")

    response = client.patch(
        f"/api/v1/items/{item.id}",
        json={
            "governance": {
                "owner_subject": "alice",
                "verification_state": "verified",
                "risk_class": "high",
            }
        },
    )

    assert response.status_code == 200

    update_records = [
        record for record in caplog.records
        if record.name == "app.services.governance"
        and record.getMessage() == "governance.item.update"
    ]
    assert len(update_records) == 1
    record = update_records[0]
    assert record.levelname == "INFO"
    assert record.item_id == str(item.id)
    assert record.tenant_id == "tenant-a"
    assert record.actor_subject == "api-key:abcdef"
    assert set(record.changed_fields) == {
        "owner_subject",
        "verification_state",
        "risk_class",
    }
    assert record.verification_state == "verified"
    assert record.risk_class == "high"


def test_patch_governance_validation_rejects_invalid_risk_class() -> None:
    item = _item()
    session = FakeSession(item)
    client = _client(session)

    response = client.patch(
        f"/api/v1/items/{item.id}",
        json={"governance": {"risk_class": "extreme"}},
    )

    assert response.status_code == 422
    body = response.json()
    location = body["detail"][0]["loc"]
    # FastAPI nested-validation errors expose the bad field under
    # ``body.governance.<field>``.
    assert location[-1] == "risk_class"
    # The PATCH handler must NOT have mutated the row on a validation error.
    assert "governance_audit" not in item.metadata_


def test_patch_governance_validation_rejects_invalid_verification_state() -> None:
    item = _item()
    session = FakeSession(item)
    client = _client(session)

    response = client.patch(
        f"/api/v1/items/{item.id}",
        json={"governance": {"verification_state": "lol"}},
    )

    assert response.status_code == 422
    body = response.json()
    location = body["detail"][0]["loc"]
    assert location[-1] == "verification_state"
    assert "governance_audit" not in item.metadata_


def test_patch_governance_validation_rejects_oversized_owner_subject() -> None:
    item = _item()
    session = FakeSession(item)
    client = _client(session)

    response = client.patch(
        f"/api/v1/items/{item.id}",
        json={"governance": {"owner_subject": "a" * 201}},
    )

    assert response.status_code == 422
    body = response.json()
    location = body["detail"][0]["loc"]
    assert location[-1] == "owner_subject"
    assert "governance_audit" not in item.metadata_


def test_patch_governance_cannot_be_set_across_tenants() -> None:
    item = _item(tenant_id="tenant-b")
    session = FakeSession(item)
    client = _client(session)

    response = client.patch(
        f"/api/v1/items/{item.id}",
        json={"governance": {"owner_subject": "alice"}},
    )

    assert response.status_code == 404
    assert "governance_audit" not in item.metadata_
    assert getattr(item, "owner_subject", None) is None


def test_patch_without_governance_does_not_audit_or_log_governance(caplog) -> None:
    item = _item()
    session = FakeSession(item)
    client = _client(session)
    caplog.set_level(logging.INFO, logger="app.services.governance")

    response = client.patch(
        f"/api/v1/items/{item.id}",
        json={"tags": ["memory", "audit"]},
    )

    assert response.status_code == 200
    # A non-governance PATCH must NOT touch the audit trail.
    assert "governance_audit" not in item.metadata_
    # And must NOT emit the structured update event.
    update_records = [
        record for record in caplog.records
        if record.name == "app.services.governance"
        and record.getMessage() == "governance.item.update"
    ]
    assert update_records == []


def test_patch_idempotent_governance_does_not_grow_audit_trail() -> None:
    item = _item()
    # Seed the column-mapped attribute so ``previous_governance_state`` reads
    # "alice" on the first call. The PATCH payload matches exactly, so the
    # diff is empty and the helper must NOT append to the audit list.
    item.governance_owner_subject = "alice"

    session = FakeSession(item)
    client = _client(session)

    payload = {"governance": {"owner_subject": "alice"}}

    first = client.patch(f"/api/v1/items/{item.id}", json=payload)
    second = client.patch(f"/api/v1/items/{item.id}", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    # No-op diff must not append; the audit key may not even exist when no
    # change has ever been recorded.
    assert item.metadata_.get("governance_audit", []) == []


def test_get_item_returns_governance_block_for_legacy_rows() -> None:
    # A row whose governance columns were never triaged must still serialize
    # through the ItemResponse model without raising.
    item = _item()
    assert item.governance_owner_subject is None
    session = FakeSession(item)
    client = _client(session)

    response = client.get(f"/api/v1/items/{item.id}")

    assert response.status_code == 200
    payload = response.json()
    # The response must always carry the ``governance`` nested object so
    # clients can branch on the field name; per-field None is the safe
    # default.
    assert "governance" in payload
    assert payload["governance"]["owner_subject"] is None
    assert payload["governance"]["verification_state"] is None
    assert payload["governance"]["risk_class"] is None
