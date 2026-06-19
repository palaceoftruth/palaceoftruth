from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from app.database import Base
from app.models.item import Item
from app.models.job import Job
from app.models.source_subscription import SourceSubscription, SourceSubscriptionEntry
from app.schemas.source_subscription import SourceSubscriptionEntryOut
from app.services.source_subscriptions import (
    DEFAULT_SOURCE_SUBSCRIPTION_PROVIDER_REGISTRY,
    SourceSubscriptionProviderError,
    SourceSubscriptionProviderRegistry,
    SourceSubscriptionBackfillPolicy,
    YoutubeChannelSourceSubscriptionProvider,
    apply_entry_queue_state,
    build_source_entry_metadata,
    create_source_subscription,
    poll_source_subscription,
    queue_source_subscription_entry,
    diagnose_stale_queued_source_subscription_entries,
    reflect_source_subscription_entry_for_job,
    validate_source_subscription_tenant,
)
from app.workers.queues import DEFAULT_WORKER_QUEUE, MEDIA_FAIR_DISPATCH_TASK_NAME, singleton_job_id


class _FakeProvider:
    provider_type = "youtube_channel"


class _BadProvider:
    provider_type = ""


class _FakeYtDlp:
    def __init__(self, opts, responses, calls):
        self.opts = opts
        self.responses = responses
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def extract_info(self, url, download=False):
        self.calls.append({"url": url, "download": download, "opts": self.opts})
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


class _FakeResult:
    def __init__(self, value):
        self.value = value

    def scalars(self):
        return self

    def all(self):
        return self.value

    def scalar_one_or_none(self):
        return self.value

    def fetchall(self):
        return self.value


class _FakeDb:
    def __init__(self, *, return_existing_when_entries: bool = False, execute_result=None):
        self.added = []
        self.entries = []
        self.items = {}
        self.jobs = {}
        self.flushes = 0
        self.commits = 0
        self.return_existing_when_entries = return_existing_when_entries
        self.execute_result = execute_result

    def add(self, obj):
        self.added.append(obj)
        if isinstance(obj, SourceSubscriptionEntry):
            if obj.id is None:
                obj.id = uuid.uuid4()
            self.entries.append(obj)
        if isinstance(obj, Item):
            if obj.id is None:
                obj.id = uuid.uuid4()
            self.items[obj.id] = obj
        if isinstance(obj, Job):
            if obj.id is None:
                obj.id = uuid.uuid4()
            self.jobs[obj.id] = obj

    async def flush(self):
        self.flushes += 1

    async def execute(self, _statement):
        if self.execute_result is not None:
            return _FakeResult(self.execute_result)
        return _FakeResult(self.entries[0] if self.return_existing_when_entries and self.entries else None)

    async def get(self, model, key):
        if model is Item:
            return self.items.get(key)
        if model is Job:
            return self.jobs.get(key)
        if model is SourceSubscriptionEntry:
            return next((entry for entry in self.entries if entry.id == key), None)
        return None

    async def commit(self):
        self.commits += 1


class _FakeRedis:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.enqueued = []

    async def enqueue_job(self, name: str, **kwargs) -> None:
        if self.error is not None:
            raise self.error
        self.enqueued.append((name, kwargs))


def _yt_dlp_factory(responses, calls):
    return lambda opts: _FakeYtDlp(opts, responses, calls)


def _fixed_now():
    return datetime(2026, 5, 15, 1, 0, tzinfo=timezone.utc)


def test_source_subscription_models_register_tenant_scoped_tables_and_indexes() -> None:
    subscription_table = Base.metadata.tables["source_subscriptions"]
    entry_table = Base.metadata.tables["source_subscription_entries"]

    assert subscription_table.c.tenant_id.nullable is False
    assert subscription_table.c.deleted_at.nullable is True
    assert entry_table.c.tenant_id.nullable is False
    assert str(entry_table.c.metadata.type) == "JSONB"

    index_sql = {
        index.name: str(CreateIndex(index).compile(dialect=postgresql.dialect()))
        for index in (*subscription_table.indexes, *entry_table.indexes)
    }

    assert "ix_source_subscriptions_tenant_status" in index_sql
    assert "ix_source_subscription_entries_subscription_status" in index_sql
    assert (
        "WHERE deleted_at IS NULL AND external_id IS NOT NULL"
        in index_sql["uq_source_subscriptions_active_external"]
    )
    assert (
        "WHERE provider_entry_id IS NOT NULL"
        in index_sql["uq_source_subscription_entries_provider_entry"]
    )


def test_source_subscription_provider_registry_instantiates_registered_provider() -> None:
    registry = SourceSubscriptionProviderRegistry()

    registry.register(_FakeProvider)

    assert registry.available_provider_types() == ("youtube_channel",)
    assert isinstance(registry.create("youtube_channel"), _FakeProvider)


def test_default_source_subscription_provider_registry_includes_youtube_channel() -> None:
    provider = DEFAULT_SOURCE_SUBSCRIPTION_PROVIDER_REGISTRY.create("youtube_channel")

    assert isinstance(provider, YoutubeChannelSourceSubscriptionProvider)


def test_source_subscription_provider_registry_rejects_invalid_and_unknown_providers() -> None:
    registry = SourceSubscriptionProviderRegistry()

    with pytest.raises(ValueError, match="provider_type"):
        registry.register(_BadProvider)

    with pytest.raises(KeyError, match="unknown source subscription provider"):
        registry.create("missing")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_url", "expected_extract_url"),
    [
        ("@example", "https://www.youtube.com/@example/videos"),
        ("https://www.youtube.com/channel/UC123", "https://www.youtube.com/channel/UC123/videos"),
    ],
)
async def test_youtube_provider_resolves_handles_and_channel_urls_without_backfill(
    source_url,
    expected_extract_url,
) -> None:
    calls = []
    provider = YoutubeChannelSourceSubscriptionProvider(
        youtube_dl_factory=_yt_dlp_factory(
            {
                expected_extract_url: {
                    "id": "UC123",
                    "channel": "Example Channel",
                    "channel_url": "https://www.youtube.com/channel/UC123",
                    "channel_handle": "@example",
                    "entries": [
                        {"id": "old-video", "url": "https://www.youtube.com/watch?v=old-video"},
                    ],
                }
            },
            calls,
        ),
        now=_fixed_now,
    )

    resolved = await provider.resolve_source(source_url, tenant_id="tenant-a")

    assert resolved.provider_type == "youtube_channel"
    assert resolved.external_id == "UC123"
    assert resolved.external_url == "https://www.youtube.com/channel/UC123"
    assert resolved.display_name == "Example Channel"
    assert resolved.cursor == {
        "created_at": "2026-05-15T01:00:00+00:00",
        "no_backfill": True,
        "seen_provider_entry_ids": ["old-video"],
        "backfill": {
            "enabled": False,
            "limit": None,
            "remaining": None,
            "published_after": None,
            "completed": True,
        },
    }
    assert resolved.metadata["discovery_backend"] == "yt-dlp"
    assert resolved.metadata["youtube_channel_handle"] == "@example"
    assert calls[0]["download"] is False
    assert calls[0]["opts"]["extract_flat"] is True
    assert calls[0]["opts"]["playlistend"] == 50


@pytest.mark.asyncio
async def test_youtube_provider_resolves_channel_with_bounded_backfill_policy() -> None:
    provider = YoutubeChannelSourceSubscriptionProvider(
        youtube_dl_factory=_yt_dlp_factory(
            {
                "https://www.youtube.com/@example/videos": {
                    "id": "UC123",
                    "channel": "Example Channel",
                    "entries": [{"id": "existing"}],
                }
            },
            [],
        ),
        now=_fixed_now,
    )

    resolved = await provider.resolve_source(
        "@example",
        tenant_id="tenant-a",
        backfill_policy=SourceSubscriptionBackfillPolicy(enabled=True, limit=2),
    )

    assert resolved.cursor["no_backfill"] is False
    assert resolved.cursor["seen_provider_entry_ids"] == []
    assert resolved.cursor["backfill"] == {
        "enabled": True,
        "limit": 2,
        "remaining": 2,
        "published_after": None,
        "completed": False,
    }
    assert resolved.metadata["backfill_enabled"] is True
    assert resolved.metadata["backfill_limit"] == 2


@pytest.mark.asyncio
async def test_create_source_subscription_resolves_and_stores_channel_metadata() -> None:
    calls = []
    registry = SourceSubscriptionProviderRegistry()
    registry.register(
        YoutubeChannelSourceSubscriptionProvider(
            youtube_dl_factory=_yt_dlp_factory(
                {
                    "https://www.youtube.com/@example/videos": {
                        "id": "UC123",
                        "channel": "Example Channel",
                        "channel_url": "https://www.youtube.com/channel/UC123",
                        "entries": [{"id": "existing"}],
                    }
                },
                calls,
            ),
            now=_fixed_now,
        )
    )
    db = _FakeDb()

    subscription = await create_source_subscription(
        db,
        tenant_id="tenant-a",
        provider_type="youtube_channel",
        source_url="@example",
        auto_tags=["research"],
        registry=registry,
    )

    assert subscription.tenant_id == "tenant-a"
    assert subscription.provider_type == "youtube_channel"
    assert subscription.external_id == "UC123"
    assert subscription.external_url == "https://www.youtube.com/channel/UC123"
    assert subscription.display_name == "Example Channel"
    assert subscription.auto_tags == ["research"]
    assert subscription.cursor["no_backfill"] is True
    assert subscription.cursor["seen_provider_entry_ids"] == ["existing"]
    assert subscription.provider_metadata["youtube_channel_id"] == "UC123"
    assert db.added == [subscription]
    assert db.entries == []


@pytest.mark.asyncio
async def test_create_source_subscription_stores_backfill_policy() -> None:
    registry = SourceSubscriptionProviderRegistry()
    registry.register(
        YoutubeChannelSourceSubscriptionProvider(
            youtube_dl_factory=_yt_dlp_factory(
                {
                    "https://www.youtube.com/@example/videos": {
                        "id": "UC123",
                        "channel": "Example Channel",
                        "entries": [{"id": "existing"}],
                    }
                },
                [],
            ),
            now=_fixed_now,
        )
    )
    db = _FakeDb()

    subscription = await create_source_subscription(
        db,
        tenant_id="tenant-a",
        provider_type="youtube_channel",
        source_url="@example",
        backfill_enabled=True,
        backfill_limit=3,
        registry=registry,
    )

    assert subscription.cursor["no_backfill"] is False
    assert subscription.cursor["seen_provider_entry_ids"] == []
    assert subscription.cursor["backfill"]["remaining"] == 3


@pytest.mark.asyncio
async def test_youtube_subscription_poll_discovers_future_uploads_and_skips_non_uploads() -> None:
    calls = []
    provider = YoutubeChannelSourceSubscriptionProvider(
        youtube_dl_factory=_yt_dlp_factory(
            {
                "https://www.youtube.com/channel/UC123/videos": {
                    "id": "UC123",
                    "entries": [
                        {
                            "id": "old-video",
                            "url": "https://www.youtube.com/watch?v=old-video",
                            "title": "Old upload",
                            "upload_date": "20260514",
                        },
                        {
                            "id": "normal-video",
                            "url": "https://www.youtube.com/watch?v=normal-video",
                            "title": "New normal upload",
                            "upload_date": "20260516",
                        },
                        {
                            "id": "short-video",
                            "url": "https://www.youtube.com/shorts/short-video",
                            "title": "Short",
                            "upload_date": "20260516",
                        },
                        {
                            "id": "live-video",
                            "url": "https://www.youtube.com/watch?v=live-video",
                            "title": "Replay",
                            "upload_date": "20260516",
                            "live_status": "was_live",
                        },
                    ],
                }
            },
            calls,
        ),
        now=_fixed_now,
    )
    registry = SourceSubscriptionProviderRegistry()
    registry.register(provider)
    subscription = SourceSubscription(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        provider_type="youtube_channel",
        source_url="https://www.youtube.com/@example",
        external_id="UC123",
        external_url="https://www.youtube.com/channel/UC123",
        display_name="Example Channel",
        status="active",
        cursor={"created_at": "2026-05-15T01:00:00+00:00", "no_backfill": True},
    )
    db = _FakeDb()

    entries = await poll_source_subscription(db, subscription, registry=registry, checked_at=_fixed_now())

    assert [entry.provider_entry_id for entry in entries] == ["normal-video", "short-video", "live-video"]
    assert [entry.status for entry in entries] == ["discovered", "skipped", "skipped"]
    assert entries[1].skip_reason == "youtube_shorts_unsupported"
    assert entries[2].skip_reason == "youtube_live_unsupported"
    assert entries[0].metadata_["youtube_video_id"] == "normal-video"
    assert subscription.last_checked_at == _fixed_now()
    assert subscription.last_discovered_at == _fixed_now()
    assert subscription.last_error is None
    assert subscription.consecutive_failures == 0
    assert subscription.cursor["no_backfill"] is True
    assert "normal-video" in subscription.cursor["seen_provider_entry_ids"]
    assert "old-video" not in subscription.cursor["seen_provider_entry_ids"]
    assert calls[0]["opts"]["playlistend"] == 50


@pytest.mark.asyncio
async def test_youtube_subscription_poll_backfills_with_limit_and_then_marks_complete() -> None:
    provider = YoutubeChannelSourceSubscriptionProvider(
        youtube_dl_factory=_yt_dlp_factory(
            {
                "https://www.youtube.com/channel/UC123/videos": {
                    "id": "UC123",
                    "entries": [
                        {
                            "id": "old-one",
                            "url": "https://www.youtube.com/watch?v=old-one",
                            "title": "Old one",
                            "upload_date": "20260501",
                        },
                        {
                            "id": "old-two",
                            "url": "https://www.youtube.com/watch?v=old-two",
                            "title": "Old two",
                            "upload_date": "20260502",
                        },
                        {
                            "id": "old-three",
                            "url": "https://www.youtube.com/watch?v=old-three",
                            "title": "Old three",
                            "upload_date": "20260503",
                        },
                    ],
                }
            },
            [],
        ),
        now=_fixed_now,
    )
    registry = SourceSubscriptionProviderRegistry()
    registry.register(provider)
    subscription = SourceSubscription(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        provider_type="youtube_channel",
        source_url="https://www.youtube.com/@example",
        external_id="UC123",
        external_url="https://www.youtube.com/channel/UC123",
        status="active",
        cursor={
            "created_at": "2026-05-15T01:00:00+00:00",
            "no_backfill": False,
            "seen_provider_entry_ids": [],
            "backfill": {"enabled": True, "limit": 2, "remaining": 2, "published_after": None, "completed": False},
        },
    )
    db = _FakeDb()

    entries = await poll_source_subscription(db, subscription, registry=registry, checked_at=_fixed_now())

    assert [entry.provider_entry_id for entry in entries] == ["old-one", "old-two"]
    assert subscription.cursor["backfill"]["remaining"] == 0
    assert subscription.cursor["backfill"]["enabled"] is False
    assert subscription.cursor["backfill"]["completed"] is True
    assert subscription.cursor["no_backfill"] is True


@pytest.mark.asyncio
async def test_youtube_subscription_poll_completes_when_provider_has_fewer_uploads_than_limit() -> None:
    available_uploads = [
        {
            "id": f"old-{index:02d}",
            "url": f"https://www.youtube.com/watch?v=old-{index:02d}",
            "title": f"Old {index:02d}",
            "upload_date": "20260501",
        }
        for index in range(1, 28)
    ]
    responses = {
        "https://www.youtube.com/channel/UC123/videos": {
            "id": "UC123",
            "entries": available_uploads,
        }
    }
    calls = []
    provider = YoutubeChannelSourceSubscriptionProvider(
        youtube_dl_factory=_yt_dlp_factory(responses, calls),
        now=_fixed_now,
    )
    registry = SourceSubscriptionProviderRegistry()
    registry.register(provider)
    subscription = SourceSubscription(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        provider_type="youtube_channel",
        source_url="https://www.youtube.com/@example",
        external_id="UC123",
        external_url="https://www.youtube.com/channel/UC123",
        status="active",
        cursor={
            "created_at": "2026-05-15T01:00:00+00:00",
            "no_backfill": False,
            "seen_provider_entry_ids": [],
            "backfill": {"enabled": True, "limit": 100, "remaining": 100, "published_after": None, "completed": False},
        },
    )
    db = _FakeDb()

    entries = await poll_source_subscription(db, subscription, registry=registry, checked_at=_fixed_now())

    assert len(entries) == 27
    assert subscription.cursor["backfill"]["remaining"] == 73
    assert subscription.cursor["backfill"]["enabled"] is False
    assert subscription.cursor["backfill"]["completed"] is True
    assert subscription.cursor["no_backfill"] is True
    assert calls[-1]["opts"]["playlistend"] == 100

    responses["https://www.youtube.com/channel/UC123/videos"] = {
        "id": "UC123",
        "entries": [
            {
                "id": "new-video",
                "url": "https://www.youtube.com/watch?v=new-video",
                "title": "New upload",
                "upload_date": "20260516",
            },
            *available_uploads,
        ],
    }

    watch_entries = await poll_source_subscription(db, subscription, registry=registry, checked_at=_fixed_now())

    assert [entry.provider_entry_id for entry in watch_entries] == ["new-video"]
    assert subscription.cursor["backfill"]["remaining"] == 73
    assert subscription.cursor["backfill"]["enabled"] is False
    assert subscription.cursor["backfill"]["completed"] is True
    assert calls[-1]["opts"]["playlistend"] == 50


@pytest.mark.asyncio
async def test_youtube_subscription_bounded_backfill_stops_undated_entries_after_boundary() -> None:
    responses = {
        "https://www.youtube.com/channel/UC123/videos": {
            "id": "UC123",
            "entries": [
                {"id": "old-one", "url": "https://www.youtube.com/watch?v=old-one", "title": "Old one"},
                {"id": "old-two", "url": "https://www.youtube.com/watch?v=old-two", "title": "Old two"},
                {"id": "old-three", "url": "https://www.youtube.com/watch?v=old-three", "title": "Old three"},
            ],
        }
    }
    calls = []
    provider = YoutubeChannelSourceSubscriptionProvider(
        youtube_dl_factory=_yt_dlp_factory(responses, calls),
        now=_fixed_now,
    )
    registry = SourceSubscriptionProviderRegistry()
    registry.register(provider)
    subscription = SourceSubscription(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        provider_type="youtube_channel",
        source_url="https://www.youtube.com/@example",
        external_id="UC123",
        external_url="https://www.youtube.com/channel/UC123",
        status="active",
        cursor={
            "created_at": "2026-05-15T01:00:00+00:00",
            "no_backfill": False,
            "seen_provider_entry_ids": [],
            "backfill": {"enabled": True, "limit": 2, "remaining": 2, "published_after": None, "completed": False},
        },
    )
    db = _FakeDb()

    backfill_entries = await poll_source_subscription(db, subscription, registry=registry, checked_at=_fixed_now())

    assert [entry.provider_entry_id for entry in backfill_entries] == ["old-one", "old-two"]
    assert subscription.cursor["backfill"]["boundary_provider_entry_id"] == "old-two"
    assert subscription.cursor["backfill"]["completed"] is True
    responses["https://www.youtube.com/channel/UC123/videos"] = {
        "id": "UC123",
        "entries": [
            {"id": "new-video", "url": "https://www.youtube.com/watch?v=new-video", "title": "New upload"},
            {"id": "old-one", "url": "https://www.youtube.com/watch?v=old-one", "title": "Old one"},
            {"id": "old-two", "url": "https://www.youtube.com/watch?v=old-two", "title": "Old two"},
            {"id": "old-three", "url": "https://www.youtube.com/watch?v=old-three", "title": "Old three"},
        ],
    }

    first_watch_entries = await poll_source_subscription(db, subscription, registry=registry, checked_at=_fixed_now())
    second_watch_entries = await poll_source_subscription(db, subscription, registry=registry, checked_at=_fixed_now())

    assert [entry.provider_entry_id for entry in first_watch_entries] == ["new-video"]
    assert second_watch_entries == []
    assert "old-three" not in {entry.provider_entry_id for entry in db.entries}
    assert subscription.cursor["backfill"]["boundary_provider_entry_id"] == "old-two"
    assert subscription.cursor["backfill"]["enabled"] is False
    assert calls[-1]["opts"]["playlistend"] == 50


@pytest.mark.asyncio
async def test_youtube_subscription_poll_backfills_since_date_once_then_returns_to_watch_mode() -> None:
    provider = YoutubeChannelSourceSubscriptionProvider(
        youtube_dl_factory=_yt_dlp_factory(
            {
                "https://www.youtube.com/channel/UC123/videos": {
                    "id": "UC123",
                    "entries": [
                        {
                            "id": "inside-window",
                            "url": "https://www.youtube.com/watch?v=inside-window",
                            "upload_date": "20260510",
                        },
                        {
                            "id": "outside-window",
                            "url": "https://www.youtube.com/watch?v=outside-window",
                            "upload_date": "20260501",
                        },
                    ],
                }
            },
            [],
        ),
        now=_fixed_now,
    )
    registry = SourceSubscriptionProviderRegistry()
    registry.register(provider)
    subscription = SourceSubscription(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        provider_type="youtube_channel",
        source_url="https://www.youtube.com/@example",
        external_id="UC123",
        external_url="https://www.youtube.com/channel/UC123",
        status="active",
        cursor={
            "created_at": "2026-05-15T01:00:00+00:00",
            "no_backfill": False,
            "seen_provider_entry_ids": [],
            "backfill": {
                "enabled": True,
                "limit": None,
                "remaining": None,
                "published_after": "2026-05-05T00:00:00+00:00",
                "completed": False,
            },
        },
    )
    db = _FakeDb()

    entries = await poll_source_subscription(db, subscription, registry=registry, checked_at=_fixed_now())

    assert [entry.provider_entry_id for entry in entries] == ["inside-window"]
    assert subscription.cursor["backfill"]["enabled"] is False
    assert subscription.cursor["backfill"]["completed"] is True
    assert subscription.cursor["no_backfill"] is True


@pytest.mark.asyncio
async def test_youtube_subscription_poll_is_idempotent_for_duplicate_entries() -> None:
    provider = YoutubeChannelSourceSubscriptionProvider(
        youtube_dl_factory=_yt_dlp_factory(
            {
                "https://www.youtube.com/channel/UC123/videos": {
                    "id": "UC123",
                    "entries": [
                        {
                            "id": "normal-video",
                            "url": "https://www.youtube.com/watch?v=normal-video",
                            "title": "New normal upload",
                            "upload_date": "20260516",
                        },
                        {
                            "id": "normal-video",
                            "url": "https://www.youtube.com/watch?v=normal-video",
                            "title": "Duplicate normal upload",
                            "upload_date": "20260516",
                        },
                    ],
                }
            },
            [],
        ),
        now=_fixed_now,
    )
    registry = SourceSubscriptionProviderRegistry()
    registry.register(provider)
    subscription = SourceSubscription(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        provider_type="youtube_channel",
        source_url="https://www.youtube.com/@example",
        external_id="UC123",
        external_url="https://www.youtube.com/channel/UC123",
        status="active",
        cursor={"created_at": "2026-05-15T01:00:00+00:00", "no_backfill": True},
    )
    db = _FakeDb(return_existing_when_entries=True)

    first_poll_entries = await poll_source_subscription(db, subscription, registry=registry, checked_at=_fixed_now())
    second_poll_entries = await poll_source_subscription(db, subscription, registry=registry, checked_at=_fixed_now())

    assert [entry.provider_entry_id for entry in first_poll_entries] == ["normal-video"]
    assert second_poll_entries == []
    assert [entry.provider_entry_id for entry in db.entries] == ["normal-video"]


@pytest.mark.asyncio
async def test_youtube_subscription_poll_records_failure_state_without_deleting_entries() -> None:
    provider = YoutubeChannelSourceSubscriptionProvider(
        youtube_dl_factory=_yt_dlp_factory(
            {"https://www.youtube.com/channel/UC123/videos": RuntimeError("yt-dlp timeout")},
            [],
        ),
        now=_fixed_now,
    )
    registry = SourceSubscriptionProviderRegistry()
    registry.register(provider)
    subscription = SourceSubscription(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        provider_type="youtube_channel",
        source_url="https://www.youtube.com/@example",
        external_id="UC123",
        external_url="https://www.youtube.com/channel/UC123",
        status="active",
        cursor={"created_at": "2026-05-15T01:00:00+00:00", "no_backfill": True},
        consecutive_failures=1,
    )
    existing_entry = SourceSubscriptionEntry(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        subscription_id=subscription.id,
        provider_entry_id="already-there",
        source_url="https://www.youtube.com/watch?v=already-there",
    )
    db = _FakeDb()
    db.entries.append(existing_entry)

    entries = await poll_source_subscription(db, subscription, registry=registry, checked_at=_fixed_now())

    assert entries == []
    assert db.entries == [existing_entry]
    assert subscription.last_checked_at == _fixed_now()
    assert subscription.consecutive_failures == 2
    assert "YouTube channel discovery failed" in subscription.last_error


@pytest.mark.asyncio
async def test_youtube_subscription_poll_auto_pauses_after_max_failures(monkeypatch) -> None:
    monkeypatch.setattr("app.services.source_subscriptions.settings.source_subscription_max_failures", 2)
    provider = YoutubeChannelSourceSubscriptionProvider(
        youtube_dl_factory=_yt_dlp_factory(
            {"https://www.youtube.com/channel/UC123/videos": RuntimeError("yt-dlp timeout")},
            [],
        ),
        now=_fixed_now,
    )
    registry = SourceSubscriptionProviderRegistry()
    registry.register(provider)
    subscription = SourceSubscription(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        provider_type="youtube_channel",
        source_url="https://www.youtube.com/@example",
        external_id="UC123",
        external_url="https://www.youtube.com/channel/UC123",
        status="active",
        cursor={"created_at": "2026-05-15T01:00:00+00:00", "no_backfill": True},
        consecutive_failures=1,
    )
    db = _FakeDb()

    entries = await poll_source_subscription(db, subscription, registry=registry, checked_at=_fixed_now())

    assert entries == []
    assert subscription.status == "paused"
    assert subscription.paused_reason == "max_discovery_failures"
    assert subscription.consecutive_failures == 2


@pytest.mark.asyncio
async def test_youtube_provider_raises_provider_error_when_channel_cannot_resolve() -> None:
    provider = YoutubeChannelSourceSubscriptionProvider(
        youtube_dl_factory=_yt_dlp_factory({"https://www.youtube.com/@missing/videos": {"entries": []}}, []),
        now=_fixed_now,
    )

    with pytest.raises(SourceSubscriptionProviderError, match="stable YouTube channel id"):
        await provider.resolve_source("@missing", tenant_id="tenant-a")


def test_source_entry_metadata_and_queue_state_preserve_traceability() -> None:
    subscription_id = uuid.uuid4()
    entry_id = uuid.uuid4()
    item_id = uuid.uuid4()
    job_id = uuid.uuid4()
    queued_at = datetime.now(timezone.utc)
    subscription = SourceSubscription(
        id=subscription_id,
        tenant_id="tenant-a",
        provider_type="youtube_channel",
        source_url="https://www.youtube.com/@example",
        external_id="UC123",
    )
    entry = SourceSubscriptionEntry(
        id=entry_id,
        tenant_id="tenant-a",
        subscription_id=subscription_id,
        provider_entry_id="video-123",
        source_url="https://www.youtube.com/watch?v=video-123",
        status="discovered",
        error_message="old failure",
    )

    metadata = build_source_entry_metadata(
        subscription=subscription,
        entry=entry,
        provider_metadata={"youtube_channel_name": "Example", "youtube_video_id": "video-123"},
    )
    apply_entry_queue_state(entry, item_id=item_id, job_id=job_id, queued_at=queued_at)

    assert metadata == {
        "capture_origin": "source_subscription",
        "subscription_id": str(subscription_id),
        "subscription_entry_id": str(entry_id),
        "source_provider_type": "youtube_channel",
        "source_external_id": "UC123",
        "source_provider_entry_id": "video-123",
        "youtube_channel_name": "Example",
        "youtube_video_id": "video-123",
    }
    assert entry.status == "queued"
    assert entry.item_id == item_id
    assert entry.job_id == job_id
    assert entry.queued_at == queued_at
    assert entry.error_message is None


@pytest.mark.asyncio
async def test_diagnose_stale_queued_source_subscription_entries_marks_missing_job_failed() -> None:
    queued_at = datetime(2026, 5, 15, 0, 0, tzinfo=timezone.utc)
    entry = SourceSubscriptionEntry(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        subscription_id=uuid.uuid4(),
        provider_entry_id="video-123",
        source_url="https://www.youtube.com/watch?v=video-123",
        status="queued",
        queued_at=queued_at,
    )
    db = _FakeDb(execute_result=[entry])

    diagnosed = await diagnose_stale_queued_source_subscription_entries(
        db,
        now=datetime(2026, 5, 15, 2, 0, tzinfo=timezone.utc),
    )

    assert diagnosed == 1
    assert entry.status == "failed"
    assert entry.failed_at == datetime(2026, 5, 15, 2, 0, tzinfo=timezone.utc)
    assert entry.error_message == "Queued source subscription entry has no ingest job"
    assert db.commits == 1


@pytest.mark.asyncio
async def test_queue_source_subscription_entry_creates_media_item_job_and_enqueues_capture() -> None:
    subscription = SourceSubscription(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        provider_type="youtube_channel",
        source_url="https://www.youtube.com/@example",
        external_id="UC123",
        display_name="Example Channel",
        auto_tags=["research"],
    )
    entry = SourceSubscriptionEntry(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        subscription_id=subscription.id,
        provider_entry_id="video-123",
        source_url="https://www.youtube.com/watch?v=video-123",
        title="New upload",
        status="discovered",
        metadata_={
            "youtube_channel_id": "UC123",
            "youtube_channel_name": "Example Channel",
            "youtube_video_id": "video-123",
            "published_at": "2026-05-15T01:00:00+00:00",
        },
    )
    db = _FakeDb()
    redis = _FakeRedis()

    queued = await queue_source_subscription_entry(db, redis, subscription, entry, queued_at=_fixed_now())

    assert queued is True
    item = next(obj for obj in db.added if isinstance(obj, Item))
    job = next(obj for obj in db.added if isinstance(obj, Job))
    assert item.source_type == "media"
    assert item.source_url == "https://www.youtube.com/watch?v=video-123"
    assert item.title == "New upload"
    assert item.tags == ["research"]
    assert item.metadata_["capture_origin"] == "source_subscription"
    assert item.metadata_["subscription_entry_id"] == str(entry.id)
    assert item.metadata_["youtube_video_id"] == "video-123"
    assert job.item_id == item.id
    assert job.job_type == "media"
    assert job.payload == {
        "retry_task": {
            "name": "process_media",
            "kwargs": {"url": "https://www.youtube.com/watch?v=video-123"},
        }
    }
    assert entry.status == "queued"
    assert entry.item_id == item.id
    assert entry.job_id == job.id
    assert entry.queued_at == _fixed_now()
    assert redis.enqueued == [
        (
            MEDIA_FAIR_DISPATCH_TASK_NAME,
            {
                "_queue_name": DEFAULT_WORKER_QUEUE,
                "_job_id": singleton_job_id(MEDIA_FAIR_DISPATCH_TASK_NAME, "media"),
            },
        )
    ]


@pytest.mark.asyncio
async def test_queue_source_subscription_entry_skips_duplicate_source_url() -> None:
    existing_item = Item(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        source_type="media",
        source_url="https://www.youtube.com/watch?v=video-123",
        title="Existing upload",
        status="ready",
    )
    subscription = SourceSubscription(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        provider_type="youtube_channel",
        source_url="https://www.youtube.com/@example",
        external_id="UC123",
    )
    entry = SourceSubscriptionEntry(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        subscription_id=subscription.id,
        provider_entry_id="video-123",
        source_url=existing_item.source_url,
        status="discovered",
    )
    db = _FakeDb(execute_result=existing_item)
    redis = _FakeRedis()

    queued = await queue_source_subscription_entry(db, redis, subscription, entry, queued_at=_fixed_now())

    assert queued is False
    assert entry.status == "skipped"
    assert entry.skip_reason == "duplicate_source_url"
    assert entry.item_id == existing_item.id
    assert redis.enqueued == []


@pytest.mark.asyncio
async def test_queue_source_subscription_entry_records_enqueue_failure() -> None:
    subscription = SourceSubscription(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        provider_type="youtube_channel",
        source_url="https://www.youtube.com/@example",
        external_id="UC123",
    )
    entry = SourceSubscriptionEntry(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        subscription_id=subscription.id,
        provider_entry_id="video-123",
        source_url="https://www.youtube.com/watch?v=video-123",
        status="discovered",
    )
    db = _FakeDb()
    redis = _FakeRedis(error=RuntimeError("redis unavailable"))

    queued = await queue_source_subscription_entry(db, redis, subscription, entry, queued_at=_fixed_now())

    item = next(obj for obj in db.added if isinstance(obj, Item))
    job = next(obj for obj in db.added if isinstance(obj, Job))
    assert queued is False
    assert entry.status == "failed"
    assert "Failed to enqueue ingest task" in entry.error_message
    assert item.status == "failed"
    assert job.status == "failed"
    assert "redis unavailable" in job.error_message


@pytest.mark.asyncio
async def test_reflect_source_subscription_entry_for_completed_job_marks_captured() -> None:
    job_id = uuid.uuid4()
    item_id = uuid.uuid4()
    entry = SourceSubscriptionEntry(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        subscription_id=uuid.uuid4(),
        status="queued",
        job_id=job_id,
        item_id=item_id,
    )
    db = _FakeDb(execute_result=entry)
    db.entries.append(entry)
    db.items[item_id] = Item(
        id=item_id,
        tenant_id="tenant-a",
        source_type="media",
        source_url="https://www.youtube.com/watch?v=video-123",
        title="New upload",
        status="ready",
    )
    db.jobs[job_id] = Job(
        id=job_id,
        item_id=item_id,
        tenant_id="tenant-a",
        job_type="media",
        status="completed",
    )

    reflected = await reflect_source_subscription_entry_for_job(db, job_id=job_id, completed_at=_fixed_now())

    assert reflected is entry
    assert entry.status == "captured"
    assert entry.captured_at == _fixed_now()
    assert entry.error_message is None


@pytest.mark.asyncio
async def test_reflect_source_subscription_entry_for_duplicate_job_marks_skipped() -> None:
    job_id = uuid.uuid4()
    duplicate_item_id = uuid.uuid4()
    entry = SourceSubscriptionEntry(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        subscription_id=uuid.uuid4(),
        status="queued",
        job_id=job_id,
    )
    db = _FakeDb(execute_result=entry)
    db.entries.append(entry)
    db.jobs[job_id] = Job(
        id=job_id,
        tenant_id="tenant-a",
        job_type="media",
        status="duplicate",
        duplicate_of=duplicate_item_id,
    )

    await reflect_source_subscription_entry_for_job(db, job_id=job_id, completed_at=_fixed_now())

    assert entry.status == "skipped"
    assert entry.skip_reason == "duplicate_content"
    assert entry.item_id == duplicate_item_id
    assert entry.skipped_at == _fixed_now()


@pytest.mark.asyncio
async def test_reflect_source_subscription_entry_for_failed_job_marks_failed() -> None:
    job_id = uuid.uuid4()
    entry = SourceSubscriptionEntry(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        subscription_id=uuid.uuid4(),
        status="queued",
        job_id=job_id,
    )
    db = _FakeDb(execute_result=entry)
    db.entries.append(entry)
    db.jobs[job_id] = Job(
        id=job_id,
        tenant_id="tenant-a",
        job_type="media",
        status="failed",
        error_message="transcription failed",
    )

    await reflect_source_subscription_entry_for_job(db, job_id=job_id, completed_at=_fixed_now())

    assert entry.status == "failed"
    assert entry.error_message == "transcription failed"
    assert entry.failed_at == _fixed_now()


def test_source_subscription_entry_schema_exposes_metadata_field() -> None:
    now = datetime.now(timezone.utc)
    entry = SourceSubscriptionEntry(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        subscription_id=uuid.uuid4(),
        source_url="https://www.youtube.com/watch?v=video-123",
        discovered_at=now,
        status="discovered",
        metadata_={"capture_origin": "source_subscription"},
        created_at=now,
        updated_at=now,
    )

    payload = SourceSubscriptionEntryOut.model_validate(entry).model_dump()

    assert payload["metadata"] == {"capture_origin": "source_subscription"}
    assert "metadata_" not in payload


def test_source_subscription_tenant_validation_rejects_cross_tenant_entries() -> None:
    subscription = SourceSubscription(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        provider_type="youtube_channel",
        source_url="https://www.youtube.com/@example",
    )
    entry = SourceSubscriptionEntry(
        id=uuid.uuid4(),
        tenant_id="tenant-b",
        subscription_id=subscription.id,
        source_url="https://www.youtube.com/watch?v=video-123",
    )

    with pytest.raises(ValueError, match="tenant mismatch"):
        validate_source_subscription_tenant(subscription, entry)
