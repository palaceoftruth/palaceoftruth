"""Canon repo binding: schema, creation, retargeting, and the pre-P8 run guard.

These are the rules that stop a binding from quietly doing the wrong thing. A
canon source that fell through to the room path would write room items for pages
no run is allowed to publish, and a binding retargeted after a run would make the
history describe work that never happened.
"""

from __future__ import annotations

import uuid
from collections import deque

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from app.models.palace import Room, SyncRun, SyncSource
from app.schemas.palace import SyncSourceCreate, SyncSourceUpdate
from app.services.palace import (
    _sync_source_summary,
    create_or_get_sync_run,
    create_sync_source,
    update_sync_source,
)


class _FakeSession:
    """Enough of AsyncSession for these paths, and no more.

    Room and SyncRun queries are answered from queues so a test states what each
    call returns instead of relying on call order across functions.
    """

    def __init__(self, *, room_ids=(), sync_run_results=()):
        self.added: list[object] = []
        self.commits = 0
        self.refreshed: list[object] = []
        self._room_ids = deque(room_ids)
        self._sync_runs = deque(sync_run_results)

    async def scalar(self, statement):
        description = statement.column_descriptions[0]
        entity = description.get("entity")
        if entity is Room:
            return self._room_ids.popleft() if self._room_ids else None
        if entity is SyncRun:
            return self._sync_runs.popleft() if self._sync_runs else None
        raise AssertionError(f"unexpected scalar query for {entity}")

    async def execute(self, statement):
        raise AssertionError("these paths should not execute a statement list")

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        self.refreshed.append(obj)

    async def get(self, model, key):
        return None


def _binding_body(**overrides):
    values = {
        "name": "agent corpus",
        "source_kind": "repo",
        "root_path": "https://github.com/sarverenterprises/agent-corpus",
        "target_kind": "canon",
        "canon_section_default": "reference",
        "scan_interval_seconds": 900,
    }
    values.update(overrides)
    return SyncSourceCreate(**values)


def _source(**overrides):
    values = {
        "id": uuid.uuid4(),
        "tenant_id": "tenant-a",
        "name": "agent corpus",
        "root_path": "https://github.com/sarverenterprises/agent-corpus",
        "source_kind": "repo",
        "credential_type": "none",
        "status": "active",
        "scan_interval_seconds": 900,
        "target_kind": "rooms",
        "canon_section_default": "reference",
        "canon_owns_pages": True,
    }
    values.update(overrides)
    return SyncSource(**values)


# --- schema ---------------------------------------------------------------


def test_an_unknown_section_is_refused_at_the_schema() -> None:
    with pytest.raises(ValidationError) as excinfo:
        _binding_body(canon_section_default="procedures")
    assert "canon_section_default must be one of" in str(excinfo.value)
    assert "runbook" in str(excinfo.value)


def test_the_schema_defaults_to_the_room_target() -> None:
    body = SyncSourceCreate(name="legacy", source_kind="folder", root_path="/srv/corpus", scan_interval_seconds=900)
    assert body.target_kind == "rooms"
    assert body.canon_section_default == "reference"


# --- creation -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_canon_binding_refuses_object_storage(monkeypatch) -> None:
    """A bucket has no commit history, so it cannot be a canon source in v1."""

    async def _probe(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("the probe must not run for a refused binding")

    monkeypatch.setattr("app.services.palace.probe_canon_repo", _probe)
    session = _FakeSession()

    with pytest.raises(HTTPException) as excinfo:
        await create_sync_source(
            session,
            tenant_id="tenant-a",
            body=_binding_body(
                source_kind="s3",
                root_path=None,
                bucket="corpus",
                endpoint_url="https://s3.example.com",
                region="us-east-1",
            ),
        )
    assert excinfo.value.status_code == 422
    assert "requires a repo source" in excinfo.value.detail
    assert session.added == []


@pytest.mark.asyncio
async def test_a_local_checkout_can_be_a_canon_binding(monkeypatch, tmp_path) -> None:
    """A checkout on this host is the same repository, and needs no clone."""
    probed: list[str] = []

    async def _probe(root_path, *, credential_ciphertext):
        probed.append(root_path)

    monkeypatch.setattr("app.services.palace.probe_canon_repo", _probe)
    # Folder sources are confined to the configured sync roots; a test directory
    # becomes one for the duration of this test.
    monkeypatch.setattr(
        "app.services.palace._allowed_sync_roots", lambda: [tmp_path.resolve()]
    )
    session = _FakeSession()

    source = await create_sync_source(
        session,
        tenant_id="tenant-a",
        body=_binding_body(source_kind="folder", root_path=str(tmp_path)),
    )
    assert probed == [str(tmp_path)]
    assert source.target_kind == "canon"


@pytest.mark.asyncio
async def test_an_unreachable_repo_refuses_the_binding(monkeypatch) -> None:
    async def _probe(*_args, **_kwargs):
        raise HTTPException(status_code=422, detail="repo unreachable: fatal: repository not found")

    monkeypatch.setattr("app.services.palace.probe_canon_repo", _probe)
    session = _FakeSession()

    with pytest.raises(HTTPException) as excinfo:
        await create_sync_source(session, tenant_id="tenant-a", body=_binding_body())
    assert excinfo.value.status_code == 422
    assert "repo unreachable" in excinfo.value.detail
    assert session.added == [], "a refused binding must not be persisted"


@pytest.mark.asyncio
async def test_a_binding_stores_its_canon_fields(monkeypatch) -> None:
    probed: list[tuple[str, object]] = []

    async def _probe(root_path, *, credential_ciphertext):
        probed.append((root_path, credential_ciphertext))

    monkeypatch.setattr("app.services.palace.probe_canon_repo", _probe)
    room_id = uuid.uuid4()
    session = _FakeSession(room_ids=[room_id])

    source = await create_sync_source(
        session,
        tenant_id="tenant-a",
        body=_binding_body(canon_section_default="charter", canon_room_id=room_id),
    )

    assert probed == [("https://github.com/sarverenterprises/agent-corpus", None)]
    assert source.target_kind == "canon"
    assert source.canon_section_default == "charter"
    assert source.canon_room_id == room_id
    assert source.canon_owns_pages is True
    assert source in session.added
    assert session.commits == 1


@pytest.mark.asyncio
async def test_a_binding_pointing_at_another_tenants_room_is_refused(monkeypatch) -> None:
    async def _probe(*_args, **_kwargs):  # pragma: no cover - refused earlier
        raise AssertionError("the probe must not run when the room is invalid")

    monkeypatch.setattr("app.services.palace.probe_canon_repo", _probe)
    # Room query answers nothing: the room does not belong to this tenant.
    session = _FakeSession(room_ids=[None])

    with pytest.raises(HTTPException) as excinfo:
        await create_sync_source(
            session,
            tenant_id="tenant-a",
            body=_binding_body(canon_room_id=uuid.uuid4()),
        )
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == "room not found"
    assert session.added == []


# --- retargeting ----------------------------------------------------------


@pytest.mark.asyncio
async def test_retargeting_after_a_run_is_refused() -> None:
    source = _source(target_kind="rooms")
    # First SyncRun query is the active-run mutability check, second is "has any run".
    session = _FakeSession(sync_run_results=[None, uuid.uuid4()])

    with pytest.raises(HTTPException) as excinfo:
        await update_sync_source(
            session,
            tenant_id="tenant-a",
            body=SyncSourceUpdate(target_kind="canon"),
            source=source,
        )
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == "target_kind cannot change after a run"
    assert source.target_kind == "rooms"


@pytest.mark.asyncio
async def test_retargeting_before_any_run_is_allowed(monkeypatch) -> None:
    async def _probe(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.services.palace.probe_canon_repo", _probe)
    source = _source(target_kind="rooms")
    session = _FakeSession(sync_run_results=[None, None])

    updated = await update_sync_source(
        session,
        tenant_id="tenant-a",
        body=SyncSourceUpdate(target_kind="canon"),
        source=source,
    )
    assert updated.target_kind == "canon"


# --- the guard before P8 --------------------------------------------------


@pytest.mark.asyncio
async def test_a_canon_source_now_queues_a_run() -> None:
    """P8 removed the pre-run refusal, so a canon source queues like any other."""
    source = _source(target_kind="canon")
    session = _FakeSession(sync_run_results=[None])

    run, created = await create_or_get_sync_run(
        session, tenant_id="tenant-a", source=source, triggered_by="scheduled"
    )

    assert created is True
    assert run.status == "queued"
    assert run.error_message is None


@pytest.mark.asyncio
async def test_a_room_source_still_queues_normally() -> None:
    source = _source(target_kind="rooms")
    session = _FakeSession(sync_run_results=[None])

    run, created = await create_or_get_sync_run(
        session, tenant_id="tenant-a", source=source, triggered_by="scheduled"
    )

    assert created is True
    assert run.status == "queued"
    assert run.error_message is None


# --- reporting ------------------------------------------------------------


def test_the_summary_reports_the_binding_fields() -> None:
    room_id = uuid.uuid4()
    summary = _sync_source_summary(
        _source(target_kind="canon", canon_section_default="charter", canon_room_id=room_id)
    )
    assert summary.target_kind == "canon"
    assert summary.canon_section_default == "charter"
    assert summary.canon_room_id == room_id
    assert summary.canon_owns_pages is True


def test_the_summary_treats_a_missing_target_as_rooms() -> None:
    source = _source()
    source.target_kind = None
    summary = _sync_source_summary(source)
    assert summary.target_kind == "rooms"


# --- the probe itself -----------------------------------------------------
#
# The tests above prove that create and update call the probe and pass its
# refusal through. They do not prove the probe refuses anything, because they
# replace it. A probe that silently returned would pass all of them.

@pytest.mark.asyncio
async def test_the_probe_raises_when_the_path_is_not_a_repository() -> None:
    from app.services.palace import probe_canon_repo

    with pytest.raises(HTTPException) as excinfo:
        await probe_canon_repo("/nonexistent/canon-corpus-path", credential_ciphertext=None)

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail.startswith("repo unreachable:")


def test_a_credential_never_appears_in_a_probe_message() -> None:
    from app.services.palace import _probe_reason

    secret = "ghp_SUPERSECRETVALUE1234567890"
    detail = _probe_reason(
        f"fatal: unable to access 'https://x-access-token:{secret}@github.com/o/r/': 403",
        "",
        secret,
    )
    assert secret not in detail
    assert "***" in detail


def test_an_empty_failure_still_produces_a_reason() -> None:
    from app.services.palace import _probe_reason

    assert _probe_reason("", "", None) == "git command failed"
