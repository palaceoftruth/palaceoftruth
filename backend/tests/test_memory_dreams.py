import asyncio
import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace

from app.models.item import Item
from app.models.job import Job
from app.models.palace import TemporalFact
from app.services.memory_dreams import (
    MEMORY_DREAM_ARTIFACT_TYPES,
    MemoryDreamBatchResult,
    MemoryDreamKey,
    build_memory_dream_idempotency_key,
    generate_memory_dreams,
    memory_dream_target_days,
)
from app.workers.palace_tasks import run_memory_dream_refresh


class _ScalarRows:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return self._items


class FakeSession:
    def __init__(self, items, *, jobs=None, facts=None, state=None):
        self.items = list(items)
        self.jobs = list(jobs or [])
        self.facts = list(facts or [])
        self.state = state
        self.added = []
        self.commits = 0
        self.flushes = 0
        self.execute_calls = 0

    async def execute(self, _statement):
        self.execute_calls += 1
        if self.execute_calls == 1:
            return _ScalarRows(self.items)
        if self.execute_calls == 2:
            return _ScalarRows([(job.item_id, job.id) for job in self.jobs])
        if self.execute_calls == 3:
            return _ScalarRows([(fact.source_item_id, fact.id, fact.metadata_json) for fact in self.facts])
        return _ScalarRows([])

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


class SessionFactory:
    def __init__(self, session) -> None:
        self.session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _memory_note(
    *,
    item_id: uuid.UUID | None = None,
    title: str,
    body: str,
    created_at: datetime,
    scope_type: str = "agent",
    scope_key: str | None = "codex",
    summary: str | None = None,
    tags: list[str] | None = None,
    metadata_extra: dict | None = None,
    content_hash: str | None = None,
) -> Item:
    metadata = {
        "memory_entry": {
            "scope": {"type": scope_type, "key": scope_key},
            "source": "codex",
            "created_at": created_at.isoformat(),
        }
    }
    if metadata_extra:
        metadata.update(metadata_extra)
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
        tags=tags or ["codex-local-memory"],
        metadata_=metadata,
        content_hash=content_hash,
    )


def _existing_dream(*, key: MemoryDreamKey, source_items: list[Item]) -> Item:
    return Item(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        source_type="note",
        source_url=f"memory://dream/{key.artifact_type}/{key.scope_type}/{key.scope_key or 'shared'}/{key.day.isoformat()}",
        title=f"Memory Dream {key.day.isoformat()} [{key.scope_type}:{key.scope_key}] {key.artifact_type}",
        summary="stale summary",
        raw_content="stale body",
        status="ready",
        created_at=datetime(2026, 5, 5, 23, 50, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 5, 23, 50, tzinfo=timezone.utc),
        tags=["memory-dream", key.artifact_type],
        categories=["memory-dream"],
        idempotency_key=build_memory_dream_idempotency_key(tenant_id="tenant-a", key=key),
        metadata_={
            "memory_dream": {
                "schema_version": 1,
                "artifact_type": key.artifact_type,
                "day": key.day.isoformat(),
                "scope_type": key.scope_type,
                "scope_key": key.scope_key,
                "source_item_ids": [str(item.id) for item in source_items],
                "source_digests": [],
                "source_job_ids": [],
                "palace_generation": 7,
                "generated_at": "2026-05-05T23:50:00+00:00",
                "prompt_version": "deterministic-v1",
                "model_version": "deterministic",
                "confidence": 0.6,
                "claims_need_source": True,
                "artifact_metrics": {},
            },
            "memory_entry": {
                "scope": {"type": key.scope_type, "key": key.scope_key},
                "source": key.artifact_type,
            },
            "sync_relative_path": f"dreams/{key.day.isoformat()}/{key.scope_type}-{key.scope_key or 'shared'}-{key.artifact_type}.md",
        },
    )


def test_build_memory_dream_idempotency_key_is_stable() -> None:
    key = MemoryDreamKey(
        day=date(2026, 5, 5),
        scope_type="agent",
        scope_key="codex",
        artifact_type="palace-dream-summary",
    )

    assert build_memory_dream_idempotency_key(tenant_id="tenant-a", key=key) == build_memory_dream_idempotency_key(
        tenant_id="tenant-a",
        key=key,
    )


def test_memory_dream_target_days_replays_recent_completed_days() -> None:
    assert memory_dream_target_days(today=date(2026, 5, 6)) == (
        date(2026, 5, 4),
        date(2026, 5, 5),
    )


def test_generate_memory_dreams_creates_three_source_traceable_artifacts(monkeypatch) -> None:
    source_item = _memory_note(
        title="Codex recall setup",
        summary="Palace MCP is primary recall.",
        body="Codex should use scoped Palace MCP memory before local files.",
        created_at=datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc),
        tags=["codex-local-memory", "routing"],
        content_hash="a" * 64,
    )
    job = Job(id=uuid.uuid4(), item_id=source_item.id, job_type="memory_artifact", tenant_id="tenant-a")
    fact = TemporalFact(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        source_item_id=source_item.id,
        fact_key="f" * 64,
        source_fingerprint="s" * 64,
        subject="Codex",
        predicate="uses",
        object_text="Palace MCP",
        metadata_json={"contradiction_sweep": {"conflict_count": 1}},
    )
    session = FakeSession(
        [source_item],
        jobs=[job],
        facts=[fact],
        state=SimpleNamespace(tenant_id="tenant-a", indexed_generation=7),
    )
    processed = []

    async def fake_process_prebuilt_item(db, *, item, embedder, llm, tenant_id, job=None, enable_ai_enrichment=False):
        assert db is session
        assert tenant_id == "tenant-a"
        assert job is None
        assert enable_ai_enrichment is False
        item.status = "ready"
        processed.append(item)

    monkeypatch.setattr("app.services.memory_dreams.process_prebuilt_item", fake_process_prebuilt_item)

    result = asyncio.run(
        generate_memory_dreams(
            session,
            tenant_id="tenant-a",
            embedder=object(),
            llm=object(),
            target_day=date(2026, 5, 5),
        )
    )

    assert result == MemoryDreamBatchResult(created=3, updated=0, unchanged=0, deactivated=0)
    assert [item.metadata_["memory_dream"]["artifact_type"] for item in session.added] == list(MEMORY_DREAM_ARTIFACT_TYPES)
    assert processed == session.added
    for artifact in session.added:
        dream = artifact.metadata_["memory_dream"]
        assert dream["source_item_ids"] == [str(source_item.id)]
        assert dream["source_digests"] == ["a" * 64]
        assert dream["source_job_ids"] == [str(job.id)]
        assert dream["palace_generation"] == 7
        assert dream["claims_need_source"] is True
        assert artifact.metadata_["memory_entry"]["scope"] == {"type": "agent", "key": "codex"}
        assert artifact.categories == ["memory-dream"]
    hygiene = next(item for item in session.added if item.metadata_["memory_dream"]["artifact_type"] == "palace-hygiene-report")
    assert hygiene.metadata_["memory_dream"]["artifact_metrics"]["contradictions"] == 1


def test_generate_memory_dreams_skips_unchanged_artifacts(monkeypatch) -> None:
    source_item = _memory_note(
        title="Codex recall setup",
        body="Codex should use scoped Palace MCP memory before local files.",
        created_at=datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc),
    )
    preview_session = FakeSession([source_item], state=SimpleNamespace(tenant_id="tenant-a", indexed_generation=7))

    async def fake_process_prebuilt_item(_db, *, item, **_kwargs):
        item.status = "ready"

    monkeypatch.setattr("app.services.memory_dreams.process_prebuilt_item", fake_process_prebuilt_item)

    preview = asyncio.run(
        generate_memory_dreams(
            preview_session,
            tenant_id="tenant-a",
            embedder=object(),
            llm=object(),
            target_day=date(2026, 5, 5),
        )
    )
    assert preview.created == 3

    unchanged_session = FakeSession(
        [source_item, *preview_session.added],
        state=SimpleNamespace(tenant_id="tenant-a", indexed_generation=7),
    )

    async def fail_process(*_args, **_kwargs):
        raise AssertionError("unchanged dreams should not be reprocessed")

    monkeypatch.setattr("app.services.memory_dreams.process_prebuilt_item", fail_process)

    result = asyncio.run(
        generate_memory_dreams(
            unchanged_session,
            tenant_id="tenant-a",
            embedder=object(),
            llm=object(),
            target_day=date(2026, 5, 5),
        )
    )

    assert result == MemoryDreamBatchResult(created=0, updated=0, unchanged=3, deactivated=0)


def test_generate_memory_dreams_deactivates_missing_source_artifacts(monkeypatch) -> None:
    key = MemoryDreamKey(
        day=date(2026, 5, 5),
        scope_type="agent",
        scope_key="codex",
        artifact_type="palace-dream-summary",
    )
    existing = _existing_dream(key=key, source_items=[])
    session = FakeSession([existing], state=SimpleNamespace(tenant_id="tenant-a", indexed_generation=7))

    async def fail_process(*_args, **_kwargs):
        raise AssertionError("missing-source dreams should be deactivated, not reprocessed")

    monkeypatch.setattr("app.services.memory_dreams.process_prebuilt_item", fail_process)

    result = asyncio.run(
        generate_memory_dreams(
            session,
            tenant_id="tenant-a",
            embedder=object(),
            llm=object(),
            target_day=date(2026, 5, 5),
        )
    )

    assert result == MemoryDreamBatchResult(created=0, updated=0, unchanged=0, deactivated=1)
    assert existing.status == "failed"


def test_generate_memory_dreams_ignores_existing_derived_artifacts(monkeypatch) -> None:
    derived = _memory_note(
        title="Existing diary",
        body="derived body",
        created_at=datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc),
        metadata_extra={"diary_rollup": {"source_item_ids": ["x"]}},
    )
    session = FakeSession([derived], state=SimpleNamespace(tenant_id="tenant-a", indexed_generation=7))

    async def fail_process(*_args, **_kwargs):
        raise AssertionError("derived source artifacts should not feed dreams")

    monkeypatch.setattr("app.services.memory_dreams.process_prebuilt_item", fail_process)

    result = asyncio.run(
        generate_memory_dreams(
            session,
            tenant_id="tenant-a",
            embedder=object(),
            llm=object(),
            target_day=date(2026, 5, 5),
        )
    )

    assert result == MemoryDreamBatchResult(created=0, updated=0, unchanged=0, deactivated=0)


def test_run_memory_dream_refresh_replays_recent_days_for_each_tenant(monkeypatch) -> None:
    class EmptySession:
        pass

    fake_session = EmptySession()
    calls = []

    async def fake_list_tenants(db):
        assert db is fake_session
        return ("tenant-a", "tenant-b")

    async def fake_generate(db, *, tenant_id, embedder, llm, target_day):
        assert db is fake_session
        calls.append((tenant_id, target_day, embedder, llm))
        return MemoryDreamBatchResult(created=0, updated=0, unchanged=3, deactivated=0)

    monkeypatch.setattr("app.workers.palace_tasks.async_session", SessionFactory(fake_session))
    monkeypatch.setattr("app.workers.palace_tasks.list_fact_registry_tenants", fake_list_tenants)
    monkeypatch.setattr("app.workers.palace_tasks.generate_memory_dreams", fake_generate)
    monkeypatch.setattr(
        "app.workers.palace_tasks.memory_dream_target_days",
        lambda: (date(2026, 5, 4), date(2026, 5, 5)),
    )

    ctx = {"embedder": object(), "llm": object()}
    asyncio.run(run_memory_dream_refresh(ctx))

    assert calls == [
        ("tenant-a", date(2026, 5, 4), ctx["embedder"], ctx["llm"]),
        ("tenant-a", date(2026, 5, 5), ctx["embedder"], ctx["llm"]),
        ("tenant-b", date(2026, 5, 4), ctx["embedder"], ctx["llm"]),
        ("tenant-b", date(2026, 5, 5), ctx["embedder"], ctx["llm"]),
    ]
