import asyncio
import uuid
from datetime import date, datetime, timezone

from sqlalchemy.dialects import postgresql

from app.models.item import Item
from app.services.wakeup_briefs import (
    WakeupBriefBatchResult,
    WakeupBriefKey,
    WakeupDiaryContext,
    WakeupFactContext,
    WakeupRoomContext,
    build_wakeup_brief_idempotency_key,
    build_wakeup_brief_summary,
    generate_wakeup_briefs,
    wakeup_brief_summary_statement,
)


class _ScalarRows:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return self._items


class FakeSession:
    def __init__(self, items, *, state=None):
        self.items = list(items)
        self.state = state
        self.added = []
        self.commits = 0
        self.flushes = 0

    async def execute(self, _statement):
        return _ScalarRows(self.items)

    async def get(self, model, key):
        if getattr(model, "__name__", "") == "PalaceTenantState" and self.state and key == self.state.tenant_id:
            return self.state
        return None

    def add(self, value):
        self.added.append(value)
        self.items.append(value)

    async def flush(self):
        self.flushes += 1
        for item in self.added:
            if getattr(item, "id", None) is None:
                item.id = uuid.uuid4()

    async def commit(self):
        self.commits += 1


def _existing_brief(*, key: WakeupBriefKey, generation: int) -> Item:
    return Item(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        source_type="note",
        source_url=f"memory://wakeup-brief/{key.scope_type}/{key.scope_key or 'tenant'}/{key.day.isoformat()}",
        title=f"Wake-up Brief {key.day.isoformat()} [{key.scope_type}:{key.scope_key}]",
        summary="stale summary",
        raw_content="stale body",
        status="ready",
        created_at=datetime(2026, 4, 23, 6, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 4, 23, 6, 0, tzinfo=timezone.utc),
        tags=["wake-up-brief"],
        categories=["brief"],
        idempotency_key=build_wakeup_brief_idempotency_key(tenant_id="tenant-a", key=key),
        metadata_={
            "wakeup_brief": {
                "schema_version": 1,
                "day": key.day.isoformat(),
                "scope_type": key.scope_type,
                "scope_key": key.scope_key,
                "generation": generation,
                "room_ids": [],
                "diary_item_ids": [],
                "fact_ids": [],
                "room_count": 0,
                "diary_count": 0,
                "fact_count": 0,
            }
        },
    )


def test_wakeup_brief_summary_statement_omits_raw_body_and_chunks() -> None:
    statement = wakeup_brief_summary_statement(tenant_id="tenant-a")

    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert "items.title" in sql
    assert "items.metadata" in sql
    assert "items.updated_at" in sql
    assert "items.raw_content" not in sql
    assert "items.content_chunks" not in sql
    assert "items.metadata ? " in sql


def test_generate_wakeup_briefs_creates_tenant_and_wing_briefs(monkeypatch) -> None:
    state = type("State", (), {"tenant_id": "tenant-a", "indexed_generation": 7})()
    session = FakeSession([], state=state)
    processed = []
    room = WakeupRoomContext(
        room_id=uuid.uuid4(),
        wing_slug="product-growth",
        wing_name="Product / Growth",
        room_name="Launch Narrative",
        item_count=4,
        summary="Launch constraints and positioning.",
        updated_at=datetime(2026, 4, 23, 5, 0, tzinfo=timezone.utc),
    )
    diary = WakeupDiaryContext(
        item_id=uuid.uuid4(),
        title="Diary Rollup 2026-04-22 [workspace:launch-pad]",
        summary="Late-night launch decisions.",
        raw_content="Late-night launch decisions.",
        updated_at=datetime(2026, 4, 23, 4, 0, tzinfo=timezone.utc),
    )
    fact = WakeupFactContext(
        fact_id=uuid.uuid4(),
        source_item_id=uuid.uuid4(),
        source_item_title="Launch brief",
        subject="Launch plan",
        predicate="targets",
        object_text="May 2026 rollout",
        extracted_at=datetime(2026, 4, 23, 3, 0, tzinfo=timezone.utc),
        valid_from=datetime(2026, 5, 1, tzinfo=timezone.utc),
        valid_to=None,
    )

    monkeypatch.setattr("app.services.wakeup_briefs._list_wakeup_rooms", lambda *args, **kwargs: asyncio.sleep(0, result=[room]))
    monkeypatch.setattr("app.services.wakeup_briefs._list_recent_diary_rollups", lambda *args, **kwargs: asyncio.sleep(0, result=[diary]))
    monkeypatch.setattr("app.services.wakeup_briefs._list_active_facts", lambda *args, **kwargs: asyncio.sleep(0, result=[fact]))

    async def fake_process_prebuilt_item(db, *, item, **_kwargs):
        assert db is session
        item.status = "ready"
        processed.append(item.title)

    monkeypatch.setattr("app.services.wakeup_briefs.process_prebuilt_item", fake_process_prebuilt_item)

    result = asyncio.run(
        generate_wakeup_briefs(
            session,
            tenant_id="tenant-a",
            embedder=object(),
            llm=object(),
            target_day=date(2026, 4, 23),
        )
    )

    assert result == WakeupBriefBatchResult(created=2, updated=0, unchanged=0, deactivated=0)
    assert len(session.added) == 2
    assert any("Wake-up Brief 2026-04-23 [tenant]" == title for title in processed)
    assert any("Wake-up Brief 2026-04-23 [wing:product-growth]" == title for title in processed)
    assert session.commits == 2


def test_generate_wakeup_briefs_skips_unchanged_briefs(monkeypatch) -> None:
    state = type("State", (), {"tenant_id": "tenant-a", "indexed_generation": 7})()
    tenant_key = WakeupBriefKey(day=date(2026, 4, 23), scope_type="tenant", scope_key=None)
    wing_key = WakeupBriefKey(day=date(2026, 4, 23), scope_type="wing", scope_key="product-growth")
    tenant_brief = _existing_brief(key=tenant_key, generation=7)
    wing_brief = _existing_brief(key=wing_key, generation=7)
    session = FakeSession([tenant_brief, wing_brief], state=state)

    room = WakeupRoomContext(
        room_id=uuid.uuid4(),
        wing_slug="product-growth",
        wing_name="Product / Growth",
        room_name="Launch Narrative",
        item_count=4,
        summary="Launch constraints and positioning.",
        updated_at=datetime(2026, 4, 23, 5, 0, tzinfo=timezone.utc),
    )
    diary = WakeupDiaryContext(
        item_id=uuid.uuid4(),
        title="Diary Rollup 2026-04-22 [workspace:launch-pad]",
        summary="Late-night launch decisions.",
        raw_content="Late-night launch decisions.",
        updated_at=datetime(2026, 4, 23, 4, 0, tzinfo=timezone.utc),
    )
    fact = WakeupFactContext(
        fact_id=uuid.uuid4(),
        source_item_id=uuid.uuid4(),
        source_item_title="Launch brief",
        subject="Launch plan",
        predicate="targets",
        object_text="May 2026 rollout",
        extracted_at=datetime(2026, 4, 23, 3, 0, tzinfo=timezone.utc),
        valid_from=datetime(2026, 5, 1, tzinfo=timezone.utc),
        valid_to=None,
    )

    monkeypatch.setattr("app.services.wakeup_briefs._list_wakeup_rooms", lambda *args, **kwargs: asyncio.sleep(0, result=[room]))
    monkeypatch.setattr("app.services.wakeup_briefs._list_recent_diary_rollups", lambda *args, **kwargs: asyncio.sleep(0, result=[diary]))
    monkeypatch.setattr("app.services.wakeup_briefs._list_active_facts", lambda *args, **kwargs: asyncio.sleep(0, result=[fact]))

    preview = []

    async def fake_process_prebuilt_item(_db, *, item, **_kwargs):
        item.status = "ready"
        preview.append(item)

    monkeypatch.setattr("app.services.wakeup_briefs.process_prebuilt_item", fake_process_prebuilt_item)

    asyncio.run(
        generate_wakeup_briefs(
            session,
            tenant_id="tenant-a",
            embedder=object(),
            llm=object(),
            target_day=date(2026, 4, 23),
        )
    )

    tenant_processed = next(item for item in preview if item.metadata_["wakeup_brief"]["scope_type"] == "tenant")
    wing_processed = next(item for item in preview if item.metadata_["wakeup_brief"]["scope_type"] == "wing")
    unchanged_session = FakeSession([tenant_processed, wing_processed], state=state)

    monkeypatch.setattr("app.services.wakeup_briefs._list_wakeup_rooms", lambda *args, **kwargs: asyncio.sleep(0, result=[room]))
    monkeypatch.setattr("app.services.wakeup_briefs._list_recent_diary_rollups", lambda *args, **kwargs: asyncio.sleep(0, result=[diary]))
    monkeypatch.setattr("app.services.wakeup_briefs._list_active_facts", lambda *args, **kwargs: asyncio.sleep(0, result=[fact]))

    async def fail_process(*_args, **_kwargs):
        raise AssertionError("unchanged brief should not be reprocessed")

    monkeypatch.setattr("app.services.wakeup_briefs.process_prebuilt_item", fail_process)

    result = asyncio.run(
        generate_wakeup_briefs(
            unchanged_session,
            tenant_id="tenant-a",
            embedder=object(),
            llm=object(),
            target_day=date(2026, 4, 23),
        )
    )

    assert result == WakeupBriefBatchResult(created=0, updated=0, unchanged=2, deactivated=0)


def test_build_wakeup_brief_summary_reports_freshness() -> None:
    today = date(2026, 4, 23)
    fresh_item = Item(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        source_type="note",
        title="Wake-up Brief 2026-04-23 [tenant]",
        summary="fresh",
        raw_content="body",
        status="ready",
        created_at=datetime(2026, 4, 23, 6, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 4, 23, 6, 5, tzinfo=timezone.utc),
        metadata_={
            "wakeup_brief": {
                "day": "2026-04-23",
                "scope_type": "tenant",
                "scope_key": None,
                "generation": 7,
                "room_count": 2,
                "diary_count": 1,
                "fact_count": 3,
            }
        },
    )
    stale_item = Item(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        source_type="note",
        title="Wake-up Brief 2026-04-23 [wing:product-growth]",
        summary="stale",
        raw_content="body",
        status="ready",
        created_at=datetime(2026, 4, 23, 6, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 4, 23, 6, 1, tzinfo=timezone.utc),
        metadata_={
            "wakeup_brief": {
                "day": "2026-04-23",
                "scope_type": "wing",
                "scope_key": "product-growth",
                "generation": 6,
                "room_count": 1,
                "diary_count": 1,
                "fact_count": 1,
            }
        },
    )
    old_item = Item(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        source_type="note",
        title="Wake-up Brief 2026-04-22 [tenant]",
        summary="old",
        raw_content="body",
        status="ready",
        created_at=datetime(2026, 4, 22, 6, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 4, 22, 6, 1, tzinfo=timezone.utc),
        metadata_={"wakeup_brief": {"day": "2026-04-22", "scope_type": "tenant", "generation": 6}},
    )
    session = FakeSession([fresh_item, stale_item, old_item])

    summary = asyncio.run(
        build_wakeup_brief_summary(
            session,
            tenant_id="tenant-a",
            indexed_generation=7,
            today=today,
        )
    )

    assert summary["fresh"] == 1
    assert summary["stale"] == 1
    assert summary["generated_for_day"] == today
    assert summary["last_refreshed_at"] == fresh_item.updated_at
    assert len(summary["recent_briefs"]) == 2
