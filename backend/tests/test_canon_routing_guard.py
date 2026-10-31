"""Self-drift regression: a canon publish must never route itself into a room.

Design section 4, "Routing guard, non-negotiable". `_apply_room_routing` assigns
every ready item to a wing and room and bumps `room.membership_generation`.
Without an explicit canon skip, every canon publish would delete the authored
charter membership, bump the generation, and make the drift detector fire
against the page's own edit. This file pins the guard in both directions: a
canon item is skipped with no membership writes and no affected rooms, and an
ordinary item is still routed exactly as before.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.models.palace import Room, RoomMembership
from app.services import palace as palace_service
from app.services.palace import _apply_room_routing, is_canon_item


class _ScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _RecordingDb:
    """Records every mutation so a skipped route is provable, not assumed."""

    def __init__(self, *, existing_auto=None, pinned_primary=None) -> None:
        self.existing_auto = list(existing_auto or [])
        self.pinned_primary = pinned_primary
        self.ops: list[str] = []
        self.added: list[Any] = []

    async def execute(self, _statement):
        self.ops.append("execute:auto")
        return _ScalarsResult(self.existing_auto)

    async def scalar(self, _statement):
        self.ops.append("scalar:pinned")
        return self.pinned_primary

    async def delete(self, membership) -> None:
        self.ops.append(f"delete:{membership.room_id}")

    async def flush(self) -> None:
        self.ops.append("flush")

    def add(self, row) -> None:
        self.ops.append(f"add:{type(row).__name__}")
        self.added.append(row)


def _room(tenant_id: str, *, generation: int = 0) -> Room:
    return Room(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        wing_id=uuid.uuid4(),
        stable_key="company:charter",
        slug="charter",
        name="Charter",
        membership_generation=generation,
    )


def test_is_canon_item_detects_source_type_and_metadata() -> None:
    assert is_canon_item(SimpleNamespace(source_type="canon", metadata_={})) is True
    assert is_canon_item(SimpleNamespace(source_type="note", metadata_={"canon": {"slug": "x"}})) is True
    assert is_canon_item(SimpleNamespace(source_type="note", metadata_={})) is False
    assert is_canon_item(SimpleNamespace(source_type="pdf", metadata_={"wakeup_brief": {}})) is False


@pytest.mark.asyncio
async def test_canon_item_is_not_routed_and_leaves_room_generation_untouched(monkeypatch) -> None:
    tenant_id = "tenant-a"
    room = _room(tenant_id, generation=7)
    item_id = uuid.uuid4()
    # A canon item that a previous, unguarded run had already auto-routed. The
    # guard must not even touch that row, because deleting it would be the same
    # self-inflicted drift the guard exists to prevent.
    stale_auto = RoomMembership(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        room_id=room.id,
        item_id=item_id,
        source="auto",
        membership_kind="primary",
    )
    item = SimpleNamespace(
        id=item_id,
        source_type="canon",
        status="ready",
        metadata_={"canon": {"slug": "company-charter"}, "sync_active": False},
        title="Company charter",
        tags=["canon"],
        categories=["canon"],
    )
    db = _RecordingDb(existing_auto=[stale_auto])

    async def fake_ensure_room(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("canon item must never reach _ensure_room")

    monkeypatch.setattr(palace_service, "_ensure_room", fake_ensure_room)

    affected_rooms = await _apply_room_routing(db, tenant_id=tenant_id, item=item, generation=9)

    assert affected_rooms == set()
    assert db.ops == []
    assert db.added == []
    assert room.membership_generation == 7


@pytest.mark.asyncio
async def test_non_canon_item_is_still_routed(monkeypatch) -> None:
    """The guard must not fire on ordinary items, or routing silently stops."""
    tenant_id = "tenant-a"
    room = _room(tenant_id)
    item_id = uuid.uuid4()
    item = SimpleNamespace(
        id=item_id,
        source_type="note",
        status="ready",
        metadata_={"sync_relative_path": "company/charter.md"},
        title="Charter",
        tags=[],
        categories=[],
    )
    db = _RecordingDb()

    async def fake_ensure_room(db_arg, *, tenant_id: str, wing_name: str, room_name: str):
        return room

    monkeypatch.setattr(palace_service, "_ensure_room", fake_ensure_room)

    affected_rooms = await _apply_room_routing(db, tenant_id=tenant_id, item=item, generation=5)

    assert affected_rooms == {room.id}
    assert any(op.startswith("add:RoomMembership") for op in db.ops)
    assert db.added[0].item_id == item_id
    assert room.membership_generation == 5
