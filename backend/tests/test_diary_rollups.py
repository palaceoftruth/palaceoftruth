import asyncio
import uuid
from datetime import date, datetime, timezone

from app.models.item import Item
from app.services.diary_rollups import (
    DiaryRollupBatchResult,
    DiaryRollupKey,
    build_diary_rollup_summary,
    build_diary_rollup_idempotency_key,
    generate_memory_diary_rollups,
)
from app.workers.palace_tasks import _diary_rollup_target_days, run_diary_rollup_maintenance


class _ScalarRows:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return self._items


class FakeSession:
    def __init__(self, items):
        self.items = list(items)
        self.added = []
        self.commits = 0
        self.flushes = 0

    async def execute(self, _statement):
        return _ScalarRows(self.items)

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


def _memory_note(
    *,
    item_id: uuid.UUID | None = None,
    title: str,
    body: str,
    created_at: datetime,
    scope_type: str,
    scope_key: str | None,
    summary: str | None = None,
) -> Item:
    return Item(
        id=item_id or uuid.uuid4(),
        tenant_id="tenant-a",
        source_type="note",
        title=title,
        summary=summary,
        raw_content=body,
        status="ready",
        created_at=created_at,
        updated_at=created_at,
        tags=[f"scope-{scope_type}"],
        metadata_={
            "memory_entry": {
                "scope": {"type": scope_type, "key": scope_key},
                "source": "hermes",
            }
        },
    )


def _existing_rollup(*, key: DiaryRollupKey, source_items: list[Item], source_ids: list[str] | None = None) -> Item:
    return Item(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        source_type="note",
        source_url=f"memory://diary-rollup/{key.scope_type}/{key.scope_key or 'shared'}/{key.day.isoformat()}",
        title=f"Diary Rollup {key.day.isoformat()} [{key.scope_type}:{key.scope_key}]",
        summary="Daily scoped diary for workspace:launch-pad from 2 source notes.",
        raw_content="stale body",
        status="ready",
        created_at=datetime(2026, 4, 12, 23, 59, tzinfo=timezone.utc),
        updated_at=datetime(2026, 4, 12, 23, 59, tzinfo=timezone.utc),
        tags=["diary-rollup", f"diary-day-{key.day.isoformat()}", f"scope-{key.scope_type}", f"{key.scope_type}-{key.scope_key}"],
        categories=["diary"],
        idempotency_key=build_diary_rollup_idempotency_key(tenant_id="tenant-a", key=key),
        metadata_={
            "memory_entry": {
                "scope": {"type": key.scope_type, "key": key.scope_key},
                "source": "palace-diary-rollup",
            },
            "diary_rollup": {
                "schema_version": 1,
                "day": key.day.isoformat(),
                "scope_type": key.scope_type,
                "scope_key": key.scope_key,
                "source_item_ids": source_ids if source_ids is not None else [str(item.id) for item in source_items],
                "source_titles": [item.title for item in source_items],
            },
            "sync_relative_path": f"diaries/{key.day.isoformat()}/{key.scope_type}-{key.scope_key or 'shared'}.md",
        },
    )


def test_build_diary_rollup_idempotency_key_is_stable() -> None:
    key = DiaryRollupKey(day=date(2026, 4, 12), scope_type="workspace", scope_key="launch-pad")

    first = build_diary_rollup_idempotency_key(tenant_id="tenant-a", key=key)
    second = build_diary_rollup_idempotency_key(tenant_id="tenant-a", key=key)

    assert first == second


def test_diary_rollup_target_days_replays_two_recent_completed_days() -> None:
    assert _diary_rollup_target_days(today=date(2026, 4, 22)) == (
        date(2026, 4, 20),
        date(2026, 4, 21),
    )


def test_generate_memory_diary_rollups_creates_new_rollup(monkeypatch) -> None:
    source_items = [
        _memory_note(
            title="Shared launch brief",
            summary="Launch context.",
            body="Agents should reuse the launch brief.",
            created_at=datetime(2026, 4, 12, 12, 0, tzinfo=timezone.utc),
            scope_type="workspace",
            scope_key="launch-pad",
        ),
        _memory_note(
            title="Afternoon follow-up",
            body="Reviewed the deploy checklist.",
            created_at=datetime(2026, 4, 12, 16, 30, tzinfo=timezone.utc),
            scope_type="workspace",
            scope_key="launch-pad",
        ),
    ]
    session = FakeSession(source_items)
    processed = []
    marked_dirty = []

    async def fake_process_prebuilt_item(db, *, item, embedder, llm, tenant_id, job=None, enable_ai_enrichment=False):
        assert db is session
        assert tenant_id == "tenant-a"
        assert job is None
        assert enable_ai_enrichment is False
        item.status = "ready"
        processed.append(item)

    async def fake_mark_item_dirty(db, *, tenant_id, item_id, reason, sync_source_id=None):
        assert db is session
        assert tenant_id == "tenant-a"
        assert reason == "diary-rollup"
        assert sync_source_id is None
        marked_dirty.append(item_id)
        return 1

    monkeypatch.setattr("app.services.diary_rollups.process_prebuilt_item", fake_process_prebuilt_item)
    monkeypatch.setattr("app.services.diary_rollups.mark_item_dirty", fake_mark_item_dirty)

    result = asyncio.run(
        generate_memory_diary_rollups(
            session,
            tenant_id="tenant-a",
            embedder=object(),
            llm=object(),
            target_day=date(2026, 4, 12),
        )
    )

    rollup = session.added[0]
    assert result.created == 1
    assert result.updated == 0
    assert result.unchanged == 0
    assert result.deactivated == 0
    assert processed == [rollup]
    assert marked_dirty == [rollup.id]
    assert rollup.title == "Diary Rollup 2026-04-12 [workspace:launch-pad]"
    assert rollup.metadata_["diary_rollup"]["source_item_ids"] == [str(item.id) for item in source_items]
    assert rollup.metadata_["sync_relative_path"] == "diaries/2026-04-12/workspace-launch-pad.md"
    assert "diary-rollup" in rollup.tags


def test_build_diary_rollup_summary_reports_latest_scope_coverage() -> None:
    workspace_old = _existing_rollup(
        key=DiaryRollupKey(day=date(2026, 4, 20), scope_type="workspace", scope_key="launch-pad"),
        source_items=[],
        source_ids=["a"],
    )
    workspace_old.updated_at = datetime(2026, 4, 20, 23, 59, tzinfo=timezone.utc)
    workspace_new = _existing_rollup(
        key=DiaryRollupKey(day=date(2026, 4, 22), scope_type="workspace", scope_key="launch-pad"),
        source_items=[],
        source_ids=["a", "b"],
    )
    workspace_new.updated_at = datetime(2026, 4, 22, 23, 59, tzinfo=timezone.utc)
    session_stale = _existing_rollup(
        key=DiaryRollupKey(day=date(2026, 4, 21), scope_type="session", scope_key="focus-1"),
        source_items=[],
        source_ids=["c"],
    )
    session_stale.updated_at = datetime(2026, 4, 21, 23, 59, tzinfo=timezone.utc)
    unrelated = _memory_note(
        title="Human note",
        body="This is not a diary rollup.",
        created_at=datetime(2026, 4, 22, 10, 0, tzinfo=timezone.utc),
        scope_type="workspace",
        scope_key="launch-pad",
    )

    summary = asyncio.run(
        build_diary_rollup_summary(
            FakeSession([workspace_old, workspace_new, session_stale, unrelated]),
            tenant_id="tenant-a",
            today=date(2026, 4, 23),
        )
    )

    assert summary["fresh"] == 1
    assert summary["stale"] == 1
    assert summary["expected_through_day"] == date(2026, 4, 22)
    assert summary["last_refreshed_at"] == datetime(2026, 4, 22, 23, 59, tzinfo=timezone.utc)
    assert [rollup["scope_type"] for rollup in summary["recent_rollups"]] == ["workspace", "session"]
    assert summary["recent_rollups"][0]["source_count"] == 2
    assert summary["recent_rollups"][0]["stale"] is False
    assert summary["recent_rollups"][1]["stale"] is True


def test_generate_memory_diary_rollups_skips_unchanged_rollup(monkeypatch) -> None:
    source_items = [
        _memory_note(
            title="Shared launch brief",
            body="Agents should reuse the launch brief.",
            created_at=datetime(2026, 4, 12, 12, 0, tzinfo=timezone.utc),
            scope_type="workspace",
            scope_key="launch-pad",
            summary="Launch context.",
        ),
    ]
    preview_session = FakeSession(source_items)

    async def fake_process_preview(_db, *, item, **_kwargs):
        item.status = "ready"

    async def fake_mark_item_dirty_preview(*args, **kwargs):
        return 1

    monkeypatch.setattr("app.services.diary_rollups.process_prebuilt_item", fake_process_preview)
    monkeypatch.setattr("app.services.diary_rollups.mark_item_dirty", fake_mark_item_dirty_preview)

    preview_result = asyncio.run(
        generate_memory_diary_rollups(
            preview_session,
            tenant_id="tenant-a",
            embedder=object(),
            llm=object(),
            target_day=date(2026, 4, 12),
        )
    )
    assert preview_result.created == 1

    unchanged_rollup = preview_session.added[0]
    unchanged_session = FakeSession([*source_items, unchanged_rollup])

    async def fake_process_never(*args, **kwargs):
        raise AssertionError("unchanged rollup should not be reprocessed")

    async def fake_mark_item_dirty_never(*args, **kwargs):
        raise AssertionError("unchanged rollup should not be marked dirty")

    monkeypatch.setattr("app.services.diary_rollups.process_prebuilt_item", fake_process_never)
    monkeypatch.setattr("app.services.diary_rollups.mark_item_dirty", fake_mark_item_dirty_never)

    result = asyncio.run(
        generate_memory_diary_rollups(
            unchanged_session,
            tenant_id="tenant-a",
            embedder=object(),
            llm=object(),
            target_day=date(2026, 4, 12),
        )
    )

    assert result.created == 0
    assert result.updated == 0
    assert result.unchanged == 1
    assert result.deactivated == 0


def test_generate_memory_diary_rollups_updates_existing_rollup_when_sources_change(monkeypatch) -> None:
    source_items = [
        _memory_note(
            title="Shared launch brief",
            body="Agents should reuse the launch brief.",
            created_at=datetime(2026, 4, 12, 12, 0, tzinfo=timezone.utc),
            scope_type="workspace",
            scope_key="launch-pad",
        ),
        _memory_note(
            title="Afternoon follow-up",
            body="Reviewed the deploy checklist.",
            created_at=datetime(2026, 4, 12, 16, 30, tzinfo=timezone.utc),
            scope_type="workspace",
            scope_key="launch-pad",
        ),
    ]
    key = DiaryRollupKey(day=date(2026, 4, 12), scope_type="workspace", scope_key="launch-pad")
    existing = _existing_rollup(key=key, source_items=source_items[:1], source_ids=[str(source_items[0].id)])
    session = FakeSession([*source_items, existing])
    processed = []
    marked_dirty = []

    async def fake_process_prebuilt_item(db, *, item, **kwargs):
        assert db is session
        item.status = "ready"
        processed.append(item.id)

    async def fake_mark_item_dirty(db, *, item_id, **kwargs):
        assert db is session
        marked_dirty.append(item_id)
        return 2

    monkeypatch.setattr("app.services.diary_rollups.process_prebuilt_item", fake_process_prebuilt_item)
    monkeypatch.setattr("app.services.diary_rollups.mark_item_dirty", fake_mark_item_dirty)

    result = asyncio.run(
        generate_memory_diary_rollups(
            session,
            tenant_id="tenant-a",
            embedder=object(),
            llm=object(),
            target_day=date(2026, 4, 12),
        )
    )

    assert result.created == 0
    assert result.updated == 1
    assert result.unchanged == 0
    assert result.deactivated == 0
    assert processed == [existing.id]
    assert marked_dirty == [existing.id]
    assert existing.metadata_["diary_rollup"]["source_item_ids"] == [str(item.id) for item in source_items]


def test_generate_memory_diary_rollups_deactivates_rollup_without_sources(monkeypatch) -> None:
    key = DiaryRollupKey(day=date(2026, 4, 12), scope_type="workspace", scope_key="launch-pad")
    existing = _existing_rollup(key=key, source_items=[], source_ids=[])
    session = FakeSession([existing])
    marked_dirty = []

    async def fake_process_prebuilt_item(*args, **kwargs):
        raise AssertionError("missing-source rollup should be deactivated, not processed")

    async def fake_mark_item_dirty(_db, *, item_id, **kwargs):
        marked_dirty.append(item_id)
        return 3

    monkeypatch.setattr("app.services.diary_rollups.process_prebuilt_item", fake_process_prebuilt_item)
    monkeypatch.setattr("app.services.diary_rollups.mark_item_dirty", fake_mark_item_dirty)

    result = asyncio.run(
        generate_memory_diary_rollups(
            session,
            tenant_id="tenant-a",
            embedder=object(),
            llm=object(),
            target_day=date(2026, 4, 12),
        )
    )

    assert result.created == 0
    assert result.updated == 0
    assert result.unchanged == 0
    assert result.deactivated == 1
    assert existing.status == "failed"
    assert marked_dirty == [existing.id]


def test_run_diary_rollup_maintenance_replays_recent_days_for_each_tenant(monkeypatch) -> None:
    class FakeSession:
        pass

    class _SessionContext:
        async def __aenter__(self):
            return fake_session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    fake_session = FakeSession()
    calls = []

    async def fake_list_tenants(db):
        assert db is fake_session
        return ("tenant-a", "tenant-b")

    async def fake_generate(db, *, tenant_id, embedder, llm, target_day):
        assert db is fake_session
        calls.append((tenant_id, target_day, embedder, llm))
        return DiaryRollupBatchResult(created=0, updated=0, unchanged=1, deactivated=0)

    monkeypatch.setattr("app.workers.palace_tasks.async_session", lambda: _SessionContext())
    monkeypatch.setattr("app.workers.palace_tasks._list_diary_rollup_tenants", fake_list_tenants)
    monkeypatch.setattr("app.workers.palace_tasks.generate_memory_diary_rollups", fake_generate)
    monkeypatch.setattr(
        "app.workers.palace_tasks._diary_rollup_target_days",
        lambda: (date(2026, 4, 20), date(2026, 4, 21)),
    )

    ctx = {"embedder": object(), "llm": object()}
    asyncio.run(run_diary_rollup_maintenance(ctx))

    assert calls == [
        ("tenant-a", date(2026, 4, 20), ctx["embedder"], ctx["llm"]),
        ("tenant-a", date(2026, 4, 21), ctx["embedder"], ctx["llm"]),
        ("tenant-b", date(2026, 4, 20), ctx["embedder"], ctx["llm"]),
        ("tenant-b", date(2026, 4, 21), ctx["embedder"], ctx["llm"]),
    ]
