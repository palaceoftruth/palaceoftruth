from __future__ import annotations

import importlib
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import select

from app.services.palace import PalaceArtifactRepairPlan
from app.models.item import Item
from app.models.palace import PalaceRun, PalaceTenantState
from app.services.palace import PalaceIndexIntegrityPlan
from app.workers.palace_tasks import _enqueue_missing_embedding_repairs, mark_items_dirty_and_schedule, palace_run_build, poll_sync_sources, recover_palace_backlog, refresh_caught_up_wakeup_briefs, refresh_dirty_palace_rooms, refresh_palace_consolidation_candidates, repair_palace_artifacts, recompute_palace_tunnel_strengths, run_fact_registry_contradiction_sweep, run_fact_registry_extraction, run_memory_dream_refresh, run_palace_maintenance, run_wakeup_story_refresh, sweep_palace_index_integrity, watch_local_sync_sources_once
from app.workers.queues import MEDIA_WORKER_QUEUE, PALACE_WORKER_QUEUE
from app.workers.worker import MediaWorkerSettings, PalaceWorkerSettings, WorkerSettings


class FakeRows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class FakeDb:
    def __init__(self, states, items=None) -> None:
        self.states = states
        self.items = {item.id: item for item in (items or [])}
        self.commits = 0

    async def execute(self, _statement):
        return FakeRows(self.states)

    async def get(self, model, key):
        if model is PalaceTenantState:
            for state in self.states:
                if getattr(state, "tenant_id", None) == key:
                    return state
        if model is Item:
            return self.items.get(key)
        return None

    async def commit(self):
        self.commits += 1


class PalaceRunDb:
    def __init__(self, *, run: SimpleNamespace | None, state: SimpleNamespace | None) -> None:
        self.run = run
        self.state = state

    async def get(self, model, key):
        if model is PalaceRun and self.run and key == self.run.id:
            return self.run
        if model is PalaceTenantState and self.state and key == self.state.tenant_id:
            return self.state
        return None


class SessionFactory:
    def __init__(self, session) -> None:
        self.session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeRedis:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict]] = []

    async def enqueue_job(self, name: str, **kwargs) -> None:
        self.enqueued.append((name, kwargs))


@pytest.mark.asyncio
async def test_recover_palace_backlog_enqueues_runs_for_backlogged_tenants(monkeypatch) -> None:
    tenant_a = SimpleNamespace(tenant_id="tenant-a", dirty_generation=5, indexed_generation=4)
    tenant_b = SimpleNamespace(tenant_id="tenant-b", dirty_generation=9, indexed_generation=7)
    redis = FakeRedis()
    created_runs: list[tuple[str, str]] = []

    async def fake_create_or_get_palace_run(db, *, tenant_id: str, triggered_by: str, source_sync_run_id=None):
        assert source_sync_run_id is None
        assert triggered_by == "maintenance"
        run_id = uuid.uuid4()
        created_runs.append((tenant_id, str(run_id)))
        return SimpleNamespace(id=run_id, requested_generation=4), True

    monkeypatch.setattr("app.workers.palace_tasks.async_session", SessionFactory(FakeDb([tenant_a, tenant_b])))
    monkeypatch.setattr("app.workers.palace_tasks.create_or_get_palace_run", fake_create_or_get_palace_run)

    await recover_palace_backlog({"redis": redis})

    assert [tenant_id for tenant_id, _run_id in created_runs] == ["tenant-a", "tenant-b"]
    assert redis.enqueued == [
        ("palace_run_build", {"_queue_name": PALACE_WORKER_QUEUE, "palace_run_id": created_runs[0][1]}),
        ("palace_run_build", {"_queue_name": PALACE_WORKER_QUEUE, "palace_run_id": created_runs[1][1]}),
    ]


@pytest.mark.asyncio
async def test_mark_items_dirty_and_schedule_coalesces_one_palace_run(monkeypatch) -> None:
    redis = FakeRedis()
    item_ids = [uuid.uuid4(), uuid.uuid4()]
    marked_batches: list[tuple[str, tuple[uuid.UUID, ...], str]] = []

    class DirtyBatchDb:
        async def execute(self, _statement):
            return FakeRows(item_ids)

        async def commit(self):
            return None

    async def fake_mark_items_dirty(db, *, tenant_id: str, item_ids, reason: str, sync_source_id=None):
        assert sync_source_id is None
        marked_batches.append((tenant_id, tuple(item_ids), reason))
        return 12

    async def fake_create_or_get_palace_run(db, *, tenant_id: str, triggered_by: str, source_sync_run_id=None):
        assert tenant_id == "tenant-a"
        assert triggered_by == "auto"
        assert source_sync_run_id is None
        return SimpleNamespace(id=uuid.UUID("00000000-0000-0000-0000-000000000123")), True

    monkeypatch.setattr("app.workers.palace_tasks.async_session", SessionFactory(DirtyBatchDb()))
    monkeypatch.setattr("app.workers.palace_tasks.mark_items_dirty", fake_mark_items_dirty)
    monkeypatch.setattr("app.workers.palace_tasks.create_or_get_palace_run", fake_create_or_get_palace_run)

    await mark_items_dirty_and_schedule(
        {"redis": redis},
        item_ids=[str(item_ids[0]), str(item_ids[1]), str(item_ids[1])],
        tenant_id="tenant-a",
        reason="bulk-import",
    )

    assert marked_batches == [("tenant-a", tuple(item_ids), "bulk-import")]
    assert redis.enqueued == [
        (
            "palace_run_build",
            {
                "_queue_name": PALACE_WORKER_QUEUE,
                "palace_run_id": "00000000-0000-0000-0000-000000000123",
            },
        )
    ]


@pytest.mark.asyncio
async def test_recover_palace_backlog_skips_enqueue_when_run_already_exists(monkeypatch) -> None:
    redis = FakeRedis()

    async def fake_create_or_get_palace_run(db, *, tenant_id: str, triggered_by: str, source_sync_run_id=None):
        assert tenant_id == "tenant-a"
        assert triggered_by == "maintenance"
        return SimpleNamespace(id=uuid.uuid4(), requested_generation=2), False

    monkeypatch.setattr(
        "app.workers.palace_tasks.async_session",
        SessionFactory(FakeDb([SimpleNamespace(tenant_id="tenant-a", dirty_generation=3, indexed_generation=2)])),
    )
    monkeypatch.setattr("app.workers.palace_tasks.create_or_get_palace_run", fake_create_or_get_palace_run)

    await recover_palace_backlog({"redis": redis})

    assert redis.enqueued == []


@pytest.mark.asyncio
async def test_refresh_dirty_palace_rooms_marks_missing_memberships_and_enqueues_run(monkeypatch) -> None:
    item_a = uuid.uuid4()
    item_b = uuid.uuid4()
    run_id = uuid.uuid4()
    tenant = SimpleNamespace(
        tenant_id="tenant-a",
        indexed_generation=4,
        active_palace_run_id=None,
        updated_at=datetime.now(timezone.utc),
    )
    redis = FakeRedis()
    marked_dirty: list[tuple[str, uuid.UUID, str]] = []

    class DirtyRoomDb(FakeDb):
        def __init__(self) -> None:
            super().__init__([tenant])
            self.execute_calls = 0

        async def execute(self, _statement):
            self.execute_calls += 1
            if self.execute_calls == 1:
                return FakeRows([tenant])
            return FakeRows([item_a, item_b])

    async def fake_mark_item_dirty(_db, *, tenant_id: str, item_id: uuid.UUID, reason: str, sync_source_id=None):
        assert sync_source_id is None
        marked_dirty.append((tenant_id, item_id, reason))
        return 6

    async def fake_create_or_get_palace_run(_db, *, tenant_id: str, triggered_by: str, source_sync_run_id=None):
        assert source_sync_run_id is None
        assert tenant_id == "tenant-a"
        assert triggered_by == "maintenance"
        return SimpleNamespace(id=run_id, requested_generation=6), True

    monkeypatch.setattr("app.workers.palace_tasks.async_session", SessionFactory(DirtyRoomDb()))
    monkeypatch.setattr("app.workers.palace_tasks.mark_item_dirty", fake_mark_item_dirty)
    monkeypatch.setattr("app.workers.palace_tasks.create_or_get_palace_run", fake_create_or_get_palace_run)

    await refresh_dirty_palace_rooms({"redis": redis})

    assert marked_dirty == [
        ("tenant-a", item_a, "maintenance"),
        ("tenant-a", item_b, "maintenance"),
    ]
    assert redis.enqueued == [
        ("palace_run_build", {"_queue_name": PALACE_WORKER_QUEUE, "palace_run_id": str(run_id)}),
    ]


def test_refresh_dirty_palace_rooms_excludes_wakeup_briefs_from_membership_repair() -> None:
    statement = select(Item.id).where(~Item.metadata_.has_key("wakeup_brief")).where(~Item.metadata_.has_key("memory_dream"))

    compiled = str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )

    assert "NOT (items.metadata ? " in compiled
    assert "wakeup_brief" in compiled
    assert "memory_dream" in compiled


@pytest.mark.asyncio
async def test_poll_sync_sources_enqueues_recent_source_when_watcher_detects_change(monkeypatch) -> None:
    source_id = uuid.uuid4()
    run_id = uuid.uuid4()
    source = SimpleNamespace(
        id=source_id,
        tenant_id="tenant-a",
        last_synced_at=datetime.now(timezone.utc),
        scan_interval_seconds=900,
    )
    redis = FakeRedis()
    triggers: list[str] = []

    async def fake_has_local_file_changes(_db, candidate) -> bool:
        assert candidate is source
        return True

    async def fake_create_or_get_sync_run(_db, *, tenant_id: str, source, triggered_by: str):
        assert tenant_id == "tenant-a"
        triggers.append(triggered_by)
        return SimpleNamespace(id=run_id), True

    monkeypatch.setattr("app.workers.palace_tasks.async_session", SessionFactory(FakeDb([source])))
    monkeypatch.setattr("app.workers.palace_tasks.sync_source_has_local_file_changes", fake_has_local_file_changes)
    monkeypatch.setattr("app.workers.palace_tasks.create_or_get_sync_run", fake_create_or_get_sync_run)

    await poll_sync_sources({"redis": redis})

    assert triggers == ["watcher"]
    assert redis.enqueued == [
        ("run_sync_source", {"_queue_name": PALACE_WORKER_QUEUE, "sync_run_id": str(run_id)})
    ]


@pytest.mark.asyncio
async def test_watch_local_sync_sources_once_enqueues_changed_sources(monkeypatch) -> None:
    changed_source = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        source_kind="folder",
    )
    unchanged_source = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id="tenant-b",
        source_kind="repo",
    )
    run_id = uuid.uuid4()
    redis = FakeRedis()
    triggers: list[tuple[str, uuid.UUID, str]] = []

    async def fake_has_local_file_changes(_db, source) -> bool:
        return source is changed_source

    async def fake_create_or_get_sync_run(_db, *, tenant_id: str, source, triggered_by: str):
        triggers.append((tenant_id, source.id, triggered_by))
        return SimpleNamespace(id=run_id), True

    monkeypatch.setattr(
        "app.workers.palace_tasks.async_session",
        SessionFactory(FakeDb([changed_source, unchanged_source])),
    )
    monkeypatch.setattr("app.workers.palace_tasks.sync_source_has_local_file_changes", fake_has_local_file_changes)
    monkeypatch.setattr("app.workers.palace_tasks.create_or_get_sync_run", fake_create_or_get_sync_run)

    enqueued = await watch_local_sync_sources_once({"redis": redis})

    assert enqueued == 1
    assert triggers == [("tenant-a", changed_source.id, "watcher")]
    assert redis.enqueued == [
        ("run_sync_source", {"_queue_name": PALACE_WORKER_QUEUE, "sync_run_id": str(run_id)})
    ]


@pytest.mark.asyncio
async def test_watch_local_sync_sources_once_continues_after_source_failure(monkeypatch, caplog) -> None:
    broken_source = SimpleNamespace(id=uuid.uuid4(), tenant_id="tenant-a", source_kind="folder")
    healthy_source = SimpleNamespace(id=uuid.uuid4(), tenant_id="tenant-b", source_kind="folder")
    run_id = uuid.uuid4()
    redis = FakeRedis()

    async def fake_has_local_file_changes(_db, source) -> bool:
        if source is broken_source:
            raise RuntimeError("stat failed")
        return True

    async def fake_create_or_get_sync_run(_db, *, tenant_id: str, source, triggered_by: str):
        assert tenant_id == "tenant-b"
        assert source is healthy_source
        assert triggered_by == "watcher"
        return SimpleNamespace(id=run_id), True

    monkeypatch.setattr(
        "app.workers.palace_tasks.async_session",
        SessionFactory(FakeDb([broken_source, healthy_source])),
    )
    monkeypatch.setattr("app.workers.palace_tasks.sync_source_has_local_file_changes", fake_has_local_file_changes)
    monkeypatch.setattr("app.workers.palace_tasks.create_or_get_sync_run", fake_create_or_get_sync_run)

    enqueued = await watch_local_sync_sources_once({"redis": redis})

    assert enqueued == 1
    assert redis.enqueued == [
        ("run_sync_source", {"_queue_name": PALACE_WORKER_QUEUE, "sync_run_id": str(run_id)})
    ]
    assert "local sync watcher failed" in caplog.text


@pytest.mark.asyncio
async def test_repair_palace_artifacts_repairs_query_results(monkeypatch) -> None:
    tenants = [
        SimpleNamespace(
            tenant_id="tenant-a",
            dirty_generation=4,
            indexed_generation=4,
            active_palace_run_id=None,
        ),
    ]
    repaired: list[tuple[str, int]] = []

    async def fake_repair_stale_room_artifacts(db, *, tenant_id: str, target_generation: int):
        repaired.append((tenant_id, target_generation))
        return PalaceArtifactRepairPlan(snapshot_room_ids=(), tunnel_room_ids=(), blocked_room_ids=(), closet_room_ids=(uuid.uuid4(),))

    monkeypatch.setattr("app.workers.palace_tasks.async_session", SessionFactory(FakeDb(tenants)))
    monkeypatch.setattr("app.workers.palace_tasks.repair_stale_room_artifacts", fake_repair_stale_room_artifacts)

    await repair_palace_artifacts({"redis": FakeRedis()})

    assert repaired == [("tenant-a", 4)]


@pytest.mark.asyncio
async def test_recompute_palace_tunnel_strengths_repairs_idle_tenants(monkeypatch) -> None:
    tenants = [
        SimpleNamespace(
            tenant_id="tenant-a",
            dirty_generation=4,
            indexed_generation=4,
            active_palace_run_id=None,
        ),
    ]
    recomputed: list[tuple[str, int, int]] = []

    async def fake_recompute_stale_room_tunnels(_db, *, tenant_id: str, target_generation: int, limit: int):
        recomputed.append((tenant_id, target_generation, limit))
        return SimpleNamespace(room_ids=(uuid.uuid4(), uuid.uuid4()), target_generation=target_generation)

    monkeypatch.setattr("app.workers.palace_tasks.async_session", SessionFactory(FakeDb(tenants)))
    monkeypatch.setattr("app.workers.palace_tasks.recompute_stale_room_tunnels", fake_recompute_stale_room_tunnels)

    await recompute_palace_tunnel_strengths({"redis": FakeRedis()})

    assert recomputed == [("tenant-a", 4, 50)]


@pytest.mark.asyncio
async def test_run_palace_maintenance_runs_each_phase_in_order(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_dirty_rooms(ctx):
        assert ctx["redis"] is redis
        calls.append("dirty-rooms")

    async def fake_recover(ctx):
        assert ctx["redis"] is redis
        calls.append("backlog")

    async def fake_repair(ctx):
        assert ctx["redis"] is redis
        calls.append("artifacts")

    async def fake_consolidation(ctx):
        assert ctx["redis"] is redis
        calls.append("consolidation")

    async def fake_recompute(ctx):
        assert ctx["redis"] is redis
        calls.append("tunnels")

    async def fake_wakeup_briefs(ctx):
        assert ctx["redis"] is redis
        calls.append("wakeup-briefs")

    redis = FakeRedis()
    monkeypatch.setattr("app.workers.palace_tasks.refresh_dirty_palace_rooms", fake_dirty_rooms)
    monkeypatch.setattr("app.workers.palace_tasks.recover_palace_backlog", fake_recover)
    monkeypatch.setattr("app.workers.palace_tasks.repair_palace_artifacts", fake_repair)
    monkeypatch.setattr("app.workers.palace_tasks.refresh_palace_consolidation_candidates", fake_consolidation)
    monkeypatch.setattr("app.workers.palace_tasks.recompute_palace_tunnel_strengths", fake_recompute)
    monkeypatch.setattr("app.workers.palace_tasks.refresh_caught_up_wakeup_briefs", fake_wakeup_briefs)

    await run_palace_maintenance({"redis": redis})

    assert calls == ["dirty-rooms", "backlog", "artifacts", "consolidation", "tunnels", "wakeup-briefs"]


@pytest.mark.asyncio
async def test_run_palace_maintenance_continues_after_phase_failure(monkeypatch, caplog) -> None:
    calls: list[str] = []

    async def fake_dirty_rooms(_ctx):
        calls.append("dirty-rooms")

    async def broken_recover(_ctx):
        calls.append("backlog")
        raise RuntimeError("temporary outage")

    async def fake_repair(_ctx):
        calls.append("artifacts")

    async def fake_consolidation(_ctx):
        calls.append("consolidation")

    async def fake_recompute(_ctx):
        calls.append("tunnels")

    async def fake_wakeup_briefs(_ctx):
        calls.append("wakeup-briefs")

    monkeypatch.setattr("app.workers.palace_tasks.refresh_dirty_palace_rooms", fake_dirty_rooms)
    monkeypatch.setattr("app.workers.palace_tasks.recover_palace_backlog", broken_recover)
    monkeypatch.setattr("app.workers.palace_tasks.repair_palace_artifacts", fake_repair)
    monkeypatch.setattr("app.workers.palace_tasks.refresh_palace_consolidation_candidates", fake_consolidation)
    monkeypatch.setattr("app.workers.palace_tasks.recompute_palace_tunnel_strengths", fake_recompute)
    monkeypatch.setattr("app.workers.palace_tasks.refresh_caught_up_wakeup_briefs", fake_wakeup_briefs)

    await run_palace_maintenance({"redis": FakeRedis()})

    assert calls == ["dirty-rooms", "backlog", "artifacts", "consolidation", "tunnels", "wakeup-briefs"]
    assert "run_palace_maintenance phase=backlog failed" in caplog.text


@pytest.mark.asyncio
async def test_enqueue_missing_embedding_repairs_sets_processing_and_requeues(monkeypatch) -> None:
    item_id = uuid.uuid4()
    item = SimpleNamespace(id=item_id, tenant_id="tenant-a", status="ready", raw_content="hello")
    db = FakeDb([], items=[item])
    redis = FakeRedis()

    repaired = await _enqueue_missing_embedding_repairs(
        {"redis": redis},
        db,
        tenant_id="tenant-a",
        item_ids=(item_id,),
    )

    assert repaired == (item_id,)
    assert item.status == "processing"
    assert redis.enqueued == [
        ("embed_item", {"item_id": str(item_id), "skip_ai_enrichment": False, "tenant_id": "tenant-a"}),
    ]


@pytest.mark.asyncio
async def test_enqueue_missing_embedding_repairs_restores_ready_state_on_enqueue_failure() -> None:
    item_id = uuid.uuid4()
    item = SimpleNamespace(id=item_id, tenant_id="tenant-a", status="ready", raw_content="hello")
    db = FakeDb([], items=[item])

    class BrokenRedis:
        async def enqueue_job(self, _name: str, **_kwargs) -> None:
            raise RuntimeError("redis down")

    with pytest.raises(RuntimeError, match="redis down"):
        await _enqueue_missing_embedding_repairs(
            {"redis": BrokenRedis()},
            db,
            tenant_id="tenant-a",
            item_ids=(item_id,),
        )

    assert item.status == "ready"
    assert db.commits == 2


@pytest.mark.asyncio
async def test_sweep_palace_index_integrity_dispatches_repairs(monkeypatch) -> None:
    item_id = uuid.uuid4()
    tenant = SimpleNamespace(
        tenant_id="tenant-a",
        dirty_generation=4,
        indexed_generation=4,
        active_palace_run_id=None,
    )
    db = FakeDb([tenant], items=[SimpleNamespace(id=item_id, tenant_id="tenant-a", status="ready", raw_content="hello")])
    redis = FakeRedis()
    marked_dirty: list[tuple[str, uuid.UUID, str]] = []
    repaired_artifacts: list[tuple[str, int]] = []

    monkeypatch.setattr("app.workers.palace_tasks.async_session", SessionFactory(db))

    async def fake_inspect_palace_index_integrity(_db, *, tenant_id: str, target_generation: int):
        assert tenant_id == "tenant-a"
        assert target_generation == 4
        return PalaceIndexIntegrityPlan(
            missing_embedding_item_ids=(item_id,),
            missing_membership_item_ids=(item_id,),
            artifact_repair_plan=PalaceArtifactRepairPlan(
                snapshot_room_ids=(uuid.uuid4(),),
                tunnel_room_ids=(uuid.uuid4(),),
                blocked_room_ids=(),
            ),
        )

    monkeypatch.setattr(
        "app.workers.palace_tasks.inspect_palace_index_integrity",
        fake_inspect_palace_index_integrity,
    )

    async def fake_mark_item_dirty(_db, *, tenant_id: str, item_id: uuid.UUID, reason: str, sync_source_id=None):
        assert sync_source_id is None
        marked_dirty.append((tenant_id, item_id, reason))
        return 5

    async def fake_create_or_get_palace_run(_db, *, tenant_id: str, triggered_by: str, source_sync_run_id=None):
        assert source_sync_run_id is None
        assert tenant_id == "tenant-a"
        assert triggered_by == "maintenance"
        run_id = uuid.uuid4()
        return SimpleNamespace(id=run_id, requested_generation=5), True

    async def fake_repair_stale_room_artifacts(_db, *, tenant_id: str, target_generation: int):
        repaired_artifacts.append((tenant_id, target_generation))
        return PalaceArtifactRepairPlan(snapshot_room_ids=(), tunnel_room_ids=(), blocked_room_ids=())

    monkeypatch.setattr("app.workers.palace_tasks.mark_item_dirty", fake_mark_item_dirty)
    monkeypatch.setattr("app.workers.palace_tasks.create_or_get_palace_run", fake_create_or_get_palace_run)
    monkeypatch.setattr("app.workers.palace_tasks.repair_stale_room_artifacts", fake_repair_stale_room_artifacts)

    await sweep_palace_index_integrity({"redis": redis})

    assert marked_dirty == [("tenant-a", item_id, "integrity-sweep")]
    assert repaired_artifacts == [("tenant-a", 4)]
    assert ("embed_item", {"item_id": str(item_id), "skip_ai_enrichment": False, "tenant_id": "tenant-a"}) in redis.enqueued
    assert any(name == "palace_run_build" for name, _kwargs in redis.enqueued)


@pytest.mark.asyncio
async def test_sweep_palace_index_integrity_repairs_closet_only_artifacts(monkeypatch) -> None:
    tenant = SimpleNamespace(
        tenant_id="tenant-a",
        dirty_generation=4,
        indexed_generation=4,
        active_palace_run_id=None,
    )
    db = FakeDb([tenant])
    redis = FakeRedis()
    repaired_artifacts: list[tuple[str, int]] = []

    monkeypatch.setattr("app.workers.palace_tasks.async_session", SessionFactory(db))

    async def fake_inspect_palace_index_integrity(_db, *, tenant_id: str, target_generation: int):
        assert tenant_id == "tenant-a"
        assert target_generation == 4
        return PalaceIndexIntegrityPlan(
            missing_embedding_item_ids=(),
            missing_membership_item_ids=(),
            artifact_repair_plan=PalaceArtifactRepairPlan(
                snapshot_room_ids=(),
                tunnel_room_ids=(),
                blocked_room_ids=(),
                closet_room_ids=(uuid.uuid4(),),
            ),
        )

    async def fake_repair_stale_room_artifacts(_db, *, tenant_id: str, target_generation: int):
        repaired_artifacts.append((tenant_id, target_generation))
        return PalaceArtifactRepairPlan(snapshot_room_ids=(), tunnel_room_ids=(), blocked_room_ids=())

    monkeypatch.setattr(
        "app.workers.palace_tasks.inspect_palace_index_integrity",
        fake_inspect_palace_index_integrity,
    )
    monkeypatch.setattr("app.workers.palace_tasks.repair_stale_room_artifacts", fake_repair_stale_room_artifacts)

    await sweep_palace_index_integrity({"redis": redis})

    assert repaired_artifacts == [("tenant-a", 4)]
    assert redis.enqueued == []


@pytest.mark.asyncio
async def test_palace_run_build_enqueues_follow_on_run_when_backlog_remains(monkeypatch) -> None:
    run_id = uuid.uuid4()
    next_run_id = uuid.uuid4()
    redis = FakeRedis()
    db = PalaceRunDb(
        run=SimpleNamespace(id=run_id, tenant_id="tenant-a"),
        state=SimpleNamespace(tenant_id="tenant-a", dirty_generation=8, indexed_generation=7),
    )
    created_runs: list[tuple[str, str]] = []

    async def fake_run_palace_run(db_arg, *, run_id: uuid.UUID):
        assert db_arg is db
        assert run_id == db.run.id
        return "completed", None

    async def fake_create_or_get_palace_run(db_arg, *, tenant_id: str, triggered_by: str, source_sync_run_id=None):
        assert db_arg is db
        assert source_sync_run_id is None
        assert tenant_id == "tenant-a"
        assert triggered_by == "auto"
        created_runs.append((tenant_id, str(next_run_id)))
        return SimpleNamespace(id=next_run_id, requested_generation=8), True

    async def fail_generate_wakeup_briefs(*_args, **_kwargs):
        raise AssertionError("wake-up briefs should wait until Palace backlog is clear")

    monkeypatch.setattr("app.workers.palace_tasks.async_session", SessionFactory(db))
    monkeypatch.setattr("app.workers.palace_tasks.run_palace_run", fake_run_palace_run)
    monkeypatch.setattr("app.workers.palace_tasks.create_or_get_palace_run", fake_create_or_get_palace_run)
    monkeypatch.setattr("app.workers.palace_tasks.generate_wakeup_briefs", fail_generate_wakeup_briefs)

    await palace_run_build({"redis": redis}, palace_run_id=str(run_id))

    assert created_runs == [("tenant-a", str(next_run_id))]
    assert redis.enqueued == [
        ("palace_run_build", {"_queue_name": PALACE_WORKER_QUEUE, "palace_run_id": str(next_run_id)}),
    ]


@pytest.mark.asyncio
async def test_palace_run_build_skips_follow_on_when_backlog_is_clear(monkeypatch) -> None:
    run_id = uuid.uuid4()
    redis = FakeRedis()
    embedder_obj = object()
    llm_obj = object()
    db = PalaceRunDb(
        run=SimpleNamespace(id=run_id, tenant_id="tenant-a"),
        state=SimpleNamespace(tenant_id="tenant-a", dirty_generation=4, indexed_generation=4),
    )
    refreshed: list[str] = []

    async def fake_run_palace_run(db_arg, *, run_id: uuid.UUID):
        assert db_arg is db
        assert run_id == db.run.id
        return "completed", None

    async def fake_create_or_get_palace_run(db_arg, *, tenant_id: str, triggered_by: str, source_sync_run_id=None):
        raise AssertionError("follow-on run should not be created when backlog is clear")

    async def fake_generate_wakeup_briefs(db_arg, *, tenant_id: str, embedder, llm):
        assert db_arg is db
        assert tenant_id == "tenant-a"
        assert embedder is embedder_obj
        assert llm is llm_obj
        refreshed.append(tenant_id)
        return SimpleNamespace(created=0, updated=2, unchanged=0, deactivated=0)

    monkeypatch.setattr("app.workers.palace_tasks.async_session", SessionFactory(db))
    monkeypatch.setattr("app.workers.palace_tasks.run_palace_run", fake_run_palace_run)
    monkeypatch.setattr("app.workers.palace_tasks.create_or_get_palace_run", fake_create_or_get_palace_run)
    monkeypatch.setattr("app.workers.palace_tasks.generate_wakeup_briefs", fake_generate_wakeup_briefs)

    await palace_run_build(
        {"redis": redis, "embedder": embedder_obj, "llm": llm_obj},
        palace_run_id=str(run_id),
    )

    assert refreshed == ["tenant-a"]
    assert redis.enqueued == []


@pytest.mark.asyncio
async def test_refresh_caught_up_wakeup_briefs_refreshes_only_clean_tenants(monkeypatch) -> None:
    embedder_obj = object()
    llm_obj = object()
    tenants = [
        SimpleNamespace(
            tenant_id="tenant-a",
            dirty_generation=4,
            indexed_generation=4,
            active_palace_run_id=None,
        ),
        SimpleNamespace(
            tenant_id="tenant-b",
            dirty_generation=5,
            indexed_generation=4,
            active_palace_run_id=None,
        ),
        SimpleNamespace(
            tenant_id="tenant-c",
            dirty_generation=8,
            indexed_generation=8,
            active_palace_run_id=uuid.uuid4(),
        ),
    ]
    db = FakeDb(tenants)
    refreshed: list[str] = []

    async def fake_generate_wakeup_briefs(db_arg, *, tenant_id: str, embedder, llm):
        assert db_arg is db
        assert embedder is embedder_obj
        assert llm is llm_obj
        refreshed.append(tenant_id)
        return SimpleNamespace(created=0, updated=1, unchanged=0, deactivated=0)

    monkeypatch.setattr("app.workers.palace_tasks.async_session", SessionFactory(db))
    monkeypatch.setattr("app.workers.palace_tasks.generate_wakeup_briefs", fake_generate_wakeup_briefs)

    await refresh_caught_up_wakeup_briefs(
        {"redis": FakeRedis(), "embedder": embedder_obj, "llm": llm_obj}
    )

    assert refreshed == ["tenant-a"]


@pytest.mark.asyncio
async def test_run_fact_registry_extraction_scans_each_ready_item_tenant(monkeypatch) -> None:
    extracted: list[str] = []

    monkeypatch.setattr("app.workers.palace_tasks.async_session", SessionFactory(FakeDb([])))

    async def fake_list_fact_registry_tenants(_db):
        return ("tenant-a", "tenant-b")

    async def fake_extract_temporal_facts(_db, *, tenant_id: str):
        extracted.append(tenant_id)
        return SimpleNamespace(items_scanned=2, created=1, updated=0, unchanged=0, superseded=0)

    monkeypatch.setattr("app.workers.palace_tasks.list_fact_registry_tenants", fake_list_fact_registry_tenants)
    monkeypatch.setattr("app.workers.palace_tasks.extract_temporal_facts", fake_extract_temporal_facts)

    await run_fact_registry_extraction({"redis": FakeRedis()})

    assert extracted == ["tenant-a", "tenant-b"]


@pytest.mark.asyncio
async def test_run_wakeup_story_refresh_does_not_rebuild_from_brief_changes(monkeypatch) -> None:
    refreshed: list[str] = []
    redis = FakeRedis()

    monkeypatch.setattr("app.workers.palace_tasks.async_session", SessionFactory(FakeDb([])))

    async def fake_list_fact_registry_tenants(_db):
        return ("tenant-a", "tenant-b")

    async def fake_generate_wakeup_briefs(_db, *, tenant_id: str, embedder, llm):
        assert embedder is embedder_obj
        assert llm is llm_obj
        refreshed.append(tenant_id)
        return SimpleNamespace(created=1, updated=0, unchanged=0, deactivated=0)

    async def fail_create_or_get_palace_run(*_args, **_kwargs):
        raise AssertionError("wake-up briefs should not enqueue Palace rebuilds")

    embedder_obj = object()
    llm_obj = object()
    monkeypatch.setattr("app.workers.palace_tasks.list_fact_registry_tenants", fake_list_fact_registry_tenants)
    monkeypatch.setattr("app.workers.palace_tasks.generate_wakeup_briefs", fake_generate_wakeup_briefs)
    monkeypatch.setattr("app.workers.palace_tasks.create_or_get_palace_run", fail_create_or_get_palace_run)

    await run_wakeup_story_refresh({"redis": redis, "embedder": embedder_obj, "llm": llm_obj})

    assert refreshed == ["tenant-a", "tenant-b"]
    assert redis.enqueued == []


@pytest.mark.asyncio
async def test_run_fact_registry_contradiction_sweep_scans_each_ready_item_tenant(monkeypatch) -> None:
    swept: list[str] = []

    monkeypatch.setattr("app.workers.palace_tasks.async_session", SessionFactory(FakeDb([])))

    async def fake_list_fact_registry_tenants(_db):
        return ("tenant-a", "tenant-b")

    async def fake_sweep_fact_registry_contradictions(_db, *, tenant_id: str):
        swept.append(tenant_id)
        return SimpleNamespace(facts_scanned=3, contradictions=1, facts_flagged=2, facts_cleared=0)

    monkeypatch.setattr("app.workers.palace_tasks.list_fact_registry_tenants", fake_list_fact_registry_tenants)
    monkeypatch.setattr(
        "app.workers.palace_tasks.sweep_fact_registry_contradictions",
        fake_sweep_fact_registry_contradictions,
    )

    await run_fact_registry_contradiction_sweep({"redis": FakeRedis()})

    assert swept == ["tenant-a", "tenant-b"]


def test_worker_settings_split_palace_maintenance_recovery() -> None:
    default_function_names = {func.__name__ for func in WorkerSettings.functions}
    media_function_names = {func.__name__ for func in MediaWorkerSettings.functions}
    palace_function_names = {func.__name__ for func in PalaceWorkerSettings.functions}
    default_cron_names = {job.name for job in WorkerSettings.cron_jobs}
    media_cron_names = {job.name for job in MediaWorkerSettings.cron_jobs}
    palace_cron_names = {job.name for job in PalaceWorkerSettings.cron_jobs}

    assert WorkerSettings.queue_name == "arq:queue"
    assert WorkerSettings.health_check_interval == 15
    assert WorkerSettings.health_check_key.startswith(f"{WorkerSettings.queue_name}:health-check:")
    assert MediaWorkerSettings.health_check_interval == 15
    assert PalaceWorkerSettings.health_check_interval == 15
    assert len({WorkerSettings.health_check_key, MediaWorkerSettings.health_check_key, PalaceWorkerSettings.health_check_key}) == 3
    assert MediaWorkerSettings.queue_name == MEDIA_WORKER_QUEUE
    assert PalaceWorkerSettings.queue_name == PALACE_WORKER_QUEUE
    assert WorkerSettings.on_startup.__name__ == "startup"
    assert MediaWorkerSettings.on_startup.__name__ == "startup"
    assert MediaWorkerSettings.max_jobs == 1
    assert PalaceWorkerSettings.on_startup.__name__ == "palace_startup"
    assert PalaceWorkerSettings.on_shutdown.__name__ == "palace_shutdown"
    assert "process_media" not in default_function_names
    assert "process_youtube" not in default_function_names
    assert "dispatch_tenant_fair_media_jobs" in default_function_names
    assert media_function_names == {"process_media", "process_youtube"}
    assert media_cron_names == set()
    assert "recover_stale_memory_jobs" in default_function_names
    assert "extract_relationships" in default_function_names
    assert "backfill_deferred_relationships" in default_function_names
    assert "recover_palace_backlog" not in default_function_names
    assert "run_palace_maintenance" not in default_function_names
    assert "recover_palace_backlog" in palace_function_names
    assert "refresh_dirty_palace_rooms" in palace_function_names
    assert "run_palace_maintenance" in palace_function_names
    assert "repair_palace_artifacts" in palace_function_names
    assert "recompute_palace_tunnel_strengths" in palace_function_names
    assert "refresh_caught_up_wakeup_briefs" in palace_function_names
    assert "run_fact_registry_extraction" in palace_function_names
    assert "run_fact_registry_contradiction_sweep" in palace_function_names
    assert "sweep_palace_index_integrity" in palace_function_names
    assert "run_wakeup_story_refresh" in palace_function_names
    assert "run_memory_dream_refresh" in palace_function_names
    assert "mark_items_dirty_and_schedule" in palace_function_names
    assert "cron:dispatch_tenant_fair_media_jobs" in default_cron_names
    assert "cron:recover_stale_memory_jobs" in default_cron_names
    assert "cron:run_palace_maintenance" not in default_cron_names
    assert "cron:run_palace_maintenance" in palace_cron_names
    assert "cron:run_fact_registry_extraction" in palace_cron_names
    assert "cron:run_fact_registry_contradiction_sweep" in palace_cron_names
    assert "cron:run_wakeup_story_refresh" in palace_cron_names
    assert "cron:run_memory_dream_refresh" in palace_cron_names
    assert "cron:sweep_palace_index_integrity" in palace_cron_names


def test_media_worker_settings_honor_queue_and_max_jobs_env(monkeypatch) -> None:
    monkeypatch.setenv("ARQ_QUEUE_NAME", "arq:queue:media-custom")
    monkeypatch.setenv("ARQ_MAX_JOBS", "3")

    worker_module = importlib.reload(importlib.import_module("app.workers.worker"))
    try:
        assert worker_module.MediaWorkerSettings.queue_name == "arq:queue:media-custom"
        assert worker_module.MediaWorkerSettings.max_jobs == 3
    finally:
        monkeypatch.delenv("ARQ_QUEUE_NAME", raising=False)
        monkeypatch.delenv("ARQ_MAX_JOBS", raising=False)
        importlib.reload(worker_module)


def test_media_worker_settings_reject_invalid_max_jobs_env(monkeypatch) -> None:
    import app.workers.worker as worker_module

    monkeypatch.setenv("ARQ_MAX_JOBS", "0")
    try:
        with pytest.raises(ValueError, match="ARQ_MAX_JOBS must be a positive integer"):
            importlib.reload(worker_module)
    finally:
        monkeypatch.delenv("ARQ_MAX_JOBS", raising=False)
        importlib.reload(worker_module)
