from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import uuid

import pytest

from app.models.item import Item
from app.models.palace import (
    PalaceDirtyItem,
    PalaceRun,
    PalaceTenantState,
    RetrievalHintArtifact,
    Room,
    RoomClosetArtifact,
    RoomMembership,
    RoomTunnel,
    SyncSource,
    Wing,
)
from app.schemas.palace import SyncSourceCreate, SyncSourceUpdate
from app.schemas.search import SearchResult
from app.services import palace as palace_service
from app.services.embedder import EmbeddingRequestError
from app.services.palace import (
    PalaceArtifactRepairPlan,
    PalaceTunnelRecomputeResult,
    _RoomConsolidationProfile,
    _build_webhook_health,
    _consolidation_candidate_signature,
    _repairable_room_artifacts,
    _encrypt_repo_credential,
    _github_blob_url,
    _github_https_repo_url,
    _github_ssh_repo_url,
    _infer_room_name,
    _infer_wing_name,
    _is_remote_github_repo,
    _apply_room_routing,
    _is_blank_sync_text,
    _iter_repo_sync_files,
    _iter_s3_sync_files,
    _load_sync_item,
    _normalize_sync_prefix,
    _normalized_allowed_extensions,
    _route_room_score,
    _retrieval_quality_decision,
    _score_consolidation_pair,
    _rebuild_room_closet_artifact,
    _rebuild_tunnels,
    _updated_tunnel_stability,
    _sync_source_locator,
    build_overview,
    create_sync_source,
    create_or_get_palace_run,
    build_room_artifact_health,
    delete_sync_source,
    find_consolidation_candidates,
    get_room_detail,
    mark_items_dirty,
    record_consolidation_candidate_events,
    repair_stale_room_artifacts,
    recompute_stale_room_tunnels,
    retrieve_palace,
    run_palace_run,
    sync_source_has_local_file_changes,
    update_sync_source,
    validate_sync_root,
)
from app.services.retrieval_hints import (
    rebuild_room_retrieval_hints,
    report_retrieval_hint_candidates,
    retrieve_retrieval_hint_rescue_results,
)
from app.services.search import RetrievalDependencyUnavailableError


def _quality_result(*, title: str, score: float, currentness: str = "current") -> SearchResult:
    return SearchResult(
        item_id=uuid.uuid4(),
        title=title,
        summary=None,
        source_type="note",
        source_url=None,
        tags=[],
        created_at=datetime.now(timezone.utc),
        chunk_text=title,
        chunk_index=0,
        score=score,
        currentness=currentness,
    )


def test_retrieval_quality_gate_rescues_full_irrelevant_results_but_accepts_good_results() -> None:
    irrelevant = [
        _quality_result(title="unrelated gardening note", score=0.81 - index * 0.01)
        for index in range(5)
    ]
    weak = _retrieval_quality_decision("current deployment owner", irrelevant)
    assert weak["decision"] == "rescue"
    assert "weak_relevance" in weak["reasons"]

    good = [
        _quality_result(title="current deployment owner", score=0.91),
        _quality_result(title="deployment history", score=0.72),
    ]
    assert _retrieval_quality_decision("current deployment owner", good)["decision"] == "accept"


TEST_SYNC_SOURCE_CREDENTIAL_KEY = base64.urlsafe_b64encode(
    b"palace-test-sync-credential-key!"
).decode()


def _fake_item(
    *,
    path: str,
    title: str,
    tags: list[str] | None = None,
    categories: list[str] | None = None,
    metadata: dict | None = None,
):
    metadata_ = {"sync_relative_path": path} if metadata is None else dict(metadata)
    metadata_.setdefault("sync_relative_path", path)
    return SimpleNamespace(
        metadata_=metadata_,
        title=title,
        tags=tags or [],
        categories=categories or [],
    )


def test_updated_tunnel_stability_preserves_stable_edges_and_penalizes_drift() -> None:
    assert _updated_tunnel_stability(
        previous_strength=0.7,
        new_strength=0.72,
        previous_stability=0.82,
    ) == 0.85
    assert _updated_tunnel_stability(
        previous_strength=0.9,
        new_strength=0.2,
        previous_stability=0.8,
    ) == 0.45
    assert _updated_tunnel_stability(
        previous_strength=None,
        new_strength=0.4,
        previous_stability=None,
    ) == 1.0


@pytest.mark.asyncio
async def test_rebuild_tunnels_preserves_activation_fields_across_recompute() -> None:
    source_room = SimpleNamespace(id=uuid.uuid4(), tunnel_generation=1)
    target_room = SimpleNamespace(id=uuid.uuid4(), tunnel_generation=1)
    last_activated_at = datetime(2026, 5, 27, 1, 0, tzinfo=timezone.utc)
    previous = RoomTunnel(
        tenant_id="default",
        source_room_id=source_room.id,
        target_room_id=target_room.id,
        tunnel_type="shared-tag",
        strength=0.6,
        activation_count=5,
        stability=0.8,
        last_activated_at=last_activated_at,
    )

    class FakeDb:
        def __init__(self) -> None:
            self.execute_calls = 0
            self.added: list[RoomTunnel] = []

        async def execute(self, _statement):
            self.execute_calls += 1
            if self.execute_calls == 1:
                return _RowsResult([previous])
            if self.execute_calls == 2:
                return _RowsResult([])
            if self.execute_calls == 3:
                return _RowsResult([source_room, target_room])
            if self.execute_calls == 4:
                return _RowsResult([(["shared", "source"],)])
            if self.execute_calls == 5:
                return _RowsResult([(["shared"],)])
            raise AssertionError(f"unexpected execute call {self.execute_calls}")

        def add(self, value) -> None:
            self.added.append(value)

    db = FakeDb()

    await _rebuild_tunnels(
        db,
        tenant_id="default",
        room_ids={source_room.id},
        generation=9,
    )

    assert len(db.added) == 1
    rebuilt = db.added[0]
    assert rebuilt.activation_count == 5
    assert rebuilt.last_activated_at == last_activated_at
    assert rebuilt.stability == 0.75
    assert source_room.tunnel_generation == 9


class _RowsResult:
    def __init__(self, rows) -> None:
        self._rows = rows

    def all(self):
        return self._rows

    def scalars(self):
        return self

    def scalar_one(self):
        if len(self._rows) != 1:
            raise AssertionError(f"Expected exactly one row, got {len(self._rows)}")
        return self._rows[0]


class _ScalarRowsResult:
    def __init__(self, rows) -> None:
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _ScalarsResult:
    def __init__(self, rows) -> None:
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _PalaceRunLeaseDb:
    def __init__(self, *, active_run=None, state=None, stale_run=None) -> None:
        self.active_run = active_run
        self.state = state
        self.stale_run = stale_run
        self.added: list[object] = []
        self.commits = 0
        self.flushes = 0
        self.refreshed: list[object] = []

    async def scalar(self, _statement):
        return self.active_run

    async def get(self, model, key):
        if model is PalaceTenantState and self.state is not None and key == self.state.tenant_id:
            return self.state
        if model is PalaceRun and self.stale_run is not None and key == self.stale_run.id:
            return self.stale_run
        return None

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushes += 1
        for obj in self.added:
            if isinstance(obj, PalaceRun) and obj.id is None:
                obj.id = uuid.uuid4()

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, obj) -> None:
        self.refreshed.append(obj)


class _RoomRoutingDb:
    def __init__(
        self,
        *,
        existing_auto: list[RoomMembership],
        pinned_primary: RoomMembership | None = None,
    ) -> None:
        self.existing_auto = existing_auto
        self.pinned_primary = pinned_primary
        self.ops: list[str] = []
        self.added: list[RoomMembership] = []

    async def execute(self, _statement):
        self.ops.append("execute:auto")
        return _ScalarsResult(self.existing_auto)

    async def scalar(self, _statement):
        self.ops.append("scalar:pinned")
        return self.pinned_primary

    async def delete(self, membership: RoomMembership) -> None:
        self.ops.append(f"delete:{membership.room_id}")

    async def flush(self) -> None:
        self.ops.append("flush")

    def add(self, row) -> None:
        self.ops.append(f"add:{type(row).__name__}")
        self.added.append(row)


@pytest.mark.asyncio
async def test_create_or_get_palace_run_reports_duplicate_active_submission(caplog) -> None:
    active_run = PalaceRun(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        status="queued",
        triggered_by="manual",
        requested_generation=4,
        applied_generation=0,
        attempt=1,
        started_at=datetime.now(timezone.utc),
    )
    db = _PalaceRunLeaseDb(active_run=active_run)

    caplog.set_level("INFO", logger="app.services.palace")
    run, created = await create_or_get_palace_run(db, tenant_id="tenant-a", triggered_by="curation")

    assert run is active_run
    assert created is False
    assert db.added == []
    assert "palace run submission coalesced" in caplog.text
    assert "trigger=curation" in caplog.text
    assert f"active_run_id={active_run.id}" in caplog.text


@pytest.mark.asyncio
async def test_create_or_get_palace_run_recovers_stale_active_pointer(caplog) -> None:
    stale_run_id = uuid.uuid4()
    state = SimpleNamespace(
        tenant_id="tenant-a",
        dirty_generation=9,
        indexed_generation=7,
        active_palace_run_id=stale_run_id,
        active_generation=8,
    )
    stale_run = PalaceRun(
        id=stale_run_id,
        tenant_id="tenant-a",
        status="completed",
        triggered_by="manual",
        requested_generation=8,
        applied_generation=8,
        attempt=1,
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
    )
    db = _PalaceRunLeaseDb(state=state, stale_run=stale_run)

    caplog.set_level("INFO", logger="app.services.palace")
    run, created = await create_or_get_palace_run(db, tenant_id="tenant-a", triggered_by="maintenance")

    assert created is True
    assert run.tenant_id == "tenant-a"
    assert run.triggered_by == "maintenance"
    assert run.requested_generation == 9
    assert state.active_palace_run_id == run.id
    assert state.active_generation == 9
    assert db.commits == 1
    assert "palace run lease recovered" in caplog.text
    assert f"stale_run_id={stale_run_id}" in caplog.text
    assert "stale_status=completed" in caplog.text
    assert "palace run lease created" in caplog.text


@pytest.mark.asyncio
async def test_mark_items_dirty_coalesces_generation_for_batch() -> None:
    item_ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
    state = PalaceTenantState(tenant_id="tenant-a", dirty_generation=7)
    existing_dirty = PalaceDirtyItem(
        tenant_id="tenant-a",
        item_id=item_ids[0],
        generation=3,
        reason="old",
    )

    class DirtyMarkerDb:
        def __init__(self) -> None:
            self.added: list[PalaceDirtyItem | PalaceTenantState] = []
            self.flushes = 0

        async def get(self, model, key):
            if model is PalaceTenantState and key == "tenant-a":
                return state
            return None

        async def execute(self, _statement):
            return _ScalarsResult([existing_dirty])

        def add(self, row) -> None:
            self.added.append(row)

        async def flush(self) -> None:
            self.flushes += 1

    db = DirtyMarkerDb()
    generation = await mark_items_dirty(
        db,
        tenant_id="tenant-a",
        item_ids=[item_ids[0], item_ids[1], item_ids[1]],
        reason="sync",
        sync_source_id=item_ids[2],
    )

    assert generation == 8
    assert state.dirty_generation == 8
    assert existing_dirty.generation == 8
    assert existing_dirty.reason == "sync"
    assert existing_dirty.sync_source_id == item_ids[2]
    assert len(db.added) == 1
    new_dirty = db.added[0]
    assert isinstance(new_dirty, PalaceDirtyItem)
    assert new_dirty.item_id == item_ids[1]
    assert new_dirty.generation == 8
    assert new_dirty.reason == "sync"
    assert db.flushes == 1


class _RepairableArtifactsDb:
    def __init__(self, rows) -> None:
        self.rows = rows

    async def execute(self, _statement):
        return _RowsResult(self.rows)


class _LocalWatcherDb:
    def __init__(self, rows) -> None:
        self.rows = rows

    async def execute(self, _statement):
        return _ScalarRowsResult(self.rows)


class _ClosetArtifactDb:
    def __init__(self, *, room, rows) -> None:
        self.room = room
        self.rows = rows
        self.added: list[object] = []
        self.execute_count = 0

    async def get(self, model, key):
        if model is Room and key == self.room.id:
            return self.room
        return None

    async def execute(self, _statement):
        self.execute_count += 1
        if self.execute_count % 2 == 1:
            return _RowsResult(self.rows)
        return _RowsResult([])

    def add(self, row) -> None:
        self.added.append(row)


class _ConsolidationCandidateDb:
    def __init__(self, *, rooms, closets, memberships=None, events=None) -> None:
        self.rooms = rooms
        self.closets = closets
        self.memberships = memberships or []
        self.events = events or []
        self.added: list[object] = []
        self.commits = 0
        self.calls = 0

    async def execute(self, _statement):
        self.calls += 1
        if self.calls == 1:
            return _RowsResult(self.rooms)
        if self.calls == 2:
            return _ScalarsResult(self.closets)
        if self.calls == 3:
            return _RowsResult(self.memberships)
        return _ScalarsResult(self.events)

    def add(self, row) -> None:
        self.added.append(row)

    async def commit(self) -> None:
        self.commits += 1

    async def scalar(self, _statement):
        return len(self.rooms)


@pytest.mark.asyncio
async def test_apply_room_routing_flushes_deleted_auto_membership_before_reinsert(monkeypatch) -> None:
    tenant_id = "tenant-a"
    item_id = uuid.uuid4()
    room = Room(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        wing_id=uuid.uuid4(),
        stable_key="general:notes",
        slug="notes",
        name="Notes",
        membership_generation=0,
    )
    existing = RoomMembership(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        room_id=room.id,
        item_id=item_id,
        source="auto",
        membership_kind="primary",
    )
    item = SimpleNamespace(
        id=item_id,
        status="ready",
        metadata_={"sync_relative_path": "general/notes.md"},
        title="Notes",
        tags=[],
        categories=[],
    )
    db = _RoomRoutingDb(existing_auto=[existing])

    async def fake_ensure_room(db_arg, *, tenant_id: str, wing_name: str, room_name: str):
        return room

    monkeypatch.setattr(palace_service, "_ensure_room", fake_ensure_room)

    affected_rooms = await _apply_room_routing(db, tenant_id=tenant_id, item=item, generation=5)

    assert affected_rooms == {room.id}
    assert db.ops.index("flush") < db.ops.index("add:RoomMembership")
    assert db.added[0].room_id == room.id
    assert db.added[0].item_id == item_id
    assert room.membership_generation == 5


@pytest.mark.asyncio
async def test_run_palace_run_routes_duplicate_dirty_rows_once(monkeypatch) -> None:
    tenant_id = "tenant-a"
    item_id = uuid.uuid4()
    run_id = uuid.uuid4()
    room_id = uuid.uuid4()
    run = PalaceRun(id=run_id, tenant_id=tenant_id, requested_generation=9, status="queued")
    state = PalaceTenantState(tenant_id=tenant_id, indexed_generation=8)
    item = SimpleNamespace(id=item_id)
    dirty_rows = [
        (SimpleNamespace(id=uuid.uuid4()), item),
        (SimpleNamespace(id=uuid.uuid4()), item),
    ]

    class DuplicateDirtyDb:
        def __init__(self) -> None:
            self.commits = 0
            self.deleted_dirty_statement = None

        async def get(self, model, key):
            if model is PalaceRun and key == run_id:
                return run
            if model is PalaceTenantState and key == tenant_id:
                return state
            return None

        async def execute(self, statement):
            if getattr(statement, "__visit_name__", "") == "delete":
                self.deleted_dirty_statement = statement
                return _RowsResult([])
            return _RowsResult(dirty_rows)

        async def commit(self) -> None:
            self.commits += 1

        async def rollback(self) -> None:
            raise AssertionError("run should not roll back")

    routed_items: list[uuid.UUID] = []

    async def fake_apply_room_routing(db_arg, *, tenant_id: str, item, generation: int):
        routed_items.append(item.id)
        return {room_id}

    async def fake_rebuild_room_snapshot(
        db_arg,
        *,
        tenant_id: str,
        room_id: uuid.UUID,
        generation: int,
    ) -> None:
        return None

    async def fake_rebuild_room_retrieval_hints(
        db_arg,
        *,
        tenant_id: str,
        room_id: uuid.UUID,
        generation: int,
    ) -> list:
        return []

    async def fake_rebuild_tunnels(
        db_arg,
        *,
        tenant_id: str,
        room_ids: set[uuid.UUID],
        generation: int,
    ) -> None:
        return None

    monkeypatch.setattr(palace_service, "_apply_room_routing", fake_apply_room_routing)
    monkeypatch.setattr(palace_service, "_rebuild_room_snapshot", fake_rebuild_room_snapshot)
    monkeypatch.setattr(palace_service, "rebuild_room_retrieval_hints", fake_rebuild_room_retrieval_hints)
    monkeypatch.setattr(palace_service, "_rebuild_tunnels", fake_rebuild_tunnels)

    db = DuplicateDirtyDb()
    status, error = await run_palace_run(db, run_id=run_id)

    assert (status, error) == ("completed", None)
    assert routed_items == [item_id]
    assert db.deleted_dirty_statement is not None
    assert run.status == "completed"
    assert run.applied_generation == 9
    assert state.indexed_generation == 9


def test_validate_sync_root_accepts_allowed_directory(monkeypatch) -> None:
    with TemporaryDirectory() as tmpdir:
        monkeypatch.setattr("app.services.palace.settings.palace_sync_allowed_roots", tmpdir)
        resolved = validate_sync_root(tmpdir)
        assert resolved == Path(tmpdir).resolve()


def test_validate_sync_root_rejects_outside_allowed_roots(monkeypatch) -> None:
    with TemporaryDirectory() as allowed, TemporaryDirectory() as outside:
        monkeypatch.setattr("app.services.palace.settings.palace_sync_allowed_roots", allowed)
        with pytest.raises(Exception):
            validate_sync_root(outside)


@pytest.mark.asyncio
async def test_sync_source_has_local_file_changes_detects_modified_file(monkeypatch, tmp_path: Path) -> None:
    note_path = tmp_path / "note.md"
    note_path.write_text("Original", encoding="utf-8")
    stat = note_path.stat()
    source = SyncSource(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        name="Vault",
        root_path=str(tmp_path),
        source_kind="folder",
        status="active",
        scan_interval_seconds=900,
        allowed_extensions=[".md"],
    )
    db = _LocalWatcherDb(
        [
            SimpleNamespace(
                relative_path="note.md",
                source_fingerprint=f"{stat.st_mtime_ns}:1",
                file_size=1,
                modified_ns=stat.st_mtime_ns,
                status="active",
            )
        ]
    )

    monkeypatch.setattr("app.services.palace.settings.palace_sync_allowed_roots", str(tmp_path))

    assert await sync_source_has_local_file_changes(db, source) is True


@pytest.mark.asyncio
async def test_sync_source_has_local_file_changes_ignores_unchanged_skipped_file(monkeypatch, tmp_path: Path) -> None:
    blank_path = tmp_path / "blank.md"
    blank_path.write_text("   \n", encoding="utf-8")
    stat = blank_path.stat()
    source = SyncSource(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        name="Vault",
        root_path=str(tmp_path),
        source_kind="folder",
        status="active",
        scan_interval_seconds=900,
        allowed_extensions=[".md"],
    )
    db = _LocalWatcherDb(
        [
            SimpleNamespace(
                relative_path="blank.md",
                source_fingerprint=f"{stat.st_mtime_ns}:{stat.st_size}",
                file_size=stat.st_size,
                modified_ns=stat.st_mtime_ns,
                status="skipped",
            )
        ]
    )

    monkeypatch.setattr("app.services.palace.settings.palace_sync_allowed_roots", str(tmp_path))

    assert await sync_source_has_local_file_changes(db, source) is False


def test_infer_wing_and_room_prefers_sync_path() -> None:
    item = _fake_item(
        path="product/pricing/launch-note.md",
        title="Pricing launch note",
        tags=["positioning"],
    )
    assert _infer_wing_name(item) == "Product / Growth"
    assert _infer_room_name(item) == "Pricing"


def test_infer_room_uses_nist_metadata_and_ignores_operational_tags() -> None:
    item = _fake_item(
        path="",
        title="NIST SP 800-160v1r1 - Engineering trustworthy secure systems",
        tags=[
            "benchmark",
            "benchmark-cleanup-ok",
            "benchmark-run-20260428",
            "nist",
            "nist-sp800",
            "nist-800-160v1r1",
            "systems-security",
        ],
        metadata={
            "memory_entry": {
                "scope": {"type": "tenant_shared", "key": None},
                "metadata": {
                    "nist": {
                        "publication_id": "800-160v1r1",
                        "title": "Engineering trustworthy secure systems",
                    }
                },
            }
        },
    )

    assert _infer_wing_name(item) == "Security / Compliance"
    assert _infer_room_name(item) == "Engineering Trustworthy Secure Systems"


def test_route_room_score_trusts_specific_room_name_match_with_long_query() -> None:
    score = _route_room_score(
        "zero trust architecture policy engine policy administrator trust algorithm",
        "Zero Trust Architecture",
        "Security / Compliance",
        "NIST SP 800-207 guidance.",
    )

    assert score >= 0.5


def test_infer_agent_scope_uses_scope_key_when_only_operational_tags() -> None:
    item = _fake_item(
        path="",
        title="Recall preference",
        tags=["scope-agent", "agent-orchestrator"],
        metadata={
            "memory_entry": {
                "scope": {"type": "agent", "key": "orchestrator"},
                "metadata": {"memory_tool": {"target": "memory"}},
            }
        },
    )

    assert _infer_wing_name(item) == "Infra / Code / Agents"
    assert _infer_room_name(item) == "Orchestrator"


def test_route_room_score_rewards_overlap() -> None:
    strong = _route_room_score(
        "pricing narrative founder cta",
        "Pricing Narrative",
        "Product / Growth",
        "Founder-facing CTA work lives here.",
    )
    weak = _route_room_score(
        "pricing narrative founder cta",
        "Infrastructure",
        "Infra / Code / Agents",
        "Kubernetes and worker operations.",
    )
    assert strong > weak


def _degraded_retrieval_body() -> SimpleNamespace:
    return SimpleNamespace(
        query="temporary embedding outage",
        room_id=None,
        limit=5,
        candidate_limit=20,
        include_neighbor_chunks=False,
        neighbor_chunk_window=1,
        context_budget_chars=None,
        include_derived_artifacts=False,
        retrieval_lens=None,
        scope_type="workspace",
        scope_key="palaceoftruth",
        tags=None,
        tags_mode="any",
        min_score=None,
        date_from=None,
        date_to=None,
    )


@pytest.mark.asyncio
async def test_retrieve_palace_reuses_retryable_embedding_degradation_across_searches(monkeypatch) -> None:
    item_id = uuid.uuid4()
    room = SimpleNamespace(
        id=uuid.uuid4(),
        name="Temporary Embedding Outage",
        stable_key="software:temporary-embedding-outage",
        snapshot_generation=0,
        membership_generation=0,
    )

    class FakeDb:
        async def execute(self, _statement):
            return _RowsResult([(room, "Software", "Temporary embedding outage operations.")])

        async def get(self, model, key):
            return room if model is Room and key == room.id else None

    class FailingEmbedder:
        calls = 0

        async def embed_single(self, _query: str) -> list[float]:
            self.calls += 1
            raise EmbeddingRequestError("private provider detail", retryable=True, failure_kind="timeout")

    class FakeSearchService:
        calls = []

        def __init__(self, db, embedder, tenant_id: str = "default"):
            self.last_ranking_trace = None

        async def vector_search(self, **kwargs):
            self.calls.append(kwargs)
            self.last_ranking_trace = {
                "retrieval_mode": "lexical_degraded",
                "dependency_degradation": {
                    "dependency": "embedding_provider",
                    "failure_kind": "timeout",
                    "retryable": True,
                },
            }
            return [
                SearchResult(
                    item_id=item_id,
                    title="Lexical fallback",
                    summary=None,
                    source_type="note",
                    source_url=None,
                    tags=[],
                    created_at=datetime.now(timezone.utc),
                    chunk_text="safe lexical result",
                    chunk_index=0,
                    score=0.8,
                )
            ]

    async def fake_ensure_tenant_state(db, tenant_id):
        return SimpleNamespace(indexed_generation=0, active_generation=None)

    monkeypatch.setattr("app.services.palace.ensure_tenant_state", fake_ensure_tenant_state)
    monkeypatch.setattr("app.services.palace.SearchService", FakeSearchService)
    embedder = FailingEmbedder()

    response = await retrieve_palace(
        FakeDb(), tenant_id="default", embedder=embedder, body=_degraded_retrieval_body()
    )

    assert embedder.calls == 1
    assert len(FakeSearchService.calls) == 2
    assert all(call["query_vector"] is None for call in FakeSearchService.calls)
    error = FakeSearchService.calls[0]["query_embedding_error"]
    assert isinstance(error, EmbeddingRequestError)
    assert error.failure_kind == "timeout"
    assert FakeSearchService.calls[1]["query_embedding_error"] is error
    assert response.trace.embedding_unavailable is True
    assert response.trace.retrieval_mode == "lexical_degraded"
    assert response.trace.embedding_failure_kind == "timeout"
    assert response.trace.embedding_failure_retryable is True
    assert "private provider detail" not in response.model_dump_json()

    second_response = await retrieve_palace(
        FakeDb(),
        tenant_id="default",
        embedder=embedder,
        body=_degraded_retrieval_body(),
        query_embedding_error=error,
    )
    assert embedder.calls == 1
    assert second_response.trace.retrieval_mode == "lexical_degraded"


@pytest.mark.asyncio
async def test_retrieve_palace_raises_dependency_unavailable_when_degraded_search_is_empty(monkeypatch) -> None:
    class FakeDb:
        async def execute(self, _statement):
            return _RowsResult([])

    class FailingEmbedder:
        async def embed_single(self, _query: str) -> list[float]:
            raise EmbeddingRequestError("timeout", retryable=True, failure_kind="timeout")

    class EmptySearchService:
        def __init__(self, db, embedder, tenant_id: str = "default"):
            self.last_ranking_trace = {"retrieval_mode": "lexical_degraded"}

        async def vector_search(self, **kwargs):
            return []

    async def fake_ensure_tenant_state(db, tenant_id):
        return SimpleNamespace(indexed_generation=0, active_generation=None)

    monkeypatch.setattr("app.services.palace.ensure_tenant_state", fake_ensure_tenant_state)
    monkeypatch.setattr("app.services.palace.SearchService", EmptySearchService)

    with pytest.raises(RetrievalDependencyUnavailableError) as raised:
        await retrieve_palace(
            FakeDb(), tenant_id="default", embedder=FailingEmbedder(), body=_degraded_retrieval_body()
        )

    assert raised.value.dependency == "embedding_provider"
    assert raised.value.failure_kind == "timeout"
    assert raised.value.retryable is True


@pytest.mark.asyncio
async def test_retrieve_palace_propagates_non_retryable_embedding_failure(monkeypatch) -> None:
    class FakeDb:
        async def execute(self, _statement):
            return _RowsResult([])

    class FailingEmbedder:
        async def embed_single(self, _query: str) -> list[float]:
            raise EmbeddingRequestError("bad request", retryable=False, failure_kind="validation")

    async def fake_ensure_tenant_state(db, tenant_id):
        return SimpleNamespace(indexed_generation=0, active_generation=None)

    monkeypatch.setattr("app.services.palace.ensure_tenant_state", fake_ensure_tenant_state)

    with pytest.raises(EmbeddingRequestError) as raised:
        await retrieve_palace(
            FakeDb(), tenant_id="default", embedder=FailingEmbedder(), body=_degraded_retrieval_body()
        )

    assert raised.value.retryable is False
    assert raised.value.failure_kind == "validation"


@pytest.mark.asyncio
async def test_retrieve_palace_prefers_exact_scope_room_over_summary_only_tie(monkeypatch) -> None:
    exact_room = SimpleNamespace(
        id=uuid.uuid4(),
        name="Receipt Shelf",
        stable_key="software-development:receipt-shelf",
        slug="receipt-shelf",
        snapshot_generation=0,
    )
    diary_room = SimpleNamespace(
        id=uuid.uuid4(),
        name="2026 05 07",
        stable_key="diary:2026-05-07",
        slug="2026-05-07",
        snapshot_generation=0,
    )
    exact_item_id = uuid.UUID("3eaf5fe0-1855-4e19-8898-d110ebecf3ae")

    class FakeDb:
        async def execute(self, _statement):
            return _RowsResult(
                [
                    (diary_room, "Diary", "Receipt Shelf status summary."),
                    (exact_room, "Software Development", "Canonical Receipt Shelf project memory."),
                ]
            )

        async def get(self, model, key):
            if model is Room and key == exact_room.id:
                return exact_room
            return None

    class FakeEmbedder:
        async def embed_single(self, _query: str) -> list[float]:
            return [0.1, 0.2, 0.3]

    class FakeSearchService:
        calls: list[tuple[list[uuid.UUID] | None, str | None]] = []

        def __init__(self, db, embedder, tenant_id: str = "default"):
            self.last_ranking_trace = None

        async def vector_search(self, *, room_ids=None, scope_type=None, **kwargs):
            self.calls.append((room_ids, scope_type))
            self.last_ranking_trace = {
                "ranking_features_version": 1,
                "source_ranking_enabled": False,
                "candidate_limit": 50,
                "candidate_count": 1,
                "results": [],
            }
            if room_ids:
                return [
                    SearchResult(
                        item_id=uuid.uuid4(),
                        title="Diary Rollup 2026-05-07",
                        summary="Summary-only Receipt Shelf diary rollup.",
                        source_type="note",
                        source_url=None,
                        tags=["scope-workspace", "workspace-receipt-shelf", "diary-rollup"],
                        created_at=datetime.now(timezone.utc),
                        chunk_text="Receipt Shelf was mentioned in the daily diary.",
                        chunk_index=0,
                        score=0.95,
                    )
                ]
            return [
                SearchResult(
                    item_id=exact_item_id,
                    title="Receipt Shelf full project brief",
                    summary="Canonical project brief.",
                    source_type="note",
                    source_url=None,
                    tags=["scope-workspace", "workspace-receipt-shelf", "codex-project-brief"],
                    created_at=datetime.now(timezone.utc),
                    chunk_text="Receipt Shelf current implementation context.",
                    chunk_index=0,
                    score=0.74,
                )
            ]

    async def fake_ensure_tenant_state(db, tenant_id):
        return SimpleNamespace(indexed_generation=0, active_generation=None)

    monkeypatch.setattr("app.services.palace.ensure_tenant_state", fake_ensure_tenant_state)
    monkeypatch.setattr("app.services.palace.SearchService", FakeSearchService)

    response = await retrieve_palace(
        FakeDb(),
        tenant_id="default",
        embedder=FakeEmbedder(),
        body=SimpleNamespace(
            query="receipt-shelf",
            room_id=None,
            limit=5,
            scope_type="workspace",
            scope_key="receipt-shelf",
            tags=None,
            tags_mode="any",
            min_score=0.3,
            date_from=None,
            date_to=None,
        ),
    )

    assert response.trace.candidate_rooms[0] == "Receipt Shelf"
    assert response.results[0].item_id == exact_item_id
    assert FakeSearchService.calls == [
        ([exact_room.id, diary_room.id], "workspace"),
        (None, "workspace"),
    ]
    assert [trace.route for trace in response.trace.ranking_traces] == ["room_scoped", "scoped_rescue"]


def test_score_consolidation_pair_detects_near_duplicate_rooms() -> None:
    item_id = uuid.uuid4()
    wing_id = uuid.uuid4()
    left = _RoomConsolidationProfile(
        room_id=uuid.uuid4(),
        room_name="Pricing Narrative",
        room_stable_key="product-growth:pricing-narrative",
        room_slug="pricing-narrative",
        wing_id=wing_id,
        wing_name="Product / Growth",
        item_ids=frozenset({item_id}),
        tag_counts={"pricing": 3, "launch": 2},
    )
    right = _RoomConsolidationProfile(
        room_id=uuid.uuid4(),
        room_name="Pricing Narratives",
        room_stable_key="product-growth:pricing-narratives",
        room_slug="pricing-narratives",
        wing_id=wing_id,
        wing_name="Product / Growth",
        item_ids=frozenset({item_id}),
        tag_counts={"pricing": 2, "launch": 1},
    )

    candidate = _score_consolidation_pair(left, right)

    assert candidate is not None
    assert candidate.score >= 0.9
    assert "very similar room names" in candidate.reasons
    assert "shared drawer references" in candidate.reasons
    assert candidate.shared_drawer_item_ids == [item_id]


@pytest.mark.asyncio
async def test_find_consolidation_candidates_ignores_cross_wing_matches() -> None:
    room_a = Room(
        id=uuid.uuid4(),
        tenant_id="default",
        wing_id=uuid.uuid4(),
        slug="pricing-narrative",
        stable_key="product-growth:pricing-narrative",
        name="Pricing Narrative",
        state="active",
    )
    room_b = Room(
        id=uuid.uuid4(),
        tenant_id="default",
        wing_id=uuid.uuid4(),
        slug="pricing-narrative",
        stable_key="research-market:pricing-narrative",
        name="Pricing Narrative",
        state="active",
    )
    db = _ConsolidationCandidateDb(
        rooms=[(room_a, "Product / Growth"), (room_b, "Research / Market")],
        closets=[],
    )

    summary = await find_consolidation_candidates(db, tenant_id="default")

    assert summary.candidate_count == 0
    assert summary.candidates == []


@pytest.mark.asyncio
async def test_find_consolidation_candidates_reports_bounded_control_tower_scan() -> None:
    wing_id = uuid.uuid4()
    room_a = Room(
        id=uuid.uuid4(),
        tenant_id="default",
        wing_id=wing_id,
        slug="pricing-narrative",
        stable_key="product-growth:pricing-narrative",
        name="Pricing Narrative",
        state="active",
    )
    room_b = Room(
        id=uuid.uuid4(),
        tenant_id="default",
        wing_id=wing_id,
        slug="pricing-narratives",
        stable_key="product-growth:pricing-narratives",
        name="Pricing Narratives",
        state="active",
    )
    room_c = Room(
        id=uuid.uuid4(),
        tenant_id="default",
        wing_id=wing_id,
        slug="unrelated",
        stable_key="product-growth:unrelated",
        name="Unrelated",
        state="active",
    )

    class BoundedDb(_ConsolidationCandidateDb):
        async def execute(self, _statement):
            self.calls += 1
            if self.calls == 1:
                return _RowsResult([(room_a, "Product / Growth"), (room_b, "Product / Growth")])
            if self.calls == 2:
                return _ScalarsResult([])
            if self.calls == 3:
                return _RowsResult([])
            return _ScalarsResult([])

    db = BoundedDb(
        rooms=[
            (room_a, "Product / Growth"),
            (room_b, "Product / Growth"),
            (room_c, "Product / Growth"),
        ],
        closets=[],
    )

    summary = await find_consolidation_candidates(db, tenant_id="default", max_profile_rooms=2)

    assert summary.total_rooms == 3
    assert summary.evaluated_rooms == 2
    assert summary.truncated is True
    assert summary.candidate_count == 1
    assert summary.candidates[0].room_id == room_a.id


@pytest.mark.asyncio
async def test_record_consolidation_candidate_events_is_non_destructive_and_deduped() -> None:
    wing_id = uuid.uuid4()
    item_id = uuid.uuid4()
    room_a = Room(
        id=uuid.uuid4(),
        tenant_id="default",
        wing_id=wing_id,
        slug="pricing-narrative",
        stable_key="product-growth:pricing-narrative",
        name="Pricing Narrative",
        state="active",
    )
    room_b = Room(
        id=uuid.uuid4(),
        tenant_id="default",
        wing_id=wing_id,
        slug="pricing-narratives",
        stable_key="product-growth:pricing-narratives",
        name="Pricing Narratives",
        state="active",
    )
    closet_a = RoomClosetArtifact(
        room_id=room_a.id,
        tenant_id="default",
        generation=3,
        drawer_refs=[{"item_id": str(item_id), "title": "Pricing memo"}],
        tag_profile={"pricing": 3, "launch": 2},
    )
    closet_b = RoomClosetArtifact(
        room_id=room_b.id,
        tenant_id="default",
        generation=3,
        drawer_refs=[{"item_id": str(item_id), "title": "Pricing memo"}],
        tag_profile={"pricing": 2, "launch": 1},
    )
    db = _ConsolidationCandidateDb(
        rooms=[(room_a, "Product / Growth"), (room_b, "Product / Growth")],
        closets=[closet_a, closet_b],
    )

    summary = await record_consolidation_candidate_events(db, tenant_id="default")

    assert summary.candidate_count == 1
    assert len(db.added) == 1
    assert db.commits == 1
    event = db.added[0]
    assert event.event_type == "consolidation-candidate"
    assert event.room_id == room_a.id
    assert event.payload["non_destructive"] is True
    assert event.payload["candidate_signature"] == _consolidation_candidate_signature(summary.candidates[0])


@pytest.mark.asyncio
async def test_retrieve_palace_merges_tenant_shared_when_agent_results_are_only_notes(monkeypatch) -> None:
    class FakeExecuteResult:
        def all(self):
            return []

    class FakeDb:
        async def execute(self, _statement):
            return FakeExecuteResult()

        async def get(self, _model, _key):
            return None

    class FakeEmbedder:
        def __init__(self) -> None:
            self.calls = 0

        async def embed_single(self, _query: str) -> list[float]:
            self.calls += 1
            return [0.1, 0.2, 0.3]

    class FakeSearchService:
        calls: list[tuple[str | None, list[float] | None]] = []

        def __init__(self, db, embedder, tenant_id: str = "default"):
            self.db = db
            self.embedder = embedder
            self.tenant_id = tenant_id

        async def vector_search(self, *, scope_type=None, query_vector=None, **kwargs):
            self.calls.append((scope_type, query_vector))
            if scope_type == "agent":
                return [
                    SearchResult(
                        item_id=uuid.uuid4(),
                        title="default: [Andrew] what do you know about Henry Intelligent Machines just based on memory",
                        summary="Nothing — I don't have any stored knowledge about Henry Intelligent Machines.",
                        source_type="note",
                        source_url=None,
                        tags=["scope-agent", "agent-orchestrator"],
                        created_at=datetime.now(timezone.utc),
                        chunk_text="I don't have any stored knowledge about Henry Intelligent Machines.",
                        chunk_index=0,
                        score=0.32,
                    ),
                    SearchResult(
                        item_id=uuid.uuid4(),
                        title="default: [Andrew] its not a fact card, it should be a media in tenant_shared scope",
                        summary="Got it — that's the gap. My recall was looking for a fact card, but Henry Intelligent Machines is stored as media in tenant_shared.",
                        source_type="note",
                        source_url=None,
                        tags=["scope-agent", "agent-orchestrator"],
                        created_at=datetime.now(timezone.utc),
                        chunk_text="# Conversation Turn\n\nThe issue is that Henry Intelligent Machines is stored as media in tenant_shared, not a fact card.",
                        chunk_index=0,
                        score=0.31,
                    )
                ]
            if scope_type == "tenant_shared":
                return [
                    SearchResult(
                        item_id=uuid.uuid4(),
                        title="https://x.com/AlexFinn/status/2041267605747712370",
                        summary="Henry Intelligent Machines is an AI agent platform.",
                        source_type="media",
                        source_url="https://x.com/AlexFinn/status/2041267605747712370",
                        tags=["ai-agents"],
                        created_at=datetime.now(timezone.utc),
                        chunk_text="Henry Intelligent Machines can autonomously execute business workflows.",
                        chunk_index=0,
                        score=0.50,
                    )
                ]
            return []

    async def fake_ensure_tenant_state(db, tenant_id):
        return SimpleNamespace(indexed_generation=0, active_generation=None)

    monkeypatch.setattr("app.services.palace.ensure_tenant_state", fake_ensure_tenant_state)
    monkeypatch.setattr("app.services.palace.SearchService", FakeSearchService)

    embedder = FakeEmbedder()
    response = await retrieve_palace(
        FakeDb(),
        tenant_id="default",
        embedder=embedder,
        body=SimpleNamespace(
            query="Henry Intelligent Machines",
            room_id=None,
            limit=5,
            scope_type="agent",
            scope_key="orchestrator",
            tags=None,
            tags_mode="any",
            min_score=0.3,
            date_from=None,
            date_to=None,
        ),
    )

    assert [(result.source_type, result.title) for result in response.results] == [
        ("media", "https://x.com/AlexFinn/status/2041267605747712370")
    ]
    assert any(step.title == "Shared memory merge" for step in response.trace.steps)
    assert any(step.title == "Conversation hygiene" for step in response.trace.steps)
    assert embedder.calls == 1
    assert [scope for scope, _ in FakeSearchService.calls] == ["agent", "tenant_shared"]
    assert FakeSearchService.calls[0][1] == FakeSearchService.calls[1][1]


@pytest.mark.asyncio
async def test_retrieve_palace_skips_tenant_shared_merge_when_scoped_result_is_curated(monkeypatch) -> None:
    class FakeExecuteResult:
        def all(self):
            return []

    class FakeDb:
        async def execute(self, _statement):
            return FakeExecuteResult()

        async def get(self, _model, _key):
            return None

    class FakeEmbedder:
        async def embed_single(self, _query: str) -> list[float]:
            return [0.1, 0.2, 0.3]

    class FakeSearchService:
        calls: list[str | None] = []

        def __init__(self, db, embedder, tenant_id: str = "default"):
            self.db = db
            self.embedder = embedder
            self.tenant_id = tenant_id

        async def vector_search(self, *, scope_type=None, **kwargs):
            self.calls.append(scope_type)
            return [
                SearchResult(
                    item_id=uuid.uuid4(),
                    title="Henry dossier",
                    summary="Curated shared-style context already exists in scoped retrieval.",
                    source_type="doc",
                    source_url=None,
                    tags=["ai-agents"],
                    created_at=datetime.now(timezone.utc),
                    chunk_text="Henry Intelligent Machines is an AI agent platform.",
                    chunk_index=0,
                    score=0.47,
                )
            ]

    async def fake_ensure_tenant_state(db, tenant_id):
        return SimpleNamespace(indexed_generation=0, active_generation=None)

    monkeypatch.setattr("app.services.palace.ensure_tenant_state", fake_ensure_tenant_state)
    monkeypatch.setattr("app.services.palace.SearchService", FakeSearchService)

    response = await retrieve_palace(
        FakeDb(),
        tenant_id="default",
        embedder=FakeEmbedder(),
        body=SimpleNamespace(
            query="Henry Intelligent Machines",
            room_id=None,
            limit=5,
            scope_type="agent",
            scope_key="orchestrator",
            tags=None,
            tags_mode="any",
            min_score=0.3,
            date_from=None,
            date_to=None,
        ),
    )

    assert response.results[0].source_type == "doc"
    assert all(step.title != "Shared memory merge" for step in response.trace.steps)
    assert FakeSearchService.calls == ["agent"]


@pytest.mark.asyncio
async def test_retrieve_palace_merges_global_results_when_room_route_confidence_is_low(monkeypatch) -> None:
    room = SimpleNamespace(
        id=uuid.uuid4(),
        name="Benchmark",
        snapshot_generation=0,
    )

    class FakeDb:
        def __init__(self) -> None:
            self.execute_count = 0

        async def execute(self, _statement):
            self.execute_count += 1
            if self.execute_count == 1:
                return _RowsResult(
                    [
                        (
                            room,
                            "Infra / Code / Agents",
                            "Benchmark groups NIST SP 800-160v1r1 engineering trustworthy secure systems.",
                        )
                    ]
                )
            return _RowsResult([])

        async def get(self, model, key):
            if model is Room and key == room.id:
                return room
            return None

    class FakeEmbedder:
        async def embed_single(self, _query: str) -> list[float]:
            return [0.1, 0.2, 0.3]

    class FakeSearchService:
        calls: list[list[uuid.UUID] | None] = []

        def __init__(self, db, embedder, tenant_id: str = "default"):
            self.db = db
            self.embedder = embedder
            self.tenant_id = tenant_id
            self.last_ranking_trace = None

        async def vector_search(self, *, room_ids=None, **kwargs):
            self.calls.append(room_ids)
            self.last_ranking_trace = {
                "ranking_features_version": 1,
                "source_ranking_enabled": False,
                "candidate_limit": 50,
                "candidate_count": 1,
                "results": [],
            }
            if room_ids:
                return [
                    SearchResult(
                        item_id=uuid.uuid4(),
                        title="NIST SP 800-160v1r1 - Engineering Trustworthy Secure Systems",
                        summary="Systems security engineering guidance.",
                        source_type="note",
                        source_url=None,
                        tags=["benchmark-run-20260428", "nist-sp800"],
                        created_at=datetime.now(timezone.utc),
                        chunk_text="Engineering trustworthy secure systems.",
                        chunk_index=0,
                        score=0.31,
                    )
                ]
            return [
                SearchResult(
                    item_id=uuid.uuid4(),
                    title="NIST SP 800-218 - Secure Software Development Framework",
                    summary="Secure software practices and tasks.",
                    source_type="note",
                    source_url=None,
                    tags=["benchmark-run-20260428", "nist-sp800"],
                    created_at=datetime.now(timezone.utc),
                    chunk_text="Secure Software Development Framework practices and tasks.",
                    chunk_index=0,
                    score=0.92,
                )
            ]

    async def fake_ensure_tenant_state(db, tenant_id):
        return SimpleNamespace(indexed_generation=0, active_generation=None)

    monkeypatch.setattr("app.services.palace.ensure_tenant_state", fake_ensure_tenant_state)
    monkeypatch.setattr("app.services.palace.SearchService", FakeSearchService)

    response = await retrieve_palace(
        FakeDb(),
        tenant_id="default",
        embedder=FakeEmbedder(),
        body=SimpleNamespace(
            query="secure software development framework practices tasks",
            room_id=None,
            limit=5,
            scope_type="tenant_shared",
            scope_key=None,
            tags=["benchmark-run-20260428", "nist-sp800"],
            tags_mode="all",
            min_score=0.3,
            date_from=None,
            date_to=None,
        ),
    )

    assert response.results[0].title == "NIST SP 800-218 - Secure Software Development Framework"
    assert FakeSearchService.calls == [[room.id], None]
    assert response.trace.fallback_used is False
    assert response.trace.completeness_warning is None
    assert response.trace.route_confidence == "low"
    assert response.trace.route_abstain_reason is None
    assert response.trace.route_candidate_count == 1
    assert response.trace.route_room_candidate_count == 1
    assert response.trace.route_global_candidate_count == 1
    assert response.trace.global_merge_rescued_results is True
    assert [trace.route for trace in response.trace.ranking_traces] == ["room_scoped", "global_merge"]
    assert any(step.title == "Tag-constrained global merge" for step in response.trace.steps)


@pytest.mark.asyncio
async def test_retrieve_palace_merges_tagged_global_results_for_confident_room_route(monkeypatch) -> None:
    room = SimpleNamespace(
        id=uuid.uuid4(),
        name="Risk Management Framework For Information Systems And Organizations",
        snapshot_generation=0,
    )
    expected_id = uuid.uuid4()
    adjacent_id = uuid.uuid4()

    class FakeDb:
        def __init__(self) -> None:
            self.execute_count = 0

        async def execute(self, _statement):
            self.execute_count += 1
            if self.execute_count == 1:
                return _RowsResult(
                    [
                        (
                            room,
                            "Security / Compliance",
                            "Risk management framework categorize select implement assess authorize monitor.",
                        )
                    ]
                )
            return _RowsResult([])

        async def get(self, model, key):
            if model is Room and key == room.id:
                return room
            return None

    class FakeEmbedder:
        async def embed_single(self, _query: str) -> list[float]:
            return [0.1, 0.2, 0.3]

    class FakeSearchService:
        calls: list[list[uuid.UUID] | None] = []

        def __init__(self, db, embedder, tenant_id: str = "default"):
            self.db = db
            self.embedder = embedder
            self.tenant_id = tenant_id
            self.last_ranking_trace = None

        async def vector_search(self, *, room_ids=None, **kwargs):
            self.calls.append(room_ids)
            self.last_ranking_trace = {
                "ranking_features_version": 1,
                "source_ranking_enabled": False,
                "candidate_limit": 50,
                "candidate_count": 2 if room_ids else 1,
                "results": [],
            }
            if room_ids:
                return [
                    SearchResult(
                        item_id=adjacent_id,
                        title="NIST SP 800-39 - Managing information security risk",
                        summary="Organization, mission, and information system view.",
                        source_type="note",
                        source_url=None,
                        tags=["benchmark-run-20260429-rel250i", "nist-sp800"],
                        created_at=datetime.now(timezone.utc),
                        chunk_text="Frame, assess, respond to, and monitor organizational risk.",
                        chunk_index=0,
                        score=0.74,
                    ),
                    SearchResult(
                        item_id=expected_id,
                        title="NIST SP 800-37r2 - Risk management framework",
                        summary="System life cycle approach for security and privacy.",
                        source_type="note",
                        source_url=None,
                        tags=["benchmark-run-20260429-rel250i", "nist-sp800"],
                        created_at=datetime.now(timezone.utc),
                        chunk_text="Categorize, select, implement, assess, authorize, and monitor.",
                        chunk_index=0,
                        score=0.73,
                    ),
                ]
            return [
                SearchResult(
                    item_id=expected_id,
                    title="NIST SP 800-37r2 - Risk management framework",
                    summary="System life cycle approach for security and privacy.",
                    source_type="note",
                    source_url=None,
                    tags=["benchmark-run-20260429-rel250i", "nist-sp800"],
                    created_at=datetime.now(timezone.utc),
                    chunk_text="Categorize, select, implement, assess, authorize, and monitor.",
                    chunk_index=0,
                    score=0.79,
                )
            ]

    async def fake_ensure_tenant_state(db, tenant_id):
        return SimpleNamespace(indexed_generation=0, active_generation=None)

    monkeypatch.setattr("app.services.palace.ensure_tenant_state", fake_ensure_tenant_state)
    monkeypatch.setattr("app.services.palace.SearchService", FakeSearchService)

    response = await retrieve_palace(
        FakeDb(),
        tenant_id="default",
        embedder=FakeEmbedder(),
        body=SimpleNamespace(
            query="risk management framework categorize select implement assess authorize monitor",
            room_id=None,
            limit=5,
            scope_type="tenant_shared",
            scope_key=None,
            tags=["benchmark-run-20260429-rel250i", "nist-sp800"],
            tags_mode="all",
            min_score=0.3,
            date_from=None,
            date_to=None,
        ),
    )

    assert response.results[0].item_id == expected_id
    assert FakeSearchService.calls == [[room.id], None]
    assert response.trace.fallback_used is False
    assert response.trace.completeness_warning is None
    assert response.trace.route_confidence == "high"
    assert response.trace.route_abstain_reason is None
    assert response.trace.route_room_candidate_count == 2
    assert response.trace.route_global_candidate_count == 1
    assert response.trace.global_merge_rescued_results is True
    global_trace = next(trace for trace in response.trace.ranking_traces if trace.route == "global_merge")
    assert global_trace.routing["global_merge_rescued_results"] is True
    assert any(
        step.title == "Tag-constrained global merge"
        and "routed corpus retrieval complete" in step.detail
        for step in response.trace.steps
    )


@pytest.mark.asyncio
async def test_retrieve_palace_uses_tag_constrained_search_without_global_fallback_when_no_room_matches(monkeypatch) -> None:
    class FakeDb:
        async def execute(self, _statement):
            return _RowsResult([])

        async def get(self, model, key):
            return None

    class FakeEmbedder:
        async def embed_single(self, _query: str) -> list[float]:
            return [0.1, 0.2, 0.3]

    class FakeSearchService:
        calls: list[list[uuid.UUID] | None] = []

        def __init__(self, db, embedder, tenant_id: str = "default"):
            self.db = db
            self.embedder = embedder
            self.tenant_id = tenant_id

        async def vector_search(self, *, room_ids=None, **kwargs):
            self.calls.append(room_ids)
            return [
                SearchResult(
                    item_id=uuid.uuid4(),
                    title="NIST SP 800-207 - Zero Trust Architecture",
                    summary="Policy engine and policy administrator guidance.",
                    source_type="note",
                    source_url=None,
                    tags=["benchmark-run-20260428", "nist-sp800"],
                    created_at=datetime.now(timezone.utc),
                    chunk_text="Zero trust architecture policy engine guidance.",
                    chunk_index=0,
                    score=0.88,
                )
            ]

    async def fake_ensure_tenant_state(db, tenant_id):
        return SimpleNamespace(indexed_generation=9, active_generation=None)

    monkeypatch.setattr("app.services.palace.ensure_tenant_state", fake_ensure_tenant_state)
    monkeypatch.setattr("app.services.palace.SearchService", FakeSearchService)

    response = await retrieve_palace(
        FakeDb(),
        tenant_id="default",
        embedder=FakeEmbedder(),
        body=SimpleNamespace(
            query="weekly zero trust architecture policy engine governance compliance identity devices",
            room_id=None,
            limit=5,
            scope_type="tenant_shared",
            scope_key=None,
            tags=["benchmark-run-20260428", "nist-sp800"],
            tags_mode="all",
            min_score=0.3,
            date_from=None,
            date_to=None,
        ),
    )

    assert FakeSearchService.calls == [None]
    assert response.trace.fallback_used is False
    assert response.trace.completeness_warning is None
    assert response.trace.route_confidence == "none"
    assert response.trace.route_abstain_reason == "no_matching_room"
    assert response.trace.route_candidate_count == 0
    assert response.trace.route_room_candidate_count is None
    assert response.trace.route_global_candidate_count == 1


@pytest.mark.asyncio
async def test_retrieve_palace_low_confidence_expansion_activates_used_tunnel(monkeypatch) -> None:
    source_room = SimpleNamespace(
        id=uuid.uuid4(),
        name="Retrieval Routing",
        snapshot_generation=4,
        membership_generation=4,
    )
    target_room = SimpleNamespace(
        id=uuid.uuid4(),
        name="Graph Evidence",
        snapshot_generation=4,
        membership_generation=4,
    )
    tunnel = RoomTunnel(
        id=uuid.uuid4(),
        tenant_id="default",
        source_room_id=source_room.id,
        target_room_id=target_room.id,
        tunnel_type="shared-tag",
        strength=0.62,
        activation_count=3,
        stability=0.7,
        last_activated_at=None,
    )
    result_id = uuid.uuid4()

    class FakeDb:
        def __init__(self) -> None:
            self.execute_calls = 0
            self.commits = 0
            self.rollbacks = 0

        async def execute(self, _statement):
            self.execute_calls += 1
            if self.execute_calls == 1:
                return _RowsResult([(source_room, "Memory", "retrieval graph routing")])
            if self.execute_calls == 2:
                return _RowsResult([(tunnel, target_room)])
            if self.execute_calls == 3:
                return _RowsResult([target_room.id])
            if self.execute_calls == 4:
                return _RowsResult([tunnel])
            raise AssertionError(f"unexpected execute call {self.execute_calls}")

        async def get(self, model, key):
            if model is Room and key == source_room.id:
                return source_room
            return None

        async def commit(self) -> None:
            self.commits += 1

        async def rollback(self) -> None:
            self.rollbacks += 1

    class FakeEmbedder:
        async def embed_single(self, _query: str) -> list[float]:
            return [0.1, 0.2, 0.3]

    class FakeSearchService:
        calls: list[list[uuid.UUID] | None] = []

        def __init__(self, db, embedder, tenant_id: str = "default"):
            self.last_ranking_trace = None

        async def vector_search(self, *, room_ids=None, **kwargs):
            self.calls.append(room_ids)
            self.last_ranking_trace = {
                "ranking_features_version": 2,
                "source_ranking_enabled": False,
                "candidate_limit": 40,
                "candidate_count": 1,
                "results": [
                    {
                        "item_id": str(result_id),
                        "source_type": "note",
                        "relationship_graph_score": 0.42,
                        "retrieval_hint_score": 0.31,
                        "base_score": 0.82,
                        "adjusted_score": 0.86,
                        "adjustments": {"relationship_graph": 0.04},
                    }
                ],
            }
            if room_ids:
                return [
                    SearchResult(
                        item_id=result_id,
                        title="Graph activation note",
                        summary="A note reached through neighboring room expansion.",
                        source_type="note",
                        source_url=None,
                        tags=["retrieval"],
                        created_at=datetime.now(timezone.utc),
                        chunk_text="Room tunnel activation should be recorded only when used.",
                        chunk_index=0,
                        score=0.82,
                    )
                ]
            return []

    async def fake_ensure_tenant_state(db, tenant_id):
        return SimpleNamespace(indexed_generation=4, active_generation=None)

    monkeypatch.setattr("app.services.palace.ensure_tenant_state", fake_ensure_tenant_state)
    monkeypatch.setattr("app.services.palace.SearchService", FakeSearchService)
    monkeypatch.setattr("app.services.palace._route_room_score", lambda *args, **kwargs: 0.4)

    db = FakeDb()
    response = await retrieve_palace(
        db,
        tenant_id="default",
        embedder=FakeEmbedder(),
        body=SimpleNamespace(
            query="retrieval graph routing",
            room_id=None,
            limit=5,
            scope_type="tenant_shared",
            scope_key=None,
            tags=None,
            tags_mode="any",
            min_score=0.3,
            date_from=None,
            date_to=None,
        ),
    )

    assert FakeSearchService.calls == [[source_room.id, target_room.id], None]
    assert response.trace.expanded_rooms == ["Graph Evidence"]
    assert tunnel.activation_count == 4
    assert tunnel.stability == 0.72
    assert tunnel.last_activated_at is not None
    assert db.commits == 1
    assert db.rollbacks == 0
    assert len(response.trace.activated_tunnels) == 1
    assert response.trace.activated_tunnels[0].activation_count == 4
    assert response.trace.activated_tunnels[0].target_room_id == target_room.id
    assert response.trace.ranking_traces[0].routing["activated_tunnel_count"] == 1
    assert response.trace.ranking_traces[0].results[0].relationship_graph_score == 0.42
    assert response.trace.ranking_traces[0].results[0].retrieval_hint_score == 0.31
    assert any(step.title == "Tunnel activation" for step in response.trace.steps)


@pytest.mark.asyncio
async def test_retrieve_palace_does_not_activate_tunnel_for_global_fallback_only(monkeypatch) -> None:
    source_room = SimpleNamespace(
        id=uuid.uuid4(),
        name="Retrieval Routing",
        snapshot_generation=4,
        membership_generation=4,
    )
    target_room = SimpleNamespace(
        id=uuid.uuid4(),
        name="Graph Evidence",
        snapshot_generation=4,
        membership_generation=4,
    )
    tunnel = RoomTunnel(
        id=uuid.uuid4(),
        tenant_id="default",
        source_room_id=source_room.id,
        target_room_id=target_room.id,
        tunnel_type="shared-tag",
        strength=0.62,
        activation_count=3,
        stability=0.7,
        last_activated_at=None,
    )

    class FakeDb:
        def __init__(self) -> None:
            self.execute_calls = 0
            self.commits = 0

        async def execute(self, _statement):
            self.execute_calls += 1
            if self.execute_calls == 1:
                return _RowsResult([(source_room, "Memory", "retrieval graph routing")])
            if self.execute_calls == 2:
                return _RowsResult([(tunnel, target_room)])
            if self.execute_calls == 3:
                return _RowsResult([])
            raise AssertionError(f"unexpected execute call {self.execute_calls}")

        async def get(self, model, key):
            if model is Room and key == source_room.id:
                return source_room
            return None

        async def commit(self) -> None:
            self.commits += 1

    class FakeEmbedder:
        async def embed_single(self, _query: str) -> list[float]:
            return [0.1, 0.2, 0.3]

    class FakeSearchService:
        calls: list[list[uuid.UUID] | None] = []

        def __init__(self, db, embedder, tenant_id: str = "default"):
            self.last_ranking_trace = None

        async def vector_search(self, *, room_ids=None, **kwargs):
            self.calls.append(room_ids)
            self.last_ranking_trace = {
                "ranking_features_version": 2,
                "source_ranking_enabled": False,
                "candidate_limit": 40,
                "candidate_count": 0 if room_ids else 1,
                "results": [],
            }
            if room_ids:
                return []
            return [
                SearchResult(
                    item_id=uuid.uuid4(),
                    title="Global fallback note",
                    summary="A note found outside the expanded room path.",
                    source_type="note",
                    source_url=None,
                    tags=["retrieval"],
                    created_at=datetime.now(timezone.utc),
                    chunk_text="Global fallback should not activate room tunnels.",
                    chunk_index=0,
                    score=0.78,
                )
            ]

    async def fake_ensure_tenant_state(db, tenant_id):
        return SimpleNamespace(indexed_generation=4, active_generation=None)

    monkeypatch.setattr("app.services.palace.ensure_tenant_state", fake_ensure_tenant_state)
    monkeypatch.setattr("app.services.palace.SearchService", FakeSearchService)
    monkeypatch.setattr("app.services.palace._route_room_score", lambda *args, **kwargs: 0.4)

    db = FakeDb()
    response = await retrieve_palace(
        db,
        tenant_id="default",
        embedder=FakeEmbedder(),
        body=SimpleNamespace(
            query="retrieval graph routing",
            room_id=None,
            limit=5,
            scope_type="tenant_shared",
            scope_key=None,
            tags=None,
            tags_mode="any",
            min_score=0.3,
            date_from=None,
            date_to=None,
        ),
    )

    assert FakeSearchService.calls == [[source_room.id, target_room.id], None]
    assert response.results
    assert response.trace.activated_tunnels == []
    assert response.trace.ranking_traces[0].routing["activated_tunnel_count"] == 0
    assert tunnel.activation_count == 3
    assert tunnel.last_activated_at is None
    assert db.commits == 0
    assert not any(step.title == "Tunnel activation" for step in response.trace.steps)


@pytest.mark.asyncio
async def test_retrieve_palace_abstains_when_room_signal_is_too_weak(monkeypatch) -> None:
    room = SimpleNamespace(
        id=uuid.uuid4(),
        name="Planning",
        snapshot_generation=0,
    )

    class FakeDb:
        async def execute(self, _statement):
            return _RowsResult([(room, "Operations", "weekly planning notes")])

        async def get(self, model, key):
            if model is Room and key == room.id:
                return room
            return None

    class FakeEmbedder:
        async def embed_single(self, _query: str) -> list[float]:
            return [0.1, 0.2, 0.3]

    class FakeSearchService:
        calls: list[list[uuid.UUID] | None] = []

        def __init__(self, db, embedder, tenant_id: str = "default"):
            self.db = db
            self.embedder = embedder
            self.tenant_id = tenant_id
            self.last_ranking_trace = None

        async def vector_search(self, *, room_ids=None, **kwargs):
            self.calls.append(room_ids)
            self.last_ranking_trace = {
                "ranking_features_version": 1,
                "source_ranking_enabled": False,
                "candidate_limit": 50,
                "candidate_count": 1,
                "results": [],
            }
            return [
                SearchResult(
                    item_id=uuid.uuid4(),
                    title="NIST SP 800-207 - Zero Trust Architecture",
                    summary="Policy engine and policy administrator guidance.",
                    source_type="note",
                    source_url=None,
                    tags=["nist-sp800"],
                    created_at=datetime.now(timezone.utc),
                    chunk_text="Zero trust architecture policy engine guidance.",
                    chunk_index=0,
                    score=0.88,
                )
            ]

    async def fake_ensure_tenant_state(db, tenant_id):
        return SimpleNamespace(indexed_generation=9, active_generation=None)

    monkeypatch.setattr("app.services.palace.ensure_tenant_state", fake_ensure_tenant_state)
    monkeypatch.setattr("app.services.palace.SearchService", FakeSearchService)

    response = await retrieve_palace(
        FakeDb(),
        tenant_id="default",
        embedder=FakeEmbedder(),
        body=SimpleNamespace(
            query="weekly zero trust architecture policy engine governance compliance identity devices",
            room_id=None,
            limit=5,
            scope_type="tenant_shared",
            scope_key=None,
            tags=None,
            tags_mode="any",
            min_score=0.3,
            date_from=None,
            date_to=None,
        ),
    )

    assert FakeSearchService.calls == [None]
    assert response.routed_room_id is None
    assert response.trace.route_confidence == "low"
    assert response.trace.route_abstain_reason == "low_confidence"
    assert response.trace.route_candidate_count == 1
    assert response.trace.route_room_candidate_count is None
    assert response.trace.route_global_candidate_count == 1
    assert response.trace.fallback_used is True
    assert response.trace.completeness_warning == "Global fallback used because room-scoped retrieval had low confidence."


@pytest.mark.asyncio
async def test_retrieve_palace_does_not_warn_for_room_fresh_at_own_membership_generation(monkeypatch) -> None:
    room = SimpleNamespace(
        id=uuid.uuid4(),
        name="Zero Trust Architecture",
        snapshot_generation=3,
        membership_generation=3,
    )

    class FakeDb:
        async def execute(self, _statement):
            return _RowsResult(
                [
                    (
                        room,
                        "Security / Compliance",
                        "Zero Trust Architecture guidance for policy engines.",
                    )
                ]
            )

        async def get(self, model, key):
            if model is Room and key == room.id:
                return room
            return None

    class FakeEmbedder:
        async def embed_single(self, _query: str) -> list[float]:
            return [0.1, 0.2, 0.3]

    class FakeSearchService:
        calls: list[list[uuid.UUID] | None] = []

        def __init__(self, db, embedder, tenant_id: str = "default"):
            self.db = db
            self.embedder = embedder
            self.tenant_id = tenant_id

        async def vector_search(self, *, room_ids=None, **kwargs):
            self.calls.append(room_ids)
            return [
                SearchResult(
                    item_id=uuid.uuid4(),
                    title="NIST SP 800-207 - Zero Trust Architecture",
                    summary="Policy engine and policy administrator guidance.",
                    source_type="note",
                    source_url=None,
                    tags=["benchmark-run-20260428", "nist-sp800"],
                    created_at=datetime.now(timezone.utc),
                    chunk_text="Zero trust architecture policy engine guidance.",
                    chunk_index=0,
                    score=0.88,
                )
            ]

    async def fake_ensure_tenant_state(db, tenant_id):
        return SimpleNamespace(indexed_generation=9, active_generation=None)

    monkeypatch.setattr("app.services.palace.ensure_tenant_state", fake_ensure_tenant_state)
    monkeypatch.setattr("app.services.palace.SearchService", FakeSearchService)

    response = await retrieve_palace(
        FakeDb(),
        tenant_id="default",
        embedder=FakeEmbedder(),
        body=SimpleNamespace(
            query="zero trust architecture policy engine",
            room_id=None,
            limit=5,
            scope_type="tenant_shared",
            scope_key=None,
            tags=["benchmark-run-20260428", "nist-sp800"],
            tags_mode="all",
            min_score=0.3,
            date_from=None,
            date_to=None,
        ),
    )

    assert FakeSearchService.calls == [[room.id], None]
    assert response.trace.fallback_used is False
    assert response.trace.completeness_warning is None
    assert response.trace.route_confidence == "high"
    assert response.trace.route_abstain_reason is None


@pytest.mark.asyncio
async def test_retrieve_palace_keeps_substantive_agent_note_when_shared_context_exists(monkeypatch) -> None:
    class FakeExecuteResult:
        def all(self):
            return []

    class FakeDb:
        async def execute(self, _statement):
            return FakeExecuteResult()

        async def get(self, _model, _key):
            return None

    class FakeEmbedder:
        async def embed_single(self, _query: str) -> list[float]:
            return [0.1, 0.2, 0.3]

    class FakeSearchService:
        def __init__(self, db, embedder, tenant_id: str = "default"):
            self.db = db
            self.embedder = embedder
            self.tenant_id = tenant_id

        async def vector_search(self, *, scope_type=None, **kwargs):
            if scope_type == "agent":
                return [
                    SearchResult(
                        item_id=uuid.uuid4(),
                        title="default: [Andrew] Henry launch planning note",
                        summary="Henry matters to us because it validates the agent-team product direction.",
                        source_type="note",
                        source_url=None,
                        tags=["scope-agent", "agent-orchestrator"],
                        created_at=datetime.now(timezone.utc),
                        chunk_text="# Conversation Turn\n\nHenry matters because it validates the agent-team product direction and the orchestrator should remember that.",
                        chunk_index=0,
                        score=0.34,
                    )
                ]
            return [
                SearchResult(
                    item_id=uuid.uuid4(),
                    title="https://x.com/AlexFinn/status/2041267605747712370",
                    summary="Henry Intelligent Machines is an AI agent platform.",
                    source_type="media",
                    source_url="https://x.com/AlexFinn/status/2041267605747712370",
                    tags=["ai-agents"],
                    created_at=datetime.now(timezone.utc),
                    chunk_text="Henry Intelligent Machines can autonomously execute business workflows.",
                    chunk_index=0,
                    score=0.50,
                )
            ]

    async def fake_ensure_tenant_state(db, tenant_id):
        return SimpleNamespace(indexed_generation=0, active_generation=None)

    monkeypatch.setattr("app.services.palace.ensure_tenant_state", fake_ensure_tenant_state)
    monkeypatch.setattr("app.services.palace.SearchService", FakeSearchService)

    response = await retrieve_palace(
        FakeDb(),
        tenant_id="default",
        embedder=FakeEmbedder(),
        body=SimpleNamespace(
            query="Henry Intelligent Machines",
            room_id=None,
            limit=5,
            scope_type="agent",
            scope_key="orchestrator",
            tags=None,
            tags_mode="any",
            min_score=0.3,
            date_from=None,
            date_to=None,
        ),
    )

    assert [result.source_type for result in response.results] == ["media", "note"]
    assert all(step.title != "Conversation hygiene" for step in response.trace.steps)


@pytest.mark.asyncio
async def test_build_overview_marks_room_artifacts_fresh_at_room_membership_generation(monkeypatch) -> None:
    wing_id = uuid.uuid4()
    fresh_room_id = uuid.uuid4()
    stale_room_id = uuid.uuid4()
    wing = SimpleNamespace(id=wing_id, slug="security-compliance", name="Security / Compliance")
    fresh_room = SimpleNamespace(
        id=fresh_room_id,
        wing_id=wing_id,
        name="Zero Trust Architecture",
        stable_key="security-compliance:zero-trust-architecture",
        state="active",
        membership_generation=3,
        snapshot_generation=3,
        tunnel_generation=3,
        redirect_room_id=None,
    )
    stale_room = SimpleNamespace(
        id=stale_room_id,
        wing_id=wing_id,
        name="Lagging Snapshot",
        stable_key="security-compliance:lagging-snapshot",
        state="active",
        membership_generation=5,
        snapshot_generation=4,
        tunnel_generation=5,
        redirect_room_id=None,
    )
    fresh_snapshot = SimpleNamespace(room_id=fresh_room_id, summary="Current room summary.", item_count=2)
    stale_snapshot = SimpleNamespace(room_id=stale_room_id, summary="Older room summary.", item_count=1)

    class FakeDb:
        def __init__(self) -> None:
            self.results = [
                _ScalarRowsResult([wing]),
                _ScalarRowsResult([stale_room, fresh_room]),
                _RowsResult([(fresh_room_id, 2), (stale_room_id, 1)]),
                _ScalarRowsResult([fresh_snapshot, stale_snapshot]),
                _RowsResult([]),
            ]

        async def execute(self, _statement):
            return self.results.pop(0)

    async def fake_ensure_tenant_state(_db, tenant_id: str):
        assert tenant_id == "default"
        return SimpleNamespace(dirty_generation=9, indexed_generation=9, active_generation=None, active_palace_run_id=None)

    monkeypatch.setattr("app.services.palace.ensure_tenant_state", fake_ensure_tenant_state)

    overview = await build_overview(FakeDb(), tenant_id="default")

    rooms = {room.name: room for room in overview.wings[0].rooms}
    assert rooms["Zero Trust Architecture"].membership_status.status == "fresh"
    assert rooms["Zero Trust Architecture"].membership_status.target_generation == 3
    assert rooms["Zero Trust Architecture"].snapshot_status.status == "fresh"
    assert rooms["Zero Trust Architecture"].snapshot_status.target_generation == 3
    assert rooms["Zero Trust Architecture"].tunnel_status.status == "fresh"
    assert rooms["Lagging Snapshot"].snapshot_status.status == "stale"
    assert rooms["Lagging Snapshot"].snapshot_status.target_generation == 5


@pytest.mark.asyncio
async def test_get_room_detail_marks_artifacts_against_room_membership_generation(monkeypatch) -> None:
    wing_id = uuid.uuid4()
    room_id = uuid.uuid4()
    room = SimpleNamespace(
        id=room_id,
        wing_id=wing_id,
        name="Room With Updated Membership",
        stable_key="security-compliance:room-with-updated-membership",
        state="active",
        tenant_id="default",
        membership_generation=7,
        snapshot_generation=6,
        tunnel_generation=7,
        redirect_room_id=None,
    )
    wing = SimpleNamespace(id=wing_id, name="Security / Compliance")
    snapshot = SimpleNamespace(room_id=room_id, summary="Previous summary.", item_count=4)

    class FakeDb:
        def __init__(self) -> None:
            self.results = [
                _ScalarRowsResult([snapshot]),
                _RowsResult([4]),
                _RowsResult([]),
                _RowsResult([]),
            ]

        async def get(self, model, key):
            if model is Room and key == room_id:
                return room
            if model is Wing and key == wing_id:
                return wing
            return None

        async def execute(self, _statement):
            return self.results.pop(0)

    async def fake_ensure_tenant_state(_db, tenant_id: str):
        assert tenant_id == "default"
        return SimpleNamespace(indexed_generation=12, active_generation=None)

    monkeypatch.setattr("app.services.palace.ensure_tenant_state", fake_ensure_tenant_state)

    detail = await get_room_detail(FakeDb(), tenant_id="default", room_id=room_id)

    assert detail.room.membership_status.status == "fresh"
    assert detail.room.membership_status.target_generation == 7
    assert detail.room.snapshot_status.status == "stale"
    assert detail.room.snapshot_status.target_generation == 7
    assert detail.room.tunnel_status.status == "fresh"


def test_normalized_allowed_extensions_accepts_markdown_only() -> None:
    assert _normalized_allowed_extensions(["md", ".md", "MD"]) == [".md"]


def test_is_blank_sync_text_treats_whitespace_notes_as_empty() -> None:
    assert _is_blank_sync_text("") is True
    assert _is_blank_sync_text("\n  \n\t") is True
    assert _is_blank_sync_text("# Heading") is False


@pytest.mark.asyncio
async def test_build_room_artifact_health_counts_closet_snapshot_and_tunnel_drift(monkeypatch) -> None:
    room_a = uuid.uuid4()
    room_b = uuid.uuid4()
    room_c = uuid.uuid4()
    room_d = uuid.uuid4()
    blocked_room_rows = [
        (room_c, "NIST SP 800-207", "security-compliance:nist-sp-800-207", 7, 5, 5, 5, "Security / Compliance")
    ]

    class FakeDb:
        def __init__(self) -> None:
            self.execute_count = 0

        async def execute(self, _statement):
            self.execute_count += 1
            if self.execute_count == 1:
                return _ScalarRowsResult([room_a, room_b, room_c, room_d])
            return _RowsResult(blocked_room_rows)

    async def fake_ensure_tenant_state(_db, tenant_id: str):
        assert tenant_id == "default"
        return SimpleNamespace(indexed_generation=6)

    async def fake_repairable_room_artifacts(_db, *, tenant_id: str, target_generation: int):
        assert tenant_id == "default"
        assert target_generation == 6
        return PalaceArtifactRepairPlan(
            snapshot_room_ids=(room_b,),
            tunnel_room_ids=(room_a, room_b),
            blocked_room_ids=(room_c,),
            closet_room_ids=(room_a,),
        )

    monkeypatch.setattr("app.services.palace.ensure_tenant_state", fake_ensure_tenant_state)
    monkeypatch.setattr("app.services.palace._repairable_room_artifacts", fake_repairable_room_artifacts)

    summary = await build_room_artifact_health(FakeDb(), tenant_id="default")

    assert summary.target_generation == 6
    assert summary.active_rooms == 4
    assert summary.blocked_rooms == 1
    assert [sample.room_name for sample in summary.blocked_room_samples] == ["NIST SP 800-207"]
    assert summary.blocked_room_samples[0].membership_generation == 7
    assert summary.closets.fresh == 2
    assert summary.closets.stale == 1
    assert summary.snapshots.fresh == 2
    assert summary.snapshots.stale == 1
    assert summary.tunnels.fresh == 1
    assert summary.tunnels.stale == 2


@pytest.mark.asyncio
async def test_build_webhook_health_counts_webhook_enabled_jobs() -> None:
    failed_job = SimpleNamespace(
        id=uuid.uuid4(),
        item_id=uuid.uuid4(),
        job_type="note",
        status="failed",
        error_message="receiver returned 500",
        created_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
    )
    queued_job = SimpleNamespace(
        id=uuid.uuid4(),
        item_id=uuid.uuid4(),
        job_type="webpage",
        status="queued",
        error_message=None,
        created_at=datetime.now(timezone.utc),
        completed_at=None,
    )

    class FakeDb:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, _statement):
            self.calls += 1
            if self.calls == 1:
                return _RowsResult([("failed", 1), ("queued", 1)])
            return _RowsResult([(failed_job, "Webhook launch note"), (queued_job, None)])

    summary = await _build_webhook_health(FakeDb(), "default")

    assert summary.configured == 2
    assert summary.pending == 1
    assert summary.terminal == 1
    assert summary.failed_jobs == 1
    assert summary.retryable_jobs == 1
    assert summary.recent_jobs[0].title == "Webhook launch note"
    assert summary.recent_jobs[0].terminal is True
    assert summary.recent_jobs[1].title == "webpage job"


@pytest.mark.asyncio
async def test_repair_stale_room_artifacts_rebuilds_snapshots_and_tunnels(monkeypatch) -> None:
    rebuilt_closets: list[tuple[uuid.UUID, int]] = []
    rebuilt_hints: list[tuple[uuid.UUID, int]] = []
    rebuilt_snapshots: list[tuple[uuid.UUID, int]] = []
    rebuilt_tunnels: list[tuple[set[uuid.UUID], int]] = []

    class FakeDb:
        def __init__(self) -> None:
            self.commits = 0

        async def commit(self) -> None:
            self.commits += 1

    db = FakeDb()
    room_a = uuid.uuid4()
    room_b = uuid.uuid4()

    async def fake_ensure_tenant_state(_db, tenant_id: str):
        assert tenant_id == "default"
        return SimpleNamespace(indexed_generation=6)

    async def fake_repairable_room_artifacts(_db, *, tenant_id: str, target_generation: int):
        assert tenant_id == "default"
        assert target_generation == 6
        return PalaceArtifactRepairPlan(
            snapshot_room_ids=(room_a,),
            tunnel_room_ids=(room_a, room_b),
            blocked_room_ids=(uuid.uuid4(),),
            closet_room_ids=(room_a,),
            retrieval_hint_room_ids=(room_b,),
        )

    async def fake_rebuild_room_closet_artifact(_db, *, tenant_id: str, room_id: uuid.UUID, generation: int):
        rebuilt_closets.append((room_id, generation))
        return SimpleNamespace()

    async def fake_rebuild_room_snapshot(_db, *, tenant_id: str, room_id: uuid.UUID, generation: int):
        rebuilt_snapshots.append((room_id, generation))

    async def fake_rebuild_room_retrieval_hints(_db, *, tenant_id: str, room_id: uuid.UUID, generation: int):
        rebuilt_hints.append((room_id, generation))
        return []

    async def fake_rebuild_tunnels(_db, *, tenant_id: str, room_ids: set[uuid.UUID], generation: int):
        rebuilt_tunnels.append((room_ids, generation))

    monkeypatch.setattr("app.services.palace.ensure_tenant_state", fake_ensure_tenant_state)
    monkeypatch.setattr("app.services.palace._repairable_room_artifacts", fake_repairable_room_artifacts)
    monkeypatch.setattr("app.services.palace._rebuild_room_closet_artifact", fake_rebuild_room_closet_artifact)
    monkeypatch.setattr("app.services.palace.rebuild_room_retrieval_hints", fake_rebuild_room_retrieval_hints)
    monkeypatch.setattr("app.services.palace._rebuild_room_snapshot", fake_rebuild_room_snapshot)
    monkeypatch.setattr("app.services.palace._rebuild_tunnels", fake_rebuild_tunnels)

    repair_plan = await repair_stale_room_artifacts(db, tenant_id="default")

    assert repair_plan.snapshot_room_ids == (room_a,)
    assert repair_plan.closet_room_ids == (room_a,)
    assert repair_plan.retrieval_hint_room_ids == (room_b,)
    assert repair_plan.tunnel_room_ids == (room_a, room_b)
    assert len(repair_plan.blocked_room_ids) == 1
    assert rebuilt_closets == [(room_a, 6)]
    assert rebuilt_hints == [(room_b, 6)]
    assert rebuilt_snapshots == [(room_a, 6)]
    assert rebuilt_tunnels == [({room_a, room_b}, 6)]
    assert db.commits == 1


@pytest.mark.asyncio
async def test_repair_stale_room_artifacts_skips_empty_generation(monkeypatch) -> None:
    class FakeDb:
        async def commit(self) -> None:
            raise AssertionError("commit should not run when there is no indexed generation")

    async def fake_ensure_tenant_state(_db, tenant_id: str):
        assert tenant_id == "default"
        return SimpleNamespace(indexed_generation=0)

    monkeypatch.setattr("app.services.palace.ensure_tenant_state", fake_ensure_tenant_state)

    repair_plan = await repair_stale_room_artifacts(FakeDb(), tenant_id="default")

    assert repair_plan == PalaceArtifactRepairPlan(
        snapshot_room_ids=(),
        tunnel_room_ids=(),
        blocked_room_ids=(),
    )


@pytest.mark.asyncio
async def test_recompute_stale_room_tunnels_rebuilds_incremental_batch(monkeypatch) -> None:
    room_a = uuid.uuid4()
    room_b = uuid.uuid4()
    rebuilt_tunnels: list[tuple[set[uuid.UUID], int]] = []

    class FakeDb:
        def __init__(self) -> None:
            self.commits = 0

        async def commit(self) -> None:
            self.commits += 1

    db = FakeDb()

    async def fake_ensure_tenant_state(_db, tenant_id: str):
        assert tenant_id == "default"
        return SimpleNamespace(indexed_generation=9)

    async def fake_stale_tunnel_room_ids(_db, *, tenant_id: str, limit: int):
        assert tenant_id == "default"
        assert limit == 2
        return (room_a, room_b)

    async def fake_rebuild_tunnels(_db, *, tenant_id: str, room_ids: set[uuid.UUID], generation: int):
        assert tenant_id == "default"
        rebuilt_tunnels.append((room_ids, generation))

    monkeypatch.setattr("app.services.palace.ensure_tenant_state", fake_ensure_tenant_state)
    monkeypatch.setattr("app.services.palace._stale_tunnel_room_ids", fake_stale_tunnel_room_ids)
    monkeypatch.setattr("app.services.palace._rebuild_tunnels", fake_rebuild_tunnels)

    result = await recompute_stale_room_tunnels(db, tenant_id="default", limit=2)

    assert result == PalaceTunnelRecomputeResult(room_ids=(room_a, room_b), target_generation=9)
    assert rebuilt_tunnels == [({room_a, room_b}, 9)]
    assert db.commits == 1


@pytest.mark.asyncio
async def test_repairable_room_artifacts_rebuilds_when_snapshot_row_is_missing() -> None:
    room_id = uuid.uuid4()

    repair_plan = await _repairable_room_artifacts(
        _RepairableArtifactsDb(
            [
                (room_id, 6, 6, 6, 6, 6, uuid.uuid4(), None, uuid.uuid4()),
            ]
        ),
        tenant_id="default",
        target_generation=6,
    )

    assert repair_plan == PalaceArtifactRepairPlan(
        snapshot_room_ids=(room_id,),
        tunnel_room_ids=(),
        blocked_room_ids=(),
    )


@pytest.mark.asyncio
async def test_repairable_room_artifacts_rebuilds_when_closet_row_is_missing() -> None:
    room_id = uuid.uuid4()

    repair_plan = await _repairable_room_artifacts(
        _RepairableArtifactsDb(
            [
                (room_id, 6, 6, 6, 6, 6, None, uuid.uuid4(), uuid.uuid4()),
            ]
        ),
        tenant_id="default",
        target_generation=6,
    )

    assert repair_plan == PalaceArtifactRepairPlan(
        snapshot_room_ids=(),
        tunnel_room_ids=(),
        blocked_room_ids=(),
        closet_room_ids=(room_id,),
    )


@pytest.mark.asyncio
async def test_repairable_room_artifacts_accepts_older_fresh_room_generation() -> None:
    room_id = uuid.uuid4()

    repair_plan = await _repairable_room_artifacts(
        _RepairableArtifactsDb(
            [
                (room_id, 3, 3, 3, 3, 3, uuid.uuid4(), uuid.uuid4(), uuid.uuid4()),
            ]
        ),
        tenant_id="default",
        target_generation=6,
    )

    assert repair_plan == PalaceArtifactRepairPlan(
        snapshot_room_ids=(),
        tunnel_room_ids=(),
        blocked_room_ids=(),
    )


@pytest.mark.asyncio
async def test_repairable_room_artifacts_rebuilds_when_retrieval_hint_row_is_missing() -> None:
    room_id = uuid.uuid4()

    repair_plan = await _repairable_room_artifacts(
        _RepairableArtifactsDb(
            [
                (room_id, 6, 6, 6, 6, 6, uuid.uuid4(), uuid.uuid4(), None),
            ]
        ),
        tenant_id="default",
        target_generation=6,
    )

    assert repair_plan == PalaceArtifactRepairPlan(
        snapshot_room_ids=(),
        tunnel_room_ids=(),
        blocked_room_ids=(),
        retrieval_hint_room_ids=(room_id,),
    )


@pytest.mark.asyncio
async def test_rebuild_room_closet_artifact_persists_compact_drawer_refs() -> None:
    room = SimpleNamespace(id=uuid.uuid4(), closet_generation=0)
    item = SimpleNamespace(
        id=uuid.uuid4(),
        title="Launch note",
        source_type="note",
        tags=["launch", "pricing", "launch"],
        created_at=datetime.now(timezone.utc),
    )
    db = _ClosetArtifactDb(room=room, rows=[(SimpleNamespace(), item)])

    closet = await _rebuild_room_closet_artifact(
        db,
        tenant_id="default",
        room_id=room.id,
        generation=7,
    )

    assert isinstance(closet, RoomClosetArtifact)
    assert closet.item_count == 1
    assert closet.drawer_refs == [
        {
            "item_id": str(item.id),
            "title": "Launch note",
            "source_type": "note",
            "tags": ["launch", "pricing", "launch"],
        }
    ]
    assert closet.tag_profile == {"launch": 2, "pricing": 1}
    assert room.closet_generation == 7
    assert db.added == [closet]


@pytest.mark.asyncio
async def test_rebuild_room_retrieval_hints_persists_canonical_item_chunk_refs() -> None:
    room = SimpleNamespace(id=uuid.uuid4(), retrieval_hint_generation=0)
    item = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id="default",
        title="NIST SP 800-37r2 - Risk Management Framework",
        summary="Official NIST RMF publication.",
        source_type="document",
        source_url="https://doi.org/10.6028/NIST.SP.800-37r2",
        tags=["nist-sp800", "benchmark-run-20260429-rel250i"],
        metadata_={"memory_entry": {"scope": {"type": "tenant_shared", "key": None}, "source": "nist"}},
        status="ready",
        created_at=datetime.now(timezone.utc),
    )
    db = _ClosetArtifactDb(
        room=room,
        rows=[
            (
                item,
                81,
                "Risk Management Framework steps select, implement, assess, authorize, and monitor controls.",
            )
        ],
    )

    hints = await rebuild_room_retrieval_hints(
        db,
        tenant_id="default",
        room_id=room.id,
        generation=8,
    )

    assert len(hints) == 1
    hint = hints[0]
    assert isinstance(hint, RetrievalHintArtifact)
    assert hint.source_item_id == item.id
    assert hint.source_chunk_index == 81
    assert hint.room_id == room.id
    assert hint.generation == 8
    assert hint.source_fingerprint
    assert hint.metadata_json["source_type"] == "document"
    assert "Risk Management Framework" in hint.hint_text
    assert room.retrieval_hint_generation == 8
    assert db.added == [hint]


@pytest.mark.asyncio
async def test_retrieval_hint_report_is_advisory_and_does_not_change_results() -> None:
    room_id = uuid.uuid4()
    returned_item_id = uuid.uuid4()
    missing_item_id = uuid.uuid4()
    result = SearchResult(
        item_id=returned_item_id,
        title="Existing RMF result",
        summary=None,
        source_type="document",
        source_url=None,
        tags=["nist-sp800"],
        created_at=datetime.now(timezone.utc),
        chunk_text="Risk Management Framework",
        chunk_index=0,
        score=0.7,
    )
    returned_hint = RetrievalHintArtifact(
        tenant_id="tenant-a",
        room_id=room_id,
        source_item_id=returned_item_id,
        source_chunk_index=0,
        generation=9,
        hint_text="risk management framework returned",
        source_fingerprint="a" * 64,
    )
    missing_hint = RetrievalHintArtifact(
        tenant_id="tenant-a",
        room_id=room_id,
        source_item_id=missing_item_id,
        source_chunk_index=4,
        generation=9,
        hint_text="risk management framework governing source",
        source_fingerprint="b" * 64,
    )

    class FakeDb:
        async def execute(self, _statement):
            return _ScalarRowsResult([returned_hint, missing_hint])

    report = await report_retrieval_hint_candidates(
        FakeDb(),
        tenant_id="tenant-a",
        query="risk management framework",
        current_results=[result],
        room_ids=[room_id],
        limit=5,
    )

    assert report["applied"] is False
    assert report["candidate_count"] == 2
    assert report["would_add_count"] == 1
    assert {row["item_id"] for row in report["candidates"]} == {str(returned_item_id), str(missing_item_id)}
    assert next(row for row in report["candidates"] if row["item_id"] == str(returned_item_id))[
        "already_returned"
    ] is True


@pytest.mark.asyncio
async def test_retrieve_palace_can_apply_high_confidence_retrieval_hint_rescue(monkeypatch) -> None:
    room = SimpleNamespace(
        id=uuid.uuid4(),
        name="Risk Management Framework",
        snapshot_generation=0,
    )
    returned_item_id = uuid.uuid4()
    missing_item_id = uuid.uuid4()
    returned_hint = RetrievalHintArtifact(
        tenant_id="default",
        room_id=room.id,
        source_item_id=returned_item_id,
        source_chunk_index=0,
        generation=9,
        hint_text="risk management framework returned",
        source_fingerprint="a" * 64,
    )
    missing_hint = RetrievalHintArtifact(
        tenant_id="default",
        room_id=room.id,
        source_item_id=missing_item_id,
        source_chunk_index=4,
        generation=9,
        hint_text="risk management framework governing source",
        source_fingerprint="b" * 64,
    )
    missing_item = SimpleNamespace(
        id=missing_item_id,
        title="NIST SP 800-37r2 - Risk Management Framework",
        summary="Governing source for the RMF lifecycle.",
        source_type="document",
        source_url="https://doi.org/10.6028/NIST.SP.800-37r2",
        tags=["nist-sp800", "benchmark-run-20260429-rel250i"],
        metadata_={},
        created_at=datetime.now(timezone.utc),
    )

    class FakeDb:
        def __init__(self) -> None:
            self.execute_count = 0

        async def execute(self, _statement):
            self.execute_count += 1
            if self.execute_count == 1:
                return _RowsResult(
                    [
                        (
                            room,
                            "Security / Compliance",
                            "Risk management framework categorize select implement assess authorize monitor.",
                        )
                    ]
                )
            if self.execute_count == 2:
                return _ScalarRowsResult([returned_hint, missing_hint])
            if self.execute_count == 3:
                return _RowsResult([(missing_hint, missing_item, 4, "Risk management framework governing source.")])
            return _RowsResult([])

        async def get(self, model, key):
            if model is Room and key == room.id:
                return room
            return None

    class FakeEmbedder:
        async def embed_single(self, _query: str) -> list[float]:
            return [0.1, 0.2, 0.3]

    class FakeSearchService:
        def __init__(self, db, embedder, tenant_id: str = "default"):
            self.db = db
            self.embedder = embedder
            self.tenant_id = tenant_id
            self.last_ranking_trace = {"result_count": 1}

        async def vector_search(self, *, room_ids=None, **kwargs):
            return [
                SearchResult(
                    item_id=returned_item_id,
                    title="Existing RMF result",
                    summary=None,
                    source_type="document",
                    source_url=None,
                    tags=["nist-sp800"],
                    created_at=datetime.now(timezone.utc),
                    chunk_text="Risk Management Framework",
                    chunk_index=0,
                    score=0.7,
                )
            ]

    async def fake_ensure_tenant_state(db, tenant_id):
        return SimpleNamespace(indexed_generation=0, active_generation=None)

    monkeypatch.setattr("app.services.palace.ensure_tenant_state", fake_ensure_tenant_state)
    monkeypatch.setattr("app.services.palace.SearchService", FakeSearchService)
    monkeypatch.setattr(palace_service.settings, "retrieval_hint_report_enabled", True)
    monkeypatch.setattr(palace_service.settings, "retrieval_hint_rescue_enabled", True)
    monkeypatch.setattr(palace_service.settings, "retrieval_hint_rescue_min_score", 0.8)
    monkeypatch.setattr(palace_service.settings, "retrieval_hint_rescue_limit", 2)

    response = await retrieve_palace(
        FakeDb(),
        tenant_id="default",
        embedder=FakeEmbedder(),
        body=SimpleNamespace(
            query="risk management framework governing source",
            room_id=None,
            limit=1,
            scope_type="tenant_shared",
            scope_key=None,
            tags=["nist-sp800"],
            tags_mode="any",
            min_score=0.3,
            date_from=None,
            date_to=None,
        ),
    )

    result_ids = [result.item_id for result in response.results]
    assert returned_item_id in result_ids
    assert missing_item_id in result_ids
    assert response.trace.hint_report["would_add_count"] == 1
    assert response.trace.hint_report["applied"] is True
    assert response.trace.hint_report["applied_count"] == 1


@pytest.mark.asyncio
async def test_retrieval_hint_rescue_ignores_returned_and_low_confidence_candidates() -> None:
    room_id = uuid.uuid4()
    returned_item_id = uuid.uuid4()
    weak_item_id = uuid.uuid4()
    returned_result = SearchResult(
        item_id=returned_item_id,
        title="Existing RMF result",
        summary=None,
        source_type="document",
        source_url=None,
        tags=["nist-sp800"],
        created_at=datetime.now(timezone.utc),
        chunk_text="Risk Management Framework",
        chunk_index=0,
        score=0.7,
    )
    returned_hint = RetrievalHintArtifact(
        tenant_id="tenant-a",
        room_id=room_id,
        source_item_id=returned_item_id,
        source_chunk_index=0,
        generation=9,
        hint_text="risk management framework governing source",
        source_fingerprint="a" * 64,
    )
    weak_hint = RetrievalHintArtifact(
        tenant_id="tenant-a",
        room_id=room_id,
        source_item_id=weak_item_id,
        source_chunk_index=1,
        generation=9,
        hint_text="risk notes",
        source_fingerprint="b" * 64,
    )
    weak_item = SimpleNamespace(
        id=weak_item_id,
        title="Weak related note",
        summary=None,
        source_type="note",
        source_url=None,
        tags=["nist-sp800"],
        created_at=datetime.now(timezone.utc),
    )

    class FakeDb:
        async def execute(self, _statement):
            return _RowsResult(
                [
                    (returned_hint, SimpleNamespace(id=returned_item_id), 0, "returned chunk"),
                    (weak_hint, weak_item, 1, "weak chunk"),
                ]
            )

    results = await retrieve_retrieval_hint_rescue_results(
        FakeDb(),
        tenant_id="tenant-a",
        query="risk management framework governing source",
        current_results=[returned_result],
        room_ids=[room_id],
        min_score=0.8,
        limit=5,
    )

    assert results == []


def test_sync_source_create_requires_bucket_for_s3() -> None:
    with pytest.raises(Exception):
        SyncSourceCreate(
            name="MinIO vault",
            source_kind="s3",
            endpoint_url="http://minio.minio.svc.cluster.local:9000",
        )


def test_sync_source_create_accepts_s3_shape() -> None:
    body = SyncSourceCreate(
        name="MinIO vault",
        source_kind="s3",
        bucket="palaceoftruth-corpus",
        prefix="/notes/",
        endpoint_url="http://minio.minio.svc.cluster.local:9000",
        force_path_style=True,
        allowed_extensions=["md"],
    )

    assert body.bucket == "palaceoftruth-corpus"
    assert body.prefix == "notes"
    assert body.allowed_extensions == [".md"]


def test_sync_source_create_accepts_repo_github_pat_shape() -> None:
    body = SyncSourceCreate(
        name="Private repo",
        source_kind="repo",
        root_path="https://github.com/palaceoftruth/palaceoftruth",
        credential_type="github_pat",
        github_pat="github_pat_123",
        allowed_extensions=["md"],
    )

    assert body.root_path == "https://github.com/palaceoftruth/palaceoftruth"
    assert body.credential_type == "github_pat"
    assert body.github_pat == "github_pat_123"
    assert body.allowed_extensions == [".md"]


def test_sync_source_create_rejects_repo_secret_for_folder() -> None:
    with pytest.raises(Exception):
        SyncSourceCreate(
            name="Folder",
            source_kind="folder",
            root_path="/tmp/folder",
            credential_type="deployment_github_pat",
        )


def test_normalize_sync_prefix_strips_slashes() -> None:
    assert _normalize_sync_prefix("/vault/notes/") == "vault/notes"
    assert _normalize_sync_prefix("") == ""


def test_sync_source_locator_formats_s3_sources() -> None:
    assert _sync_source_locator(source_kind="s3", bucket="vault", prefix="notes/2026") == "s3://vault/notes/2026"
    assert _sync_source_locator(source_kind="s3", bucket="vault", prefix=None) == "s3://vault"


def test_remote_github_repo_helpers_normalize_urls() -> None:
    https_url = "https://github.com/palaceoftruth/palaceoftruth"
    ssh_url = "git@github.com:palaceoftruth/palaceoftruth.git"

    assert _is_remote_github_repo(https_url) is True
    assert _is_remote_github_repo(ssh_url) is True
    assert _github_https_repo_url(ssh_url) == "https://github.com/palaceoftruth/palaceoftruth.git"
    assert _github_ssh_repo_url(https_url) == "git@github.com:palaceoftruth/palaceoftruth.git"
    assert _github_blob_url(https_url, "main", "content/launch.md") == (
        "https://github.com/palaceoftruth/palaceoftruth/blob/main/content/launch.md"
    )


def test_iter_s3_sync_files_filters_prefix_and_extensions(monkeypatch) -> None:
    class FakePaginator:
        def paginate(self, **_kwargs):
            return [
                {
                    "Contents": [
                        {
                            "Key": "notes/launch.md",
                            "ETag": '"abc123"',
                            "Size": 12,
                            "LastModified": datetime(2026, 4, 11, 12, 0, tzinfo=timezone.utc),
                        },
                        {
                            "Key": "notes/.env",
                            "ETag": '"skip-env"',
                            "Size": 8,
                            "LastModified": datetime(2026, 4, 11, 12, 1, tzinfo=timezone.utc),
                        },
                        {
                            "Key": "notes/raw.json",
                            "ETag": '"skip-json"',
                            "Size": 42,
                            "LastModified": datetime(2026, 4, 11, 12, 2, tzinfo=timezone.utc),
                        },
                    ]
                }
            ]

    class FakeClient:
        def get_paginator(self, name: str):
            assert name == "list_objects_v2"
            return FakePaginator()

        def get_object(self, *, Bucket: str, Key: str):
            assert Bucket == "vault"
            assert Key == "notes/launch.md"

            class Body:
                def read(self) -> bytes:
                    return b"# Launch"

                def close(self) -> None:
                    return None

            return {"Body": Body()}

    monkeypatch.setattr("app.services.palace._make_s3_client", lambda _source: FakeClient())

    source = SyncSource(
        tenant_id="default",
        name="Vault",
        root_path="s3://vault/notes",
        source_kind="s3",
        bucket="vault",
        prefix="notes",
        endpoint_url="http://minio.minio.svc.cluster.local:9000",
        force_path_style=True,
        allowed_extensions=[".md"],
        status="active",
        scan_interval_seconds=900,
    )

    files = _iter_s3_sync_files(source)

    assert len(files) == 1
    assert files[0].relative_path == "launch.md"
    assert files[0].source_url == "s3://vault/notes/launch.md"
    assert files[0].source_fingerprint == "abc123"
    assert files[0].load_text() == "# Launch"


def test_iter_repo_sync_files_builds_github_blob_urls(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    launch = repo_root / "content" / "launch.md"
    launch.parent.mkdir()
    launch.write_text("# Launch", encoding="utf-8")

    source = SyncSource(
        tenant_id="default",
        name="Repo",
        root_path="https://github.com/palaceoftruth/palaceoftruth",
        source_kind="repo",
        status="active",
        scan_interval_seconds=900,
    )

    files = _iter_repo_sync_files(repo_root, source=source, branch="main", allowed_extensions=[".md"])

    assert len(files) == 1
    assert files[0].relative_path == "content/launch.md"
    assert files[0].source_url == "https://github.com/palaceoftruth/palaceoftruth/blob/main/content/launch.md"
    assert files[0].load_text() == "# Launch"


@pytest.mark.asyncio
async def test_create_sync_source_encrypts_repo_pat(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.palace.settings.palaceoftruth_sync_source_credential_key",
        TEST_SYNC_SOURCE_CREDENTIAL_KEY,
    )

    class FakeDb:
        def add(self, _obj) -> None:
            return None

        async def commit(self) -> None:
            return None

        async def refresh(self, _obj) -> None:
            return None

    source = await create_sync_source(
        FakeDb(),
        tenant_id="tenant-a",
        body=SyncSourceCreate(
            name="Private repo",
            source_kind="repo",
            root_path="https://github.com/palaceoftruth/palaceoftruth",
            credential_type="github_pat",
            github_pat="github_pat_123",
        ),
    )

    assert source.credential_type == "github_pat"
    assert source.credential_ciphertext is not None
    assert source.credential_ciphertext != "github_pat_123"


@pytest.mark.asyncio
async def test_update_sync_source_keeps_existing_repo_pat_when_secret_is_omitted(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.palace.settings.palaceoftruth_sync_source_credential_key",
        TEST_SYNC_SOURCE_CREDENTIAL_KEY,
    )
    existing_ciphertext = _encrypt_repo_credential("github_pat_123")
    source = SyncSource(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        name="Private repo",
        root_path="https://github.com/palaceoftruth/palaceoftruth",
        source_kind="repo",
        credential_type="github_pat",
        credential_ciphertext=existing_ciphertext,
        status="active",
        scan_interval_seconds=900,
        allowed_extensions=[".md"],
    )

    class FakeDb:
        async def scalar(self, _statement):
            return None

        async def commit(self) -> None:
            return None

        async def refresh(self, _obj) -> None:
            return None

    updated = await update_sync_source(
        FakeDb(),
        tenant_id="tenant-a",
        source=source,
        body=SyncSourceUpdate(
            name="Private repo (rotated later)",
            scan_interval_seconds=1800,
        ),
    )

    assert updated.name == "Private repo (rotated later)"
    assert updated.scan_interval_seconds == 1800
    assert updated.credential_type == "github_pat"
    assert updated.credential_ciphertext == existing_ciphertext


@pytest.mark.asyncio
async def test_delete_sync_source_deactivates_owned_items_and_disables_source(monkeypatch, tmp_path: Path) -> None:
    source_id = uuid.uuid4()
    source = SyncSource(
        id=source_id,
        tenant_id="tenant-a",
        name="Private repo",
        root_path="https://github.com/palaceoftruth/palaceoftruth",
        source_kind="repo",
        credential_type="deployment_github_pat",
        status="active",
        scan_interval_seconds=900,
    )
    item = Item(
        id=uuid.uuid4(),
        source_type="note",
        source_url="https://github.com/palaceoftruth/palaceoftruth/blob/main/content/launch.md",
        title="Launch",
        raw_content="# Launch",
        tenant_id="tenant-a",
        status="ready",
        metadata_={"sync_source_id": str(source_id), "sync_active": True},
    )
    checkout_dir = tmp_path / "tenant-a" / str(source_id)
    checkout_dir.mkdir(parents=True)
    monkeypatch.setattr("app.services.palace.settings.palace_repo_checkout_root", str(tmp_path))

    dirty_calls: list[tuple[uuid.UUID, str]] = []

    async def fake_mark_item_dirty(db, *, tenant_id: str, item_id: uuid.UUID, reason: str, sync_source_id: uuid.UUID | None = None):
        dirty_calls.append((item_id, reason))
        return 5

    monkeypatch.setattr("app.services.palace.mark_item_dirty", fake_mark_item_dirty)

    class FakeResult:
        def __init__(self, items: list[Item]) -> None:
            self.items = items

        def scalars(self):
            return self

        def all(self):
            return self.items

    class FakeDb:
        def __init__(self) -> None:
            self.deleted: list[object] = []
            self.added: list[object] = []

        async def scalar(self, _statement):
            return None

        async def execute(self, _statement):
            return FakeResult([item])

        def add(self, obj) -> None:
            self.added.append(obj)

        async def delete(self, obj) -> None:
            self.deleted.append(obj)

        async def commit(self) -> None:
            return None

    db = FakeDb()
    deactivated = await delete_sync_source(db, tenant_id="tenant-a", source=source)

    assert deactivated == 1
    assert item.status == "failed"
    assert item.metadata_["sync_active"] is False
    assert item.metadata_["sync_deleted"] is True
    assert dirty_calls == [(item.id, "sync-source-delete")]
    assert source.status == "disabled"
    assert source.disabled_reason == "sync-source-removal"
    assert db.deleted == []
    assert db.added[0].event_type == "sync-source-disabled"
    assert checkout_dir.exists()


@pytest.mark.asyncio
async def test_load_sync_item_reuses_existing_source_url_when_mapping_is_missing() -> None:
    existing_item = Item(
        source_type="note",
        source_url="file:///mnt/palace-sync/05%20Themes/Content.md",
        title="Content",
        raw_content="# Content",
        tenant_id="default",
        status="ready",
        metadata_={},
    )

    class FakeDb:
        async def get(self, model, key):
            return None

        async def scalar(self, statement):
            compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
            assert "file:///mnt/palace-sync/05%20Themes/Content.md" in compiled
            assert "default" in compiled
            return existing_item

    item = await _load_sync_item(
        FakeDb(),
        tenant_id="default",
        row=None,
        source_url="file:///mnt/palace-sync/05%20Themes/Content.md",
    )

    assert item is existing_item
