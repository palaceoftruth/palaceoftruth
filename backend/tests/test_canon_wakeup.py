"""The canon block in the wake-up brief.

Two acceptance points live here: the brief metadata records canon character
spend, and canon leads the body above the room list. A third is a decision of
mine worth pinning: when there is no canon the section is absent entirely,
because a "no canon" placeholder trains an agent to skip the section.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import pytest

from app.services.canon import CanonDigest
from app.services.wakeup_briefs import (
    WakeupBriefKey,
    WakeupRoomContext,
    _brief_metadata,
    _render_wakeup_body,
    generate_wakeup_briefs,
)

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
KEY = WakeupBriefKey(day=date(2026, 10, 31), scope_type="tenant", scope_key=None)
HEADING = "## Canon (authored, authoritative)"


def _digest(**overrides) -> CanonDigest:
    values = dict(
        text="identity: we build agentic operations software.\nrules: no silent fallback.",
        char_count=74,
        page_count=4,
        truncated=False,
        next=None,
        fingerprint="fp-1",
    )
    values.update(overrides)
    return CanonDigest(**values)


def _room(wing_slug: str = "company", wing_name: str = "Company") -> WakeupRoomContext:
    return WakeupRoomContext(
        room_id=uuid.uuid4(),
        wing_slug=wing_slug,
        wing_name=wing_name,
        room_name="Ops",
        item_count=12,
        summary="Runs the platform.",
        updated_at=NOW,
    )


def _render(canon=None) -> str:
    return _render_wakeup_body(
        KEY,
        generation=9,
        rooms=[_room()],
        diaries=[],
        facts=[],
        canon=canon,
    )


# --- The body -------------------------------------------------------------


def test_canon_leads_the_body_above_the_rooms() -> None:
    body = _render(_digest())

    assert HEADING in body
    assert body.index(HEADING) < body.index("## High-signal rooms")
    assert "we build agentic operations software" in body


def test_the_digest_text_is_verbatim() -> None:
    """The service owns every cap. The brief must not re-wrap or re-cut it."""
    digest = _digest()

    assert digest.text in _render(digest)


def test_no_canon_emits_no_section_at_all() -> None:
    body = _render(None)

    assert HEADING not in body
    assert "canon" not in body.lower()


def test_an_empty_digest_emits_no_section() -> None:
    """An empty digest is not canon. A bare heading would be noise."""
    body = _render(_digest(text="   ", char_count=0, page_count=0))

    assert HEADING not in body


def test_the_rest_of_the_body_is_unchanged() -> None:
    """Adding canon must not disturb the sections that already exist."""
    with_canon = _render(_digest())
    without = _render(None)

    for section in ("## High-signal rooms", "## Recent diary rollups", "## Current facts"):
        assert section in with_canon
        assert section in without
    # Canon adds content, and the remainder is preserved in order.
    tail = with_canon.split("## High-signal rooms", 1)[1]
    assert tail == without.split("## High-signal rooms", 1)[1]


# --- The metadata: acceptance asks for visible spend ----------------------


def test_metadata_records_the_canon_spend() -> None:
    metadata = _brief_metadata(
        key=KEY,
        idempotency_key="idem-1",
        generation=9,
        rooms=[_room()],
        diaries=[],
        facts=[],
        canon=_digest(),
    )

    assert metadata["canon"] == {"page_count": 4, "chars": 74, "truncated": False}


def test_metadata_records_truncation() -> None:
    metadata = _brief_metadata(
        key=KEY,
        idempotency_key="idem-1",
        generation=9,
        rooms=[_room()],
        diaries=[],
        facts=[],
        canon=_digest(truncated=True, next="canon:digest @page 13"),
    )

    assert metadata["canon"]["truncated"] is True


def test_metadata_records_absence_rather_than_omitting_the_key() -> None:
    """A missing key and a zero spend look the same to a reader. They are not."""
    metadata = _brief_metadata(
        key=KEY,
        idempotency_key="idem-1",
        generation=9,
        rooms=[_room()],
        diaries=[],
        facts=[],
        canon=None,
    )

    assert "canon" in metadata
    assert metadata["canon"] is None


# --- The batch: one digest, and a canon failure cannot stop a brief -------


class _FakeState:
    indexed_generation = 9


class _FakeResult:
    def scalars(self):
        return self

    def all(self):
        return []


class _FakeDb:
    async def get(self, _model, _key):
        return _FakeState()

    async def execute(self, *_args, **_kwargs):
        return _FakeResult()

    def add(self, _obj):
        return None

    async def flush(self):
        return None

    async def commit(self):
        return None


@pytest.mark.asyncio
async def test_the_digest_is_computed_once_per_batch(monkeypatch) -> None:
    """The digest is tenant-wide. Recomputing it per scope is pure waste, and a
    wing-heavy tenant would pay for it many times over."""
    calls: list[str] = []

    async def fake_digest(_db, *, tenant_id, **_kwargs):
        calls.append(tenant_id)
        return _digest()

    monkeypatch.setattr("app.services.wakeup_briefs.get_canon_digest", fake_digest)
    monkeypatch.setattr(
        "app.services.wakeup_briefs.process_prebuilt_item", _noop_process
    )

    rooms = [_room("company", "Company"), _room("sales", "Sales"), _room("support", "Support")]
    monkeypatch.setattr(
        "app.services.wakeup_briefs._list_wakeup_rooms",
        lambda *_a, **_k: _async(rooms),
    )
    monkeypatch.setattr(
        "app.services.wakeup_briefs._list_recent_diary_rollups",
        lambda *_a, **_k: _async([]),
    )
    monkeypatch.setattr(
        "app.services.wakeup_briefs._list_active_facts",
        lambda *_a, **_k: _async([]),
    )

    result = await generate_wakeup_briefs(
        _FakeDb(), tenant_id="tenant-a", embedder=None, llm=None
    )

    assert len(calls) == 1, f"digest computed {len(calls)} times for {result}"


@pytest.mark.asyncio
async def test_a_canon_failure_does_not_stop_the_briefs(monkeypatch) -> None:
    """A missing brief is worse than a brief without canon."""

    async def broken(_db, *, tenant_id, **_kwargs):
        raise RuntimeError("canon is down")

    monkeypatch.setattr("app.services.wakeup_briefs.get_canon_digest", broken)
    monkeypatch.setattr(
        "app.services.wakeup_briefs.process_prebuilt_item", _noop_process
    )
    monkeypatch.setattr(
        "app.services.wakeup_briefs._list_wakeup_rooms",
        lambda *_a, **_k: _async([_room()]),
    )
    monkeypatch.setattr(
        "app.services.wakeup_briefs._list_recent_diary_rollups",
        lambda *_a, **_k: _async([]),
    )
    monkeypatch.setattr(
        "app.services.wakeup_briefs._list_active_facts",
        lambda *_a, **_k: _async([]),
    )

    result = await generate_wakeup_briefs(
        _FakeDb(), tenant_id="tenant-a", embedder=None, llm=None
    )

    assert result.created + result.updated + result.unchanged >= 1


async def _async(value):
    return value


async def _noop_process(*_args, **_kwargs):
    return None
