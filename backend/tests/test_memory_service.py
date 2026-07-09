import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.models.item import Item
from app.models.job import Job
from app.models.palace import MemoryEntry, PalaceTenantState
from app.schemas.memory import (
    AgentMemoryRetrieveRequest,
    LegacyMemoryArtifactRequest,
    MemoryEntryRequest,
    MemoryRetrieveRequest,
    MemoryScope,
    MemoryScopeProfileUpsertRequest,
    SemanticRecallRequest,
    MemoryTrajectoryRequest,
)
from app.schemas.palace import PalaceRetrieveResponse, PalaceRetrieveTrace
from app.schemas.search import SearchResult
from app.services.memory import (
    MEMORY_JOB_TYPE,
    accept_canonical_memory_entry,
    accept_memory_artifact,
    build_memory_idempotency_key,
    build_memory_tags,
    get_memory_wakeup_brief,
    get_memory_scope_profile,
    list_memory_entries,
    list_memory_scopes,
    retrieve_memory,
    retrieve_agent_memory,
    retry_memory_job,
    semantic_recall_memory,
    serialize_memory_job,
    upsert_memory_scope_profile,
)
from app.services.memory_trajectory import retrieve_memory_trajectory
from app.services.memory_entries import source_project_from_memory_metadata
from app.services.memory_entries import normalize_legacy_memory_artifact, normalize_memory_entry
from app.services.memory_telemetry import memory_telemetry_snapshot, reset_memory_telemetry_for_tests
from app.services.palace import _append_search_ranking_trace


class FakeSession:
    def __init__(self, scalar_results=None, get_results=None, objects=None, execute_results=None) -> None:
        self.scalar_results = list(scalar_results or [])
        self.get_results = get_results or {}
        self.objects = objects or {}
        self.execute_results = list(execute_results or [])
        self.executed = []
        self.added = []
        self.commits = 0
        self.rollbacks = 0

    async def scalar(self, *args, **kwargs):
        if self.scalar_results:
            return self.scalar_results.pop(0)
        return None

    async def get(self, model, key):
        return self.objects.get((model, key), self.get_results.get(key))

    async def execute(self, statement, *_args, **_kwargs):
        self.executed.append(str(statement.compile(compile_kwargs={"literal_binds": True})))
        if self.execute_results:
            return _ScalarsResult(self.execute_results.pop(0))
        return _ScalarsResult([])

    async def scalars(self, statement, *_args, **_kwargs):
        self.executed.append(str(statement.compile(compile_kwargs={"literal_binds": True})))
        if self.execute_results:
            return _ScalarsResult(self.execute_results.pop(0))
        return _ScalarsResult([])

    def add(self, value) -> None:
        self.added.append(value)
        key = getattr(value, "id", None)
        if key is not None:
            self.objects[(type(value), key)] = value

    async def flush(self) -> None:
        for value in self.added:
            if getattr(value, "id", None) is None:
                value.id = uuid.uuid4()

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, value) -> None:
        if getattr(value, "id", None) is None:
            value.id = uuid.uuid4()
        if getattr(value, "created_at", None) is None:
            value.created_at = datetime.now(timezone.utc)

    async def rollback(self) -> None:
        self.rollbacks += 1


class FakeEmbedder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def embed_single(self, query: str) -> list[float]:
        self.calls.append(query)
        return [0.1, 0.2, 0.3]


class _ScalarsResult:
    def __init__(self, values) -> None:
        self.values = values

    def mappings(self):
        return self

    def scalars(self):
        return self

    def all(self):
        return self.values

    def one(self):
        if len(self.values) != 1:
            raise AssertionError(f"Expected exactly one row, got {len(self.values)}")
        return self.values[0]

    def one_or_none(self):
        if len(self.values) > 1:
            raise AssertionError(f"Expected at most one row, got {len(self.values)}")
        return self.values[0] if self.values else None

    def scalar_one(self):
        return self.values


def test_source_project_metadata_uses_normalized_agent_workspace() -> None:
    assert (
        source_project_from_memory_metadata(
            {"memory_entry": {"metadata": {"agent_workspace": "Palace Of Truth"}}}
        )
        == "palace-of-truth"
    )


def test_source_project_metadata_ignores_legacy_project_ids() -> None:
    assert (
        source_project_from_memory_metadata(
            {
                "memory_entry": {
                    "legacy_kind": "task_retrospective",
                    "scope": {"type": "workspace", "key": "exampleos"},
                },
                "memory_contract": {"project_id": "exampleos"},
            }
        )
        is None
    )


def _search_result(
    title: str,
    *,
    score: float,
    item_id: uuid.UUID | None = None,
    chunk_text: str | None = None,
    tags: list[str] | None = None,
    created_at: datetime | None = None,
    source_item_id: uuid.UUID | None = None,
    source_span: dict | None = None,
    retrieved_scope_label: str | None = None,
) -> SearchResult:
    return SearchResult(
        item_id=item_id or uuid.uuid4(),
        title=title,
        summary=None,
        source_type="note",
        source_url=None,
        tags=tags or ["codex-memory"],
        created_at=created_at or datetime.now(timezone.utc),
        chunk_text=chunk_text or f"{title} body",
        chunk_index=0,
        score=score,
        source_item_id=source_item_id,
        source_span=source_span,
        retrieved_scope_label=retrieved_scope_label,
    )


def test_memory_ranking_trace_is_bounded_and_redacted() -> None:
    trace = PalaceRetrieveTrace(requested_scope_type="workspace", requested_scope_key="launch-pad")
    service = SimpleNamespace(
        last_ranking_trace={
            "ranking_features_version": 1,
            "retrieval_lens": "engineering",
            "retrieval_lens_profile": {
                "name": "engineering",
                "trace_label": "engineering-context",
                "description": "safe",
                "prompt": "not exposed",
            },
            "source_ranking_enabled": True,
            "retrieval_hint_report": {"raw_hint": "not exposed"},
            "second_stage_reranker": {
                "enabled": True,
                "provider": "lexical-overlap",
                "status": "applied",
                "candidate_count": 20,
                "prompt": "not exposed",
            },
            "candidate_limit": 200,
            "candidate_count": 40,
            "trust_class_counts": {"low_support_generated": 30},
            "source_support_counts": {"unsupported": 30},
            "freshness_counts": {"undated": 30},
            "derived_raw_counts": {"derived": 30},
            "reuse_metrics": {
                "returned_to_client": 25,
                "answer_reuse_tracked": False,
            },
            "results": [
                {
                    "rank": index,
                    "item_id": str(uuid.uuid4()),
                    "title": "Sensitive title",
                    "chunk_text": "Sensitive source body",
                    "query": "private query",
                    "source_type": "note",
                    "artifact_provenance_type": "wakeup_brief",
                    "artifact_provenance_label": "Wake-up brief",
                    "derived_artifact_keys": ["wakeup_brief"],
                    "retrieved_scope_type": "workspace",
                    "retrieved_scope_key": "launch-pad",
                    "retrieved_scope_label": "workspace/launch-pad",
                    "trust_class": "low_support_generated",
                    "source_support_state": "unsupported",
                    "freshness": "undated",
                    "derived_raw_classification": "derived",
                    "reranker_score": 0.75,
                    "reranker_bonus": 0.06,
                    "reranker_provider": "lexical-overlap",
                    "reranker_reason": "query_token_overlap",
                    "base_score": 0.5 + index / 1000,
                    "adjusted_score": 0.6 + index / 1000,
                    "adjustments": {
                        "lexical_rescue": 0.05,
                        "second_stage_reranker": 0.06,
                        "ignored": "not numeric",
                    },
                }
                for index in range(30)
            ],
        }
    )

    _append_search_ranking_trace(
        trace,
        service,
        route="room_scoped",
        limit=50,
        routing={
            "scope_type": "workspace",
            "scope_key": "launch-pad",
            "raw_query": "private query",
            "fallback_used": False,
            "room_count": 3,
            "rooms": ["Sensitive Room"],
        },
    )

    assert len(trace.ranking_traces) == 1
    ranking_trace = trace.ranking_traces[0]
    assert ranking_trace.route == "room_scoped"
    assert ranking_trace.retrieval_lens == "engineering"
    assert ranking_trace.retrieval_lens_profile == {
        "name": "engineering",
        "trace_label": "engineering-context",
        "description": "safe",
    }
    assert ranking_trace.ranking_features_version == 1
    assert ranking_trace.second_stage_reranker == {
        "enabled": True,
        "provider": "lexical-overlap",
        "status": "applied",
        "candidate_count": 20,
    }
    assert ranking_trace.candidate_limit == 200
    assert ranking_trace.candidate_count == 40
    assert ranking_trace.trust_class_counts == {"low_support_generated": 30}
    assert ranking_trace.source_support_counts == {"unsupported": 30}
    assert ranking_trace.freshness_counts == {"undated": 30}
    assert ranking_trace.derived_raw_counts == {"derived": 30}
    assert ranking_trace.reuse_metrics == {
        "returned_to_client": 25,
        "answer_reuse_tracked": False,
    }
    assert ranking_trace.result_count == 25
    assert len(ranking_trace.results) == 25
    assert ranking_trace.results[0].adjustments == {
        "lexical_rescue": 0.05,
        "second_stage_reranker": 0.06,
    }
    assert ranking_trace.results[0].reranker_score == 0.75
    assert ranking_trace.results[0].reranker_bonus == 0.06
    assert ranking_trace.results[0].reranker_provider == "lexical-overlap"
    assert ranking_trace.results[0].reranker_reason == "query_token_overlap"
    assert ranking_trace.results[0].artifact_provenance_type == "wakeup_brief"
    assert ranking_trace.results[0].artifact_provenance_label == "Wake-up brief"
    assert ranking_trace.results[0].derived_artifact_keys == ["wakeup_brief"]
    assert ranking_trace.results[0].retrieved_scope_label == "workspace/launch-pad"
    assert ranking_trace.results[0].trust_class == "low_support_generated"
    assert ranking_trace.results[0].source_support_state == "unsupported"
    assert ranking_trace.results[0].freshness == "undated"
    assert ranking_trace.results[0].derived_raw_classification == "derived"
    assert ranking_trace.routing == {
        "scope_type": "workspace",
        "scope_key": "launch-pad",
        "fallback_used": False,
        "room_count": 3,
    }
    serialized = trace.model_dump(mode="json")
    assert "Sensitive title" not in str(serialized)
    assert "Sensitive source body" not in str(serialized)
    assert "private query" not in str(serialized)
    assert "raw_hint" not in str(serialized)


def _artifact(**overrides) -> LegacyMemoryArtifactRequest:
    base = {
        "tenant_id": "tenant-a",
        "company_id": "company-a",
        "memory_kind": "task_retrospective",
        "title": "Task Retrospective: task-123",
        "summary": "Morning review CTA instrumentation outcome.",
        "body": "task-123 was approved and instrumented for the morning review flow.",
        "tags": ["custom-tag"],
        "created_by_role": "agent",
        "source": "exampleos",
        "created_at": datetime(2026, 4, 8, 20, 0, tzinfo=timezone.utc),
        "task_id": "task-123",
        "outcome": "approved",
    }
    base.update(overrides)
    return LegacyMemoryArtifactRequest.model_validate(base)


def _entry(**overrides) -> MemoryEntryRequest:
    base = {
        "tenant_id": "tenant-a",
        "title": "Shared launch brief",
        "summary": "Cross-host workspace context.",
        "body": "Agents should reuse the same launch brief when they migrate hosts.",
        "source": "hermes",
        "created_at": datetime(2026, 4, 12, 12, 0, tzinfo=timezone.utc),
        "tags": ["launch"],
        "scope": {"type": "workspace", "key": "launch-pad"},
    }
    base.update(overrides)
    return MemoryEntryRequest.model_validate(base)


def test_build_memory_tags_applies_required_tag_matrix() -> None:
    engineering = build_memory_tags(_artifact())
    content = build_memory_tags(
        _artifact(
            memory_kind="content_approval",
            title="Content Approval: ticket-9",
            body="ticket-9 approved for launch on LinkedIn.",
            task_id=None,
            ticket_id="ticket-9",
            tags=["brand-voice"],
        )
    )
    founder = build_memory_tags(
        _artifact(
            memory_kind="founder_note",
            title="Founder Note",
            body="Founder note about next week launch priorities.",
            task_id=None,
            outcome=None,
            tags=["brand-voice"],
        )
    )

    assert "retrospective" in engineering
    assert "agent-retrospective" in engineering
    assert "task-task-123" in engineering
    assert "content-memory" in content
    assert "content-approved" in content
    assert "ticket-ticket-9" in content
    assert "founder-note" in founder
    assert "brand-voice" in founder


def test_build_memory_idempotency_key_is_stable_for_same_legacy_payload() -> None:
    first = build_memory_idempotency_key(_artifact())
    second = build_memory_idempotency_key(_artifact())
    changed = build_memory_idempotency_key(_artifact(outcome="rejected"))

    assert first == second
    assert first != changed


def test_accept_memory_artifact_creates_scoped_note_item_and_job() -> None:
    session = FakeSession()

    result = asyncio.run(
        accept_memory_artifact(
            session,
            body=_artifact(project_id="launch-pad", relationship_policy="deferred"),
            signing_key="signing-key",
        )
    )

    item = next(value for value in session.added if value.__class__.__name__ == "Item")
    job = next(value for value in session.added if value.__class__.__name__ == "Job")

    assert result.enqueue_requested is True
    assert result.job is job
    assert item.source_type == "note"
    assert item.idempotency_key
    assert item.metadata_["memory_contract"]["memory_kind"] == "task_retrospective"
    assert item.metadata_["memory_entry"]["scope"] == {"type": "workspace", "key": "launch-pad"}
    assert "scope-workspace" in item.tags
    assert "workspace-launch-pad" in item.tags
    assert job.job_type == MEMORY_JOB_TYPE
    assert job.payload["scope_type"] == "workspace"
    assert job.payload["scope_key"] == "launch-pad"
    assert job.payload["relationship_policy"] == "deferred"


def test_accept_canonical_memory_entry_uses_canonical_metadata() -> None:
    session = FakeSession()

    result = asyncio.run(
        accept_canonical_memory_entry(
            session,
            body=_entry(relationship_policy="skip"),
            signing_key="signing-key",
        )
    )

    item = next(value for value in session.added if value.__class__.__name__ == "Item")
    memory_entry = next(value for value in session.added if value.__class__.__name__ == "MemoryEntry")
    job = next(value for value in session.added if value.__class__.__name__ == "Job")

    assert result.accepted_as == "canonical"
    assert result.scope_type == "workspace"
    assert item.metadata_["memory_entry"]["scope"] == {"type": "workspace", "key": "launch-pad"}
    assert item.metadata_["memory_entry"]["source"] == "hermes"
    assert item.metadata_.get("memory_contract") is None
    assert memory_entry.item_id == item.id
    assert memory_entry.scope_type == "workspace"
    assert memory_entry.scope_key == "launch-pad"
    assert memory_entry.source == "hermes"
    assert memory_entry.idempotency_key == item.idempotency_key
    assert "scope-workspace" in item.tags
    assert job.payload["accepted_as"] == "canonical"
    assert job.payload["relationship_policy"] == "skip"
    assert isinstance(job.payload["request_fingerprint"], str)
    assert job.payload["memory_entry_id"] == str(memory_entry.id)


def test_accept_canonical_memory_entry_persists_temporal_fields_and_supersession_lineage() -> None:
    previous = MemoryEntry(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        item_id=uuid.uuid4(),
        scope_type="workspace",
        scope_key="launch-pad",
        source="hermes",
        source_url="memory://old",
        idempotency_key="old-key",
        created_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )
    session = FakeSession(scalar_results=[None, previous])
    valid_from = datetime(2026, 4, 12, 0, 0, tzinfo=timezone.utc)
    valid_until = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)

    asyncio.run(
        accept_canonical_memory_entry(
            session,
            body=_entry(
                idempotency_key="new-key",
                valid_from=valid_from,
                valid_until=valid_until,
                supersedes_entry_id=previous.id,
                fact_kind="experience",
                metadata={"agent_workspace": "Launch Pad"},
            ),
            signing_key="signing-key",
        )
    )

    memory_entry = next(
        value
        for value in session.added
        if value.__class__.__name__ == "MemoryEntry" and value.id != previous.id
    )
    assert memory_entry.valid_from == valid_from
    assert memory_entry.valid_until == valid_until
    assert memory_entry.supersedes_entry_id == previous.id
    assert memory_entry.fact_kind == "experience"
    assert memory_entry.metadata_ == {"agent_workspace": "Launch Pad"}
    assert previous.superseded_by_entry_id == memory_entry.id


def test_accept_canonical_memory_entry_rolls_back_invalid_supersession() -> None:
    session = FakeSession(scalar_results=[None, None])

    try:
        asyncio.run(
            accept_canonical_memory_entry(
                session,
                body=_entry(supersedes_entry_id=uuid.uuid4()),
                signing_key="signing-key",
            )
        )
    except HTTPException as exc:
        assert exc.status_code == 422
        assert exc.detail["status"] == "invalid_supersession"
    else:
        raise AssertionError("invalid supersedes_entry_id should fail closed")

    assert session.rollbacks == 1


def test_accept_canonical_memory_entry_rejects_already_superseded_entry() -> None:
    previous = MemoryEntry(
        id=uuid.uuid4(),
        item_id=uuid.uuid4(),
        tenant_id="tenant-a",
        scope_type="agent",
        scope_key="iris",
        source="hermes",
        superseded_by_entry_id=uuid.uuid4(),
    )
    session = FakeSession(scalar_results=[None, previous])

    try:
        asyncio.run(
            accept_canonical_memory_entry(
                session,
                body=_entry(supersedes_entry_id=previous.id),
                signing_key="signing-key",
            )
        )
    except HTTPException as exc:
        assert exc.status_code == 409
        assert exc.detail["status"] == "invalid_supersession"
        assert exc.detail["superseded_by_entry_id"] == str(previous.superseded_by_entry_id)
    else:
        raise AssertionError("already superseded lineage should fail closed")

    assert session.rollbacks == 1


def test_accept_canonical_memory_entry_rejects_supersession_cycle() -> None:
    first_id = uuid.uuid4()
    second_id = uuid.uuid4()
    first = MemoryEntry(
        id=first_id,
        item_id=uuid.uuid4(),
        tenant_id="tenant-a",
        scope_type="agent",
        scope_key="iris",
        source="hermes",
        supersedes_entry_id=second_id,
    )
    second = MemoryEntry(
        id=second_id,
        item_id=uuid.uuid4(),
        tenant_id="tenant-a",
        scope_type="agent",
        scope_key="iris",
        source="hermes",
        supersedes_entry_id=first_id,
    )
    session = FakeSession(scalar_results=[None, first, second])

    try:
        asyncio.run(
            accept_canonical_memory_entry(
                session,
                body=_entry(supersedes_entry_id=first.id),
                signing_key="signing-key",
            )
        )
    except HTTPException as exc:
        assert exc.status_code == 409
        assert exc.detail["status"] == "invalid_supersession"
        assert exc.detail["cycle_entry_id"] == str(first.id)
    else:
        raise AssertionError("supersession cycles should fail closed")

    assert session.rollbacks == 1


def test_accept_canonical_memory_entry_replays_same_payload_with_existing_pointers() -> None:
    entry = _entry(idempotency_key="shared-key")
    existing_item_id = uuid.uuid4()
    existing = Job(
        id=uuid.uuid4(),
        item_id=existing_item_id,
        job_type=MEMORY_JOB_TYPE,
        tenant_id="tenant-a",
        status="completed",
        progress=100,
        payload={
            "idempotency_key": "shared-key",
            "scope_type": "workspace",
            "scope_key": "launch-pad",
            "request_fingerprint": normalize_memory_entry(entry).request_fingerprint,
        },
    )
    session = FakeSession(scalar_results=[existing])

    result = asyncio.run(
        accept_canonical_memory_entry(
            session,
            body=entry,
            signing_key="signing-key",
        )
    )

    assert result.enqueue_requested is False
    assert result.replayed is True
    assert result.job is existing
    assert result.source_item_id == existing_item_id
    assert session.added == []


def test_accept_canonical_memory_entry_replays_legacy_job_without_stored_fingerprint() -> None:
    entry = _entry(idempotency_key="legacy-key")
    normalized = normalize_memory_entry(entry)
    existing_item = Item(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        title=entry.title,
        summary=entry.summary,
        source_type="note",
        source_url=entry.source_url,
        raw_content=entry.body,
        tags=normalized.tags,
        status="completed",
        created_at=entry.created_at,
        updated_at=entry.created_at,
        idempotency_key="legacy-key",
        metadata_=normalized.metadata,
    )
    existing = Job(
        id=uuid.uuid4(),
        item_id=existing_item.id,
        job_type=MEMORY_JOB_TYPE,
        tenant_id="tenant-a",
        status="queued",
        progress=0,
        payload={
            "idempotency_key": "legacy-key",
            "scope_type": "workspace",
            "scope_key": "launch-pad",
            "accepted_as": "canonical",
            "relationship_policy": entry.relationship_policy,
        },
    )
    session = FakeSession(scalar_results=[existing], get_results={existing_item.id: existing_item})

    result = asyncio.run(
        accept_canonical_memory_entry(
            session,
            body=entry,
            signing_key="signing-key",
        )
    )

    assert result.replayed is True
    assert result.job is existing
    assert result.source_item_id == existing_item.id


def test_accept_canonical_memory_entry_rejects_legacy_job_without_matching_payload() -> None:
    entry = _entry(idempotency_key="legacy-key")
    normalized = normalize_memory_entry(entry)
    existing_item = Item(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        title=entry.title,
        summary=entry.summary,
        source_type="note",
        source_url=entry.source_url,
        raw_content="A different persisted body.",
        tags=normalized.tags,
        status="completed",
        created_at=entry.created_at,
        updated_at=entry.created_at,
        idempotency_key="legacy-key",
        metadata_=normalized.metadata,
    )
    existing = Job(
        id=uuid.uuid4(),
        item_id=existing_item.id,
        job_type=MEMORY_JOB_TYPE,
        tenant_id="tenant-a",
        status="queued",
        progress=0,
        payload={
            "idempotency_key": "legacy-key",
            "scope_type": "workspace",
            "scope_key": "launch-pad",
            "accepted_as": "canonical",
            "relationship_policy": entry.relationship_policy,
        },
    )
    session = FakeSession(scalar_results=[existing], get_results={existing_item.id: existing_item})

    try:
        asyncio.run(
            accept_canonical_memory_entry(
                session,
                body=entry,
                signing_key="signing-key",
            )
        )
    except HTTPException as exc:
        assert exc.status_code == 409
        assert exc.detail["status"] == "duplicate_conflict"
        assert exc.detail["retryable"] is False
        assert exc.detail["conflict_kind"] == "payload_mismatch"
    else:
        raise AssertionError("legacy same key with mismatched persisted body should conflict")


def test_accept_canonical_memory_entry_rejects_same_key_different_payload() -> None:
    existing = Job(
        id=uuid.uuid4(),
        item_id=uuid.uuid4(),
        job_type=MEMORY_JOB_TYPE,
        tenant_id="tenant-a",
        status="queued",
        progress=0,
        payload={
            "idempotency_key": "shared-key",
            "scope_type": "workspace",
            "scope_key": "launch-pad",
            "request_fingerprint": normalize_memory_entry(_entry(idempotency_key="shared-key")).request_fingerprint,
        },
    )
    session = FakeSession(scalar_results=[existing])

    try:
        asyncio.run(
            accept_canonical_memory_entry(
                session,
                body=_entry(idempotency_key="shared-key", body="A different memory body."),
                signing_key="signing-key",
            )
        )
    except HTTPException as exc:
        assert exc.status_code == 409
        assert exc.detail["status"] == "duplicate_conflict"
        assert exc.detail["contract_status"] == "rejected"
        assert exc.detail["retryable"] is False
        assert exc.detail["conflict_kind"] == "payload_mismatch"
        assert exc.detail["existing_job_id"] == str(existing.id)
        assert exc.detail["existing_source_item_id"] == str(existing.item_id)
    else:
        raise AssertionError("same idempotency key with different payload should conflict")


def test_accept_canonical_memory_entry_rejects_same_key_different_scope() -> None:
    existing = Job(
        id=uuid.uuid4(),
        item_id=uuid.uuid4(),
        job_type=MEMORY_JOB_TYPE,
        tenant_id="tenant-a",
        status="queued",
        progress=0,
        payload={
            "idempotency_key": "shared-key",
            "scope_type": "workspace",
            "scope_key": "other-workspace",
            "request_fingerprint": normalize_memory_entry(_entry(idempotency_key="shared-key")).request_fingerprint,
        },
    )
    session = FakeSession(scalar_results=[existing])

    try:
        asyncio.run(
            accept_canonical_memory_entry(
                session,
                body=_entry(idempotency_key="shared-key"),
                signing_key="signing-key",
            )
        )
    except HTTPException as exc:
        assert exc.status_code == 409
        assert exc.detail["status"] == "duplicate_conflict"
        assert exc.detail["retryable"] is False
        assert exc.detail["conflict_kind"] == "scope_mismatch"
    else:
        raise AssertionError("same idempotency key with different scope should conflict")


def test_accept_canonical_memory_entry_insert_race_replays_existing_job() -> None:
    entry = _entry(idempotency_key="race-key")
    existing = Job(
        id=uuid.uuid4(),
        item_id=uuid.uuid4(),
        job_type=MEMORY_JOB_TYPE,
        tenant_id="tenant-a",
        status="queued",
        progress=0,
        payload={
            "idempotency_key": "race-key",
            "scope_type": "workspace",
            "scope_key": "launch-pad",
            "request_fingerprint": normalize_memory_entry(entry).request_fingerprint,
        },
    )

    class RaceSession(FakeSession):
        async def flush(self) -> None:
            raise IntegrityError("insert", {}, Exception("duplicate"))

    session = RaceSession(scalar_results=[None, existing])

    result = asyncio.run(
        accept_canonical_memory_entry(
            session,
            body=entry,
            signing_key="signing-key",
        )
    )

    assert session.rollbacks == 1
    assert result.replayed is True
    assert result.job is existing


def test_accept_memory_artifact_reuses_existing_job() -> None:
    artifact = _artifact()
    normalized = normalize_legacy_memory_artifact(artifact)
    item_id = uuid.uuid4()
    item = Item(
        id=item_id,
        tenant_id="tenant-a",
        title=artifact.title,
        summary=artifact.summary,
        source_type="note",
        status="processing",
        raw_content=artifact.body,
        tags=normalized.tags,
        created_at=artifact.created_at,
        updated_at=artifact.created_at,
        metadata_=normalized.metadata,
        idempotency_key=normalized.idempotency_key,
    )
    existing = Job(
        id=uuid.uuid4(),
        item_id=item_id,
        job_type=MEMORY_JOB_TYPE,
        tenant_id="tenant-a",
        status="queued",
        progress=0,
        payload={
            "idempotency_key": build_memory_idempotency_key(artifact),
            "relationship_policy": artifact.relationship_policy,
        },
    )
    session = FakeSession(scalar_results=[existing], get_results={item_id: item})

    result = asyncio.run(
        accept_memory_artifact(
            session,
            body=_artifact(webhook_url="https://example.com/hook"),
            signing_key="signing-key",
        )
    )

    assert result.enqueue_requested is False
    assert result.job is existing
    assert existing.webhook_url == "https://example.com/hook"
    assert existing.signing_key == "signing-key"
    assert session.added == []
    assert session.commits == 1


def test_accept_memory_artifact_requeues_stale_existing_job() -> None:
    item_id = uuid.uuid4()
    artifact = _artifact()
    normalized = normalize_legacy_memory_artifact(artifact)
    existing = Job(
        id=uuid.uuid4(),
        item_id=item_id,
        job_type=MEMORY_JOB_TYPE,
        tenant_id="tenant-a",
        status="processing",
        progress=65,
        error_message="worker disappeared",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=25),
        completed_at=datetime.now(timezone.utc),
        payload={
            "idempotency_key": build_memory_idempotency_key(artifact),
            "relationship_policy": artifact.relationship_policy,
        },
    )
    item = Item(
        id=item_id,
        tenant_id="tenant-a",
        title=artifact.title,
        summary=artifact.summary,
        source_type="note",
        status="failed",
        raw_content=artifact.body,
        tags=normalized.tags,
        created_at=artifact.created_at,
        updated_at=artifact.created_at,
        metadata_=normalized.metadata,
        idempotency_key=normalized.idempotency_key,
    )
    session = FakeSession(scalar_results=[existing], get_results={item_id: item})

    result = asyncio.run(
        accept_memory_artifact(
            session,
            body=_artifact(webhook_url="https://example.com/hook"),
            signing_key="signing-key",
        )
    )

    assert result.enqueue_requested is True
    assert result.job is existing
    assert existing.status == "queued"
    assert existing.progress == 0
    assert existing.error_message is None
    assert existing.completed_at is None
    assert existing.webhook_url == "https://example.com/hook"
    assert existing.signing_key == "signing-key"
    assert item.status == "processing"
    assert session.added == []
    assert session.commits == 1


def test_serialize_memory_job_maps_completed_to_complete() -> None:
    job = Job(
        id=uuid.uuid4(),
        job_type=MEMORY_JOB_TYPE,
        tenant_id="tenant-a",
        status="completed",
        progress=100,
        created_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
    )

    serialized = serialize_memory_job(job)

    assert serialized.job_id == job.id
    assert serialized.status == "complete"
    assert serialized.created_at == job.created_at


def test_retry_memory_job_requeues_failed_note() -> None:
    item_id = uuid.uuid4()
    job_id = uuid.uuid4()
    item = Item(
        id=item_id,
        source_type="note",
        title="Shared launch brief",
        summary="Cross-host workspace context.",
        raw_content="Agents should reuse the same launch brief when they migrate hosts.",
        metadata_={},
        tags=["launch"],
        categories=[],
        tenant_id="tenant-a",
        status="failed",
    )
    job = Job(
        id=job_id,
        item_id=item_id,
        job_type=MEMORY_JOB_TYPE,
        tenant_id="tenant-a",
        status="failed",
        progress=100,
        error_message="embedding timeout",
        completed_at=datetime.now(timezone.utc),
    )
    session = FakeSession(objects={(Item, item_id): item, (Job, job_id): job})

    retried = asyncio.run(retry_memory_job(session, tenant_id="tenant-a", job_id=job_id))

    assert retried is job
    assert job.status == "queued"
    assert job.progress == 0
    assert job.error_message is None
    assert job.completed_at is None
    assert job.duplicate_of is None
    assert item.status == "processing"
    assert session.commits == 1


def test_retry_memory_job_requires_source_note_content() -> None:
    item_id = uuid.uuid4()
    item = Item(
        id=item_id,
        source_type="note",
        title="Shared launch brief",
        summary="Cross-host workspace context.",
        raw_content=None,
        metadata_={},
        tags=["launch"],
        categories=[],
        tenant_id="tenant-a",
        status="failed",
    )
    job = Job(
        id=uuid.uuid4(),
        item_id=item_id,
        job_type=MEMORY_JOB_TYPE,
        tenant_id="tenant-a",
        status="failed",
        progress=100,
    )
    session = FakeSession(objects={(Item, item_id): item, (Job, job.id): job})

    try:
        asyncio.run(retry_memory_job(session, tenant_id="tenant-a", job_id=job.id))
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 409
        assert "Memory source note content is unavailable" in str(exc.detail)
    else:
        raise AssertionError("retry_memory_job should reject missing note content")


def test_get_memory_wakeup_brief_returns_latest_scoped_brief() -> None:
    older_id = uuid.uuid4()
    latest_id = uuid.uuid4()
    state = PalaceTenantState(tenant_id="tenant-a", indexed_generation=8)
    older = Item(
        id=older_id,
        source_type="note",
        source_url="memory://wakeup-brief/wing/product-growth/2026-04-22",
        title="Wake-up Brief 2026-04-22 [wing:product-growth]",
        summary="Older launch context.",
        raw_content="Older body",
        metadata_={
            "wakeup_brief": {
                "day": "2026-04-22",
                "scope_type": "wing",
                "scope_key": "product-growth",
                "generation": 7,
                "room_count": 1,
                "diary_count": 1,
                "fact_count": 1,
            }
        },
        tenant_id="tenant-a",
        status="ready",
        updated_at=datetime(2026, 4, 22, 6, 0, tzinfo=timezone.utc),
    )
    latest = Item(
        id=latest_id,
        source_type="note",
        source_url="memory://wakeup-brief/wing/product-growth/2026-04-23",
        title="Wake-up Brief 2026-04-23 [wing:product-growth]",
        summary="Current launch context.",
        raw_content="Current body",
        metadata_={
            "wakeup_brief": {
                "day": "2026-04-23",
                "scope_type": "wing",
                "scope_key": "product-growth",
                "generation": 7,
                "room_count": 3,
                "diary_count": 2,
                "fact_count": 5,
            }
        },
        tenant_id="tenant-a",
        status="ready",
        updated_at=datetime(2026, 4, 23, 6, 0, tzinfo=timezone.utc),
    )
    session = FakeSession(
        objects={(PalaceTenantState, "tenant-a"): state},
        execute_results=[[older, latest]],
    )

    brief = asyncio.run(
        get_memory_wakeup_brief(
            session,
            tenant_id="tenant-a",
            scope_type="wing",
            scope_key="product-growth",
        )
    )

    assert brief.source_item_id == latest_id
    assert brief.body == "Current body"
    assert brief.day == "2026-04-23"
    assert brief.generation == 7
    assert brief.indexed_generation == 8
    assert brief.freshness == "stale"
    assert brief.room_count == 3


def test_get_memory_wakeup_brief_validates_scope_shape() -> None:
    session = FakeSession()

    try:
        asyncio.run(
            get_memory_wakeup_brief(
                session,
                tenant_id="tenant-a",
                scope_type="wing",
                scope_key=None,
            )
        )
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 422
        assert "scope_key is required" in str(exc.detail)
    else:
        raise AssertionError("wing wake-up briefs should require a scope_key")


def test_list_memory_entries_filters_scope_tags_tenant_and_serializes_job_state() -> None:
    item_id = uuid.uuid4()
    job_id = uuid.uuid4()
    item = Item(
        id=item_id,
        source_type="note",
        source_url="memory://entry/workspace/launch-pad",
        title="Launch brief",
        summary="Cross-host launch context.",
        raw_content="Agents should reuse launch context.",
        metadata_={
            "memory_entry": {
                "source": "mcp",
                "scope": {"type": "workspace", "key": "launch-pad"},
                "metadata": {"agent_workspace": "Launch Pad"},
            }
        },
        tags=["launch", "agent-memory", "skill-codex-automation-handoff"],
        categories=[],
        tenant_id="tenant-a",
        status="ready",
        created_at=datetime(2026, 4, 12, 12, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 4, 12, 12, 3, tzinfo=timezone.utc),
    )
    job = Job(
        id=job_id,
        item_id=item_id,
        job_type=MEMORY_JOB_TYPE,
        tenant_id="tenant-a",
        status="completed",
    )
    memory_entry = MemoryEntry(
        id=uuid.uuid4(),
        item_id=item_id,
        tenant_id="tenant-a",
        scope_type="workspace",
        scope_key="launch-pad",
        source="mcp-relational",
        source_url="memory://entry/workspace/launch-pad",
        idempotency_key="entry-key",
        valid_from=datetime(2026, 4, 1, tzinfo=timezone.utc),
        valid_until=datetime(2026, 6, 1, tzinfo=timezone.utc),
        fact_kind="world",
        metadata_={"agent_workspace": "Launch Pad"},
        created_at=datetime(2026, 4, 12, 12, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 4, 12, 12, 3, tzinfo=timezone.utc),
    )
    session = FakeSession(execute_results=[1, [(item, job, memory_entry)]])

    listed = asyncio.run(
        list_memory_entries(
            session,
            tenant_id="tenant-a",
            scope=_entry().scope,
            tags=["launch", "agent-memory", "skill-codex-automation-handoff"],
            tags_mode="all",
            limit=20,
        )
    )

    assert listed.total == 1
    assert listed.next_cursor is None
    assert listed.entries[0].source_item_id == item_id
    assert listed.entries[0].entry_id == memory_entry.id
    assert listed.entries[0].scope.type == "workspace"
    assert listed.entries[0].scope.key == "launch-pad"
    assert listed.entries[0].source == "mcp-relational"
    assert listed.entries[0].source_project == "launch-pad"
    assert listed.entries[0].valid_from == datetime(2026, 4, 1, tzinfo=timezone.utc)
    assert listed.entries[0].valid_until == datetime(2026, 6, 1, tzinfo=timezone.utc)
    assert listed.entries[0].fact_kind == "world"
    assert listed.entries[0].system_tags == ["skill-codex-automation-handoff"]
    assert listed.entries[0].semantic_tags == ["launch", "agent-memory"]
    assert listed.entries[0].readiness_state == "ready"
    assert listed.entries[0].job_id == job_id
    assert listed.entries[0].job_status == "complete"
    compiled = "\n".join(session.executed)
    assert "items.tenant_id = 'tenant-a'" in compiled
    assert "jobs.tenant_id = 'tenant-a'" in compiled
    assert "jobs.job_type = 'memory_artifact'" in compiled
    assert "@>" in compiled


def test_list_memory_entries_uses_cursor_for_next_page() -> None:
    first = Item(
        id=uuid.uuid4(),
        source_type="note",
        title="First",
        metadata_={"memory_entry": {"scope": {"type": "tenant_shared"}}},
        tags=[],
        categories=[],
        tenant_id="tenant-a",
        status="processing",
        created_at=datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc),
    )
    extra = Item(
        id=uuid.uuid4(),
        source_type="note",
        title="Extra",
        metadata_={"memory_entry": {"scope": {"type": "tenant_shared"}}},
        tags=[],
        categories=[],
        tenant_id="tenant-a",
        status="processing",
        created_at=datetime(2026, 4, 12, 12, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 4, 12, 12, 0, tzinfo=timezone.utc),
    )
    session = FakeSession(execute_results=[2, [(first, None, None), (extra, None, None)]])

    listed = asyncio.run(
        list_memory_entries(
            session,
            tenant_id="tenant-a",
            scope=MemoryEntryRequest.model_validate(
                {
                    "tenant_id": "tenant-a",
                    "title": "x",
                    "body": "x",
                    "source": "x",
                    "created_at": datetime.now(timezone.utc),
                    "scope": {"type": "tenant_shared"},
                }
            ).scope,
            limit=1,
            cursor=datetime(2026, 4, 14, 0, 0, tzinfo=timezone.utc),
        )
    )

    assert listed.total == 2
    assert [entry.title for entry in listed.entries] == ["First"]
    assert listed.next_cursor == extra.created_at
    assert "items.created_at < '2026-04-14" in "\n".join(session.executed)


def _semantic_memory_row(
    *,
    title: str,
    body: str,
    scope_key: str,
    valid_from: datetime | None,
    valid_until: datetime | None = None,
    superseded_by_entry_id: uuid.UUID | None = None,
    supersedes_entry_id: uuid.UUID | None = None,
    fact_kind: str | None = "experience",
    created_at: datetime | None = None,
    metadata: dict | None = None,
) -> tuple[Item, MemoryEntry]:
    item_id = uuid.uuid4()
    entry_id = uuid.uuid4()
    item = Item(
        id=item_id,
        source_type="note",
        title=title,
        summary=body,
        raw_content=body,
        metadata_=metadata or {},
        tags=["agent-memory", "scope-agent", f"agent-{scope_key}"],
        categories=[],
        tenant_id="tenant-a",
        status="ready",
        created_at=created_at or datetime(2026, 7, 1, tzinfo=timezone.utc),
        updated_at=created_at or datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    entry = MemoryEntry(
        id=entry_id,
        item_id=item_id,
        tenant_id="tenant-a",
        scope_type="agent",
        scope_key=scope_key,
        source="hermes",
        source_url=f"memory://entry/agent/{scope_key}/{entry_id}",
        valid_from=valid_from,
        valid_until=valid_until,
        supersedes_entry_id=supersedes_entry_id,
        superseded_by_entry_id=superseded_by_entry_id,
        fact_kind=fact_kind,
        metadata_={},
        created_at=item.created_at,
        updated_at=item.updated_at,
    )
    return item, entry


def test_semantic_recall_prefers_current_entry_and_marks_superseded() -> None:
    old_item, old_entry = _semantic_memory_row(
        title="Iris staging port was 8443",
        body="Iris used staging port 8443 for the Palace deploy.",
        scope_key="iris",
        valid_from=datetime(2026, 4, 1, tzinfo=timezone.utc),
        valid_until=datetime(2026, 6, 1, tzinfo=timezone.utc),
        created_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )
    current_item, current_entry = _semantic_memory_row(
        title="Iris staging port is 9443",
        body="Iris currently uses staging port 9443 for the Palace deploy.",
        scope_key="iris",
        valid_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
        supersedes_entry_id=old_entry.id,
        created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    old_entry.superseded_by_entry_id = current_entry.id
    session = FakeSession(execute_results=[2, [(old_item, old_entry), (current_item, current_entry)]])

    response = asyncio.run(
        semantic_recall_memory(
            session,
            tenant_id="tenant-a",
            body=SemanticRecallRequest(
                scope_type="agent",
                scope_key="iris",
                query="Iris staging port",
                top_k=2,
            ),
        )
    )

    assert [item.entry_id for item in response.items] == [current_entry.id, old_entry.id]
    assert response.items[0].temporal_status == "current"
    assert response.items[1].temporal_status == "superseded"
    assert response.total_considered == 2


def test_semantic_recall_valid_at_returns_historical_entry_only() -> None:
    old_item, old_entry = _semantic_memory_row(
        title="Iris staging port was 8443",
        body="Iris used staging port 8443 for the Palace deploy.",
        scope_key="iris",
        valid_from=datetime(2026, 4, 1, tzinfo=timezone.utc),
        valid_until=datetime(2026, 6, 1, tzinfo=timezone.utc),
        created_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )
    session = FakeSession(execute_results=[1, [(old_item, old_entry)]])

    response = asyncio.run(
        semantic_recall_memory(
            session,
            tenant_id="tenant-a",
            body=SemanticRecallRequest(
                scope_type="agent",
                scope_key="iris",
                query="staging port",
                valid_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
                top_k=5,
            ),
        )
    )

    assert [item.entry_id for item in response.items] == [old_entry.id]
    assert response.items[0].temporal_status == "current"
    compiled = "\n".join(session.executed)
    assert "memory_entries.valid_from IS NULL OR memory_entries.valid_from <= '2026-04-01" in compiled
    assert "memory_entries.valid_until IS NULL OR memory_entries.valid_until > '2026-04-01" in compiled


def test_semantic_recall_valid_at_keeps_open_ended_superseded_entry_current() -> None:
    old_item, old_entry = _semantic_memory_row(
        title="Iris staging port was 8443",
        body="Iris used staging port 8443 for the Palace deploy.",
        scope_key="iris",
        valid_from=datetime(2026, 4, 1, tzinfo=timezone.utc),
        valid_until=None,
        superseded_by_entry_id=uuid.uuid4(),
        created_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )
    session = FakeSession(execute_results=[1, [(old_item, old_entry)]])

    response = asyncio.run(
        semantic_recall_memory(
            session,
            tenant_id="tenant-a",
            body=SemanticRecallRequest(
                scope_type="agent",
                scope_key="iris",
                query="staging port",
                valid_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            ),
        )
    )

    assert [item.entry_id for item in response.items] == [old_entry.id]
    assert response.items[0].temporal_status == "current"
    compiled = "\n".join(session.executed)
    assert "memory_entries.valid_until IS NOT NULL" in compiled


def test_semantic_recall_default_marks_open_ended_superseded_entry() -> None:
    old_item, old_entry = _semantic_memory_row(
        title="Iris staging port was 8443",
        body="Iris used staging port 8443 for the Palace deploy.",
        scope_key="iris",
        valid_from=datetime(2026, 4, 1, tzinfo=timezone.utc),
        valid_until=None,
        superseded_by_entry_id=uuid.uuid4(),
        created_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )
    session = FakeSession(execute_results=[1, [(old_item, old_entry)]])

    response = asyncio.run(
        semantic_recall_memory(
            session,
            tenant_id="tenant-a",
            body=SemanticRecallRequest(
                scope_type="agent",
                scope_key="iris",
                query="staging port",
            ),
        )
    )

    assert [item.entry_id for item in response.items] == [old_entry.id]
    assert response.items[0].temporal_status == "superseded"


def test_semantic_recall_keeps_agent_scope_strict_even_for_matching_sibling_entry() -> None:
    iris_item, iris_entry = _semantic_memory_row(
        title="Iris staging port is 9443",
        body="Iris staging port matches the query.",
        scope_key="iris",
        valid_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    session = FakeSession(execute_results=[1, [(iris_item, iris_entry)]])

    response = asyncio.run(
        semantic_recall_memory(
            session,
            tenant_id="tenant-a",
            body=SemanticRecallRequest(
                scope_type="agent",
                scope_key="iris",
                query="staging port perfect match",
                top_k=5,
            ),
        )
    )

    assert [item.scope.key for item in response.items] == ["iris"]
    compiled = "\n".join(session.executed)
    assert "memory_entries.scope_type = 'agent'" in compiled
    assert "memory_entries.scope_key = 'iris'" in compiled
    assert "vera" not in compiled
    assert "eva" not in compiled
    assert "ORDER BY CASE" in compiled
    assert compiled.index("ORDER BY CASE") < compiled.index("semantic_score DESC")


def test_semantic_recall_filters_fact_kind_and_keeps_date_filters_distinct() -> None:
    item, entry = _semantic_memory_row(
        title="Iris observed SAR-1037",
        body="Iris observed SAR-1037 recall behavior.",
        scope_key="iris",
        valid_from=datetime(2026, 7, 1, tzinfo=timezone.utc),
        fact_kind="observation",
    )
    session = FakeSession(execute_results=[1, [(item, entry)]])

    response = asyncio.run(
        semantic_recall_memory(
            session,
            tenant_id="tenant-a",
            body=SemanticRecallRequest(
                scope_type="agent",
                scope_key="iris",
                query="SAR-1037 recall",
                fact_kind_filter=["observation"],
                date_from=datetime(2026, 7, 1, tzinfo=timezone.utc),
                date_to=datetime(2026, 7, 2, tzinfo=timezone.utc),
            ),
        )
    )

    assert response.items[0].fact_kind == "observation"
    assert response.trace.fact_kind_filter == ["observation"]
    compiled = "\n".join(session.executed)
    assert "memory_entries.fact_kind IN ('observation')" in compiled
    assert "items.created_at >= '2026-07-01" in compiled
    assert "items.created_at <= '2026-07-02" in compiled


def test_semantic_recall_empty_result_returns_success_trace() -> None:
    reset_memory_telemetry_for_tests()
    session = FakeSession(execute_results=[0, []])

    response = asyncio.run(
        semantic_recall_memory(
            session,
            tenant_id="tenant-a",
            body=SemanticRecallRequest(
                scope_type="agent",
                scope_key="iris",
                query="nothing here",
            ),
        )
    )

    assert response.items == []
    assert response.total == 0
    assert response.total_considered == 0
    assert response.scope.type == "agent"
    assert response.scope.key == "iris"
    assert response.trace.status == "ok"
    assert response.trace.searched_scope.type == "agent"
    assert response.trace.searched_scope.key == "iris"
    assert memory_telemetry_snapshot()["semantic_recall"] == [(("empty", "agent"), 1)]


def test_semantic_recall_drops_zero_score_rows_for_tokenized_queries() -> None:
    item, entry = _semantic_memory_row(
        title="Unrelated Palace deploy note",
        body="This row does not match the requested search terms.",
        scope_key="iris",
        valid_from=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    session = FakeSession(execute_results=[1, [(item, entry, 0)]])

    response = asyncio.run(
        semantic_recall_memory(
            session,
            tenant_id="tenant-a",
            body=SemanticRecallRequest(
                scope_type="agent",
                scope_key="iris",
                query="staging port",
            ),
        )
    )

    assert response.items == []
    assert response.total == 0
    assert response.total_considered == 1
    compiled = "\n".join(session.executed)
    assert "> 0" in compiled


def test_semantic_recall_orders_current_candidates_before_score_limit() -> None:
    session = FakeSession(execute_results=[0, []])

    asyncio.run(
        semantic_recall_memory(
            session,
            tenant_id="tenant-a",
            body=SemanticRecallRequest(
                scope_type="agent",
                scope_key="iris",
                query="staging port",
                candidate_limit=1,
            ),
        )
    )

    compiled = "\n".join(session.executed)
    assert "> 0" in compiled
    assert "ORDER BY CASE" in compiled
    assert compiled.index("ORDER BY CASE") < compiled.index("semantic_score DESC")
    assert "LIMIT 1" in compiled


def test_semantic_recall_prefers_source_backed_entry_over_derived_summary() -> None:
    source_item, source_entry = _semantic_memory_row(
        title="Iris SAR-1037 source note",
        body="Iris saw SAR-1037 semantic recall source evidence.",
        scope_key="iris",
        valid_from=datetime(2026, 7, 1, tzinfo=timezone.utc),
        created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    derived_item, derived_entry = _semantic_memory_row(
        title="Iris SAR-1037 generated summary",
        body="Iris saw SAR-1037 semantic recall source evidence.",
        scope_key="iris",
        valid_from=datetime(2026, 7, 1, tzinfo=timezone.utc),
        created_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
        metadata={"memory_dream": {"artifact_type": "summary"}},
    )
    session = FakeSession(
        execute_results=[
            2,
            [(derived_item, derived_entry, 4), (source_item, source_entry, 4)],
        ]
    )

    response = asyncio.run(
        semantic_recall_memory(
            session,
            tenant_id="tenant-a",
            body=SemanticRecallRequest(
                scope_type="agent",
                scope_key="iris",
                query="SAR-1037 semantic recall source evidence",
                top_k=2,
            ),
        )
    )

    assert [result.entry_id for result in response.items] == [source_entry.id, derived_entry.id]
    compiled = "\n".join(session.executed)
    assert "? 'memory_dream'" in compiled
    assert "length(btrim(coalesce(items.raw_content, ''))) > 0" in compiled


def test_semantic_recall_sql_source_rank_treats_blank_body_as_not_source_backed() -> None:
    source_item, source_entry = _semantic_memory_row(
        title="Iris SAR-1037 source note",
        body="Iris saw SAR-1037 semantic recall source evidence.",
        scope_key="iris",
        valid_from=datetime(2026, 7, 1, tzinfo=timezone.utc),
        created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    blank_item, blank_entry = _semantic_memory_row(
        title="Iris SAR-1037 blank note",
        body="Iris saw SAR-1037 semantic recall source evidence.",
        scope_key="iris",
        valid_from=datetime(2026, 7, 1, tzinfo=timezone.utc),
        created_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
    )
    blank_item.raw_content = "   "
    session = FakeSession(
        execute_results=[
            2,
            [(blank_item, blank_entry, 4), (source_item, source_entry, 4)],
        ]
    )

    response = asyncio.run(
        semantic_recall_memory(
            session,
            tenant_id="tenant-a",
            body=SemanticRecallRequest(
                scope_type="agent",
                scope_key="iris",
                query="SAR-1037 semantic recall source evidence",
                top_k=2,
            ),
        )
    )

    assert [result.entry_id for result in response.items] == [source_entry.id, blank_entry.id]
    compiled = "\n".join(session.executed)
    assert "length(btrim(coalesce(items.raw_content, ''))) > 0" in compiled


def test_semantic_recall_short_query_returns_success_without_tokens() -> None:
    item, entry = _semantic_memory_row(
        title="Iris short recall",
        body="Short recall should not crash.",
        scope_key="iris",
        valid_from=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    session = FakeSession(execute_results=[1, [(item, entry, 0)]])

    response = asyncio.run(
        semantic_recall_memory(
            session,
            tenant_id="tenant-a",
            body=SemanticRecallRequest(
                scope_type="agent",
                scope_key="iris",
                query="a",
            ),
        )
    )

    assert [result.entry_id for result in response.items] == [entry.id]
    assert response.items[0].score == 0
    assert response.trace.status == "ok"


def test_semantic_recall_sql_score_includes_tags_for_thresholds() -> None:
    item, entry = _semantic_memory_row(
        title="Iris recall",
        body="Body omits the provenance token.",
        scope_key="iris",
        valid_from=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    item.tags = ["agent-memory", "scope-agent", "codex-specific"]
    session = FakeSession(execute_results=[1, [(item, entry, 1)]])

    response = asyncio.run(
        semantic_recall_memory(
            session,
            tenant_id="tenant-a",
            body=SemanticRecallRequest(
                scope_type="agent",
                scope_key="iris",
                query="codex-specific",
                score_threshold=1,
            ),
        )
    )

    assert [result.entry_id for result in response.items] == [entry.id]
    assert response.items[0].score == 1
    compiled = "\n".join(session.executed)
    assert "array_to_string(items.tags" in compiled
    assert ">= 1" in compiled


def test_semantic_recall_threshold_ignores_question_stopwords() -> None:
    item, entry = _semantic_memory_row(
        title="SAR-1015 auto-advanced",
        body="Iris auto-advanced SAR-1015 from Backlog to In Progress.",
        scope_key="iris",
        valid_from=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    session = FakeSession(execute_results=[1, [(item, entry, 2)]])

    response = asyncio.run(
        semantic_recall_memory(
            session,
            tenant_id="tenant-a",
            body=SemanticRecallRequest(
                scope_type="agent",
                scope_key="iris",
                query="which SAR tickets did I auto-advance last week?",
                score_threshold=0.7,
            ),
        )
    )

    assert [result.entry_id for result in response.items] == [entry.id]
    assert response.items[0].score == 1
    compiled = "\n".join(session.executed)
    assert ">= 1.4" in compiled


def test_semantic_recall_sql_score_escapes_like_wildcards() -> None:
    item, entry = _semantic_memory_row(
        title="Iris recall",
        body="Body omits the underscore token.",
        scope_key="iris",
        valid_from=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    item.tags = ["codex_memory"]
    session = FakeSession(execute_results=[1, [(item, entry, 1)]])

    response = asyncio.run(
        semantic_recall_memory(
            session,
            tenant_id="tenant-a",
            body=SemanticRecallRequest(
                scope_type="agent",
                scope_key="iris",
                query="codex_memory",
                score_threshold=1,
            ),
        )
    )

    compiled = "\n".join(session.executed)
    assert [result.entry_id for result in response.items] == [entry.id]
    assert "ESCAPE '/'" in compiled
    assert "codex/_memory" in compiled


def test_semantic_recall_defaults_match_v1_contract() -> None:
    request = SemanticRecallRequest(
        scope_type="agent",
        scope_key="iris",
        query="default recall contract",
    )

    assert request.top_k == 8
    assert request.recall_max_tokens == 1500
    response = asyncio.run(
        semantic_recall_memory(
            FakeSession(execute_results=[0, []]),
            tenant_id="tenant-a",
            body=request,
        )
    )
    assert response.trace.candidate_limit == 50


def test_semantic_recall_token_budget_uses_estimated_character_budget() -> None:
    item, entry = _semantic_memory_row(
        title="Iris long recall",
        body="x" * 900,
        scope_key="iris",
        valid_from=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    session = FakeSession(execute_results=[1, [(item, entry)]])

    response = asyncio.run(
        semantic_recall_memory(
            session,
            tenant_id="tenant-a",
            body=SemanticRecallRequest(
                scope_type="agent",
                scope_key="iris",
                query="Iris long recall",
                recall_max_tokens=500,
            ),
        )
    )

    assert [result.entry_id for result in response.items] == [entry.id]
    assert response.trace.budget_truncated is False


def test_semantic_recall_truncates_first_item_to_budget() -> None:
    item, entry = _semantic_memory_row(
        title="Iris oversized recall",
        body="x" * 900,
        scope_key="iris",
        valid_from=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    session = FakeSession(execute_results=[1, [(item, entry)]])

    response = asyncio.run(
        semantic_recall_memory(
            session,
            tenant_id="tenant-a",
            body=SemanticRecallRequest(
                scope_type="agent",
                scope_key="iris",
                query="Iris oversized recall",
                context_budget_chars=260,
            ),
        )
    )

    assert [result.entry_id for result in response.items] == [entry.id]
    assert response.trace.budget_truncated is True
    assert len(response.items[0].title) + len(response.items[0].summary or "") + len(response.items[0].body or "") <= 260


def test_list_memory_scopes_summarizes_without_raw_content() -> None:
    session = FakeSession(
        execute_results=[
            [
                {
                    "scope_type": "workspace",
                    "scope_key": "exampleos",
                    "entry_count": 3,
                    "latest_created_at": datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
                    "latest_updated_at": datetime(2026, 5, 6, 12, 5, tzinfo=timezone.utc),
                    "tags": ["codex-memory", "migration-staging"],
                    "sources": ["codex"],
                    "retain_mission": "Keep operator deployment facts.",
                    "quiet_recall": True,
                    "profile_created_at": datetime(2026, 5, 6, 11, 0, tzinfo=timezone.utc),
                    "profile_updated_at": datetime(2026, 5, 6, 11, 5, tzinfo=timezone.utc),
                    "created_by": "codex",
                    "updated_by": "codex",
                },
                {
                    "scope_type": "tenant_shared",
                    "scope_key": None,
                    "entry_count": 2,
                    "latest_created_at": datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc),
                    "latest_updated_at": datetime(2026, 5, 5, 12, 5, tzinfo=timezone.utc),
                    "tags": [],
                    "sources": [],
                },
            ]
        ]
    )

    response = asyncio.run(list_memory_scopes(session, tenant_id="tenant-a", limit=50, sample_limit=8))

    assert response.total == 2
    assert response.scopes[0].scope.type == "workspace"
    assert response.scopes[0].scope.key == "exampleos"
    assert response.scopes[0].entry_count == 3
    assert response.scopes[0].tags == ["codex-memory", "migration-staging"]
    assert response.scopes[0].sources == ["codex"]
    assert response.scopes[0].profile.retain_mission == "Keep operator deployment facts."
    assert response.scopes[0].profile.reflect_mission == ""
    assert response.scopes[0].profile.reflection_enabled is False
    assert response.scopes[0].profile.quiet_recall is True
    assert response.scopes[1].scope.type == "tenant_shared"
    assert response.scopes[1].scope.key is None
    assert response.scopes[1].profile.retain_mission == ""
    assert response.scopes[1].profile.reflect_mission == ""
    assert response.scopes[1].profile.reflection_enabled is False
    assert response.scopes[1].profile.quiet_recall is False
    compiled = "\n".join(session.executed)
    assert "i.raw_content" not in compiled
    assert "memory_scope_profiles" in compiled
    assert "reflect_mission" in compiled
    assert "reflection_enabled" in compiled
    assert "i.tenant_id =" in compiled


def test_get_memory_scope_profile_defaults_when_profile_missing() -> None:
    session = FakeSession(execute_results=[[]])

    profile = asyncio.run(
        get_memory_scope_profile(
            session,
            tenant_id="tenant-a",
            scope=MemoryScope(type="agent", key="codex"),
        )
    )

    assert profile.scope.type == "agent"
    assert profile.scope.key == "codex"
    assert profile.retain_mission == ""
    assert profile.reflect_mission == ""
    assert profile.reflection_enabled is False
    assert profile.quiet_recall is False
    assert "memory_scope_profiles" in "\n".join(session.executed)


def test_upsert_memory_scope_profile_persists_shared_runtime_fields() -> None:
    updated_at = datetime(2026, 7, 9, 2, 0, tzinfo=timezone.utc)
    session = FakeSession(
        execute_results=[
            [
                {
                    "scope_type": "workspace",
                    "scope_key": "hermes",
                    "retain_mission": "Retain durable Hermes routing decisions.",
                    "reflect_mission": "Reflect only promoted Hermes observations.",
                    "reflection_enabled": True,
                    "quiet_recall": True,
                    "profile_created_at": updated_at,
                    "profile_updated_at": updated_at,
                    "created_by": "codex",
                    "updated_by": "codex",
                }
            ]
        ]
    )

    profile = asyncio.run(
        upsert_memory_scope_profile(
            session,
            tenant_id="tenant-a",
            body=MemoryScopeProfileUpsertRequest(
                scope=MemoryScope(type="workspace", key="hermes"),
                retain_mission=" Retain durable Hermes routing decisions. ",
                reflect_mission=" Reflect only promoted Hermes observations. ",
                reflection_enabled=True,
                quiet_recall=True,
                updated_by="codex",
            ),
        )
    )

    assert profile.scope.type == "workspace"
    assert profile.scope.key == "hermes"
    assert profile.retain_mission == "Retain durable Hermes routing decisions."
    assert profile.reflect_mission == "Reflect only promoted Hermes observations."
    assert profile.reflection_enabled is True
    assert profile.quiet_recall is True
    assert session.commits == 1
    compiled = "\n".join(session.executed)
    assert "ON CONFLICT" in compiled
    assert "retain_mission" in compiled
    assert "reflect_mission" in compiled
    assert "reflection_enabled" in compiled


def test_retrieve_agent_memory_searches_policy_scopes_and_excludes_private_broad_corpus(monkeypatch) -> None:
    import app.services.memory as memory_service

    scope_calls: list[tuple[str, str | None]] = []
    broad_calls: list[dict] = []
    scoped_vectors: list[list[float] | None] = []

    async def fake_retrieve_memory(db, *, embedder, tenant_id: str, body, query_vector=None):
        scope_calls.append((body.scope.type, body.scope.key))
        scoped_vectors.append(query_vector)
        return memory_service.MemoryRetrieveResponse(
            scope=body.scope,
            routed_room_id=None,
            redirected_from_room_id=None,
            trace=PalaceRetrieveTrace(
                requested_scope_type=body.scope.type,
                requested_scope_key=body.scope.key,
                fallback_used=False,
            ),
            results=[],
            total=0,
        )

    class FakeSearchService:
        def __init__(self, db, embedder, tenant_id: str) -> None:
            self.tenant_id = tenant_id

        async def vector_search(self, **kwargs):
            broad_calls.append(kwargs)
            return []

    monkeypatch.setattr(memory_service, "retrieve_memory", fake_retrieve_memory)
    monkeypatch.setattr(memory_service, "SearchService", FakeSearchService)

    embedder = FakeEmbedder()
    response = asyncio.run(
        retrieve_agent_memory(
            FakeSession(),
            embedder=embedder,
            tenant_id="tenant-a",
            body=AgentMemoryRetrieveRequest(
                query="exampleos memory",
                agent_scope_key="orchestrator",
                workspace_scope_keys=["exampleos", "exampleos"],
                include_tenant_shared=True,
                include_broad_corpus=True,
            ),
        )
    )

    assert scope_calls == [
        ("agent", "orchestrator"),
        ("workspace", "exampleos"),
        ("tenant_shared", None),
    ]
    assert embedder.calls == ["exampleos memory"]
    assert scoped_vectors == [[0.1, 0.2, 0.3]] * 3
    assert broad_calls[0]["query_vector"] == [0.1, 0.2, 0.3]
    assert broad_calls[0]["exclude_private_memory_scopes"] is True
    assert response.trace.searched_scopes[1].type == "workspace"
    assert response.trace.excluded_scope_types == ["agent", "workspace", "session"]
    assert response.trace.query_embedding_reused is True
    assert response.trace.selected_scope_query_count == 3
    assert response.trace.selected_scope_duration_ms is not None
    assert response.trace.broad_corpus_duration_ms is not None
    assert response.trace.merge_duration_ms is not None
    assert response.trace.total_duration_ms is not None


def test_retrieve_memory_forwards_explicit_derived_artifact_policy(monkeypatch) -> None:
    import app.services.memory as memory_service

    captured_body = None

    async def fake_retrieve_palace(db, *, tenant_id: str, embedder, body, query_vector=None):
        nonlocal captured_body
        captured_body = body
        return PalaceRetrieveResponse(
            routed_room_id=None,
            redirected_from_room_id=None,
            trace=PalaceRetrieveTrace(
                requested_scope_type=body.scope_type,
                requested_scope_key=body.scope_key,
                fallback_used=False,
            ),
            results=[],
            total=0,
        )

    monkeypatch.setattr(memory_service, "retrieve_palace", fake_retrieve_palace)

    response = asyncio.run(
        retrieve_memory(
            FakeSession(),
            embedder=object(),
            tenant_id="tenant-a",
            body=MemoryRetrieveRequest(
                query="risk management framework",
                scope=MemoryScope(type="workspace", key="nist"),
                include_derived_artifacts=True,
            ),
        )
    )

    assert response.scope.type == "workspace"
    assert captured_body.include_derived_artifacts is True


def test_retrieve_agent_memory_forwards_explicit_derived_artifact_policy(monkeypatch) -> None:
    import app.services.memory as memory_service

    scoped_flags: list[bool] = []
    broad_flags: list[bool] = []

    async def fake_retrieve_memory(db, *, embedder, tenant_id: str, body, query_vector=None):
        scoped_flags.append(body.include_derived_artifacts)
        return memory_service.MemoryRetrieveResponse(
            scope=body.scope,
            routed_room_id=None,
            redirected_from_room_id=None,
            trace=PalaceRetrieveTrace(
                requested_scope_type=body.scope.type,
                requested_scope_key=body.scope.key,
                fallback_used=False,
            ),
            results=[],
            total=0,
        )

    class FakeSearchService:
        def __init__(self, db, embedder, tenant_id: str) -> None:
            self.tenant_id = tenant_id

        async def vector_search(self, **kwargs):
            broad_flags.append(kwargs["include_derived_artifacts"])
            return []

    monkeypatch.setattr(memory_service, "retrieve_memory", fake_retrieve_memory)
    monkeypatch.setattr(memory_service, "SearchService", FakeSearchService)

    asyncio.run(
        retrieve_agent_memory(
            FakeSession(),
            embedder=object(),
            tenant_id="tenant-a",
            body=AgentMemoryRetrieveRequest(
                query="risk management framework",
                agent_scope_key="codex",
                include_tenant_shared=True,
                include_broad_corpus=True,
                include_derived_artifacts=True,
            ),
        )
    )

    assert scoped_flags == [True, True]
    assert broad_flags == [True]


def test_retrieve_memory_trajectory_orders_facts_and_marks_stale(monkeypatch) -> None:
    import app.services.memory as memory_service
    import app.services.memory_trajectory as trajectory_service

    source_item_id = uuid.uuid4()
    captured_body = None

    async def fake_retrieve_agent_memory(db, *, embedder, tenant_id: str, body, delegated_policy=None):
        nonlocal captured_body
        captured_body = body
        return memory_service.AgentMemoryRetrieveResponse(
            scopes=[MemoryScope(type="agent", key="codex")],
            trace=memory_service.AgentMemoryRetrieveTrace(
                searched_scopes=[MemoryScope(type="agent", key="codex")],
                result_counts_by_scope={"agent/codex": 2},
            ),
            results=[
                _search_result(
                    "Conversation fact: Andrew said",
                    score=0.91,
                    created_at=datetime(2026, 5, 2, tzinfo=timezone.utc),
                    chunk_text="Subject: Andrew\nPredicate: said\nObject: The PR is ready for deploy.\n\nSource span: latest",
                    tags=["conversation-fact", "derived-memory"],
                    source_item_id=source_item_id,
                    source_span={
                        "source_item_id": str(source_item_id),
                        "line_start": 5,
                        "line_end": 5,
                        "turn_index": 1,
                        "timestamp": "2026-05-02T12:00:00Z",
                    },
                    retrieved_scope_label="agent/codex",
                ),
                _search_result(
                    "Conversation fact: Andrew said",
                    score=0.95,
                    created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
                    chunk_text="Subject: Andrew\nPredicate: said\nObject: The PR is still blocked.\n\nSource span: earlier",
                    tags=["conversation-fact", "derived-memory"],
                    source_item_id=source_item_id,
                    source_span={
                        "source_item_id": str(source_item_id),
                        "line_start": 1,
                        "line_end": 1,
                        "turn_index": 0,
                        "timestamp": "2026-05-01T12:00:00Z",
                    },
                    retrieved_scope_label="agent/codex",
                ),
            ],
            total=2,
        )

    monkeypatch.setattr(trajectory_service, "retrieve_agent_memory", fake_retrieve_agent_memory)

    response = asyncio.run(
        retrieve_memory_trajectory(
            FakeSession(),
            embedder=object(),
            tenant_id="tenant-a",
            body=MemoryTrajectoryRequest(
                query="how did the PR status change?",
                trajectory_subject="pr status",
                agent_scope_key="codex",
                include_broad_corpus=False,
                tags=["release"],
            ),
        )
    )

    assert captured_body.include_derived_artifacts is True
    assert captured_body.tags == ["release", "conversation-fact"]
    assert captured_body.tags_mode == "all"
    assert captured_body.include_broad_corpus is False
    assert [entry.object_text for entry in response.entries] == [
        "The PR is still blocked.",
        "The PR is ready for deploy.",
    ]
    assert [entry.status for entry in response.entries] == ["stale", "current"]
    assert response.entries[0].source_span["line_start"] == 1
    assert response.current_entries[0].object_text == "The PR is ready for deploy."


def test_retrieve_agent_memory_uses_candidate_display_and_context_budgets(monkeypatch) -> None:
    import app.services.memory as memory_service

    duplicate_id = uuid.uuid4()
    scope_limits: list[int] = []
    broad_limits: list[int] = []

    async def fake_retrieve_memory(db, *, embedder, tenant_id: str, body, query_vector=None):
        scope_limits.append(body.limit)
        score = 0.71 if body.scope.type == "agent" else 0.92
        return memory_service.MemoryRetrieveResponse(
            scope=body.scope,
            routed_room_id=None,
            redirected_from_room_id=None,
            trace=PalaceRetrieveTrace(
                requested_scope_type=body.scope.type,
                requested_scope_key=body.scope.key,
                fallback_used=False,
            ),
            results=[
                _search_result(
                    f"{body.scope.type} duplicate",
                    score=score,
                    item_id=duplicate_id,
                    chunk_text="duplicate route",
                ),
                _search_result(f"{body.scope.type} extra", score=0.4),
            ],
            total=2,
        )

    class FakeSearchService:
        def __init__(self, db, embedder, tenant_id: str) -> None:
            self.tenant_id = tenant_id

        async def vector_search(self, **kwargs):
            broad_limits.append(kwargs["limit"])
            return [
                _search_result(
                    "broad winner",
                    score=0.99,
                    chunk_text="broad same-tenant memory that should survive the candidate pass",
                ),
                _search_result("broad tail", score=0.2),
            ]

    monkeypatch.setattr(memory_service, "retrieve_memory", fake_retrieve_memory)
    monkeypatch.setattr(memory_service, "SearchService", FakeSearchService)

    embedder = FakeEmbedder()
    response = asyncio.run(
        retrieve_agent_memory(
            FakeSession(),
            embedder=embedder,
            tenant_id="tenant-a",
            body=AgentMemoryRetrieveRequest(
                query="exampleos memory",
                agent_scope_key="codex",
                workspace_scope_keys=["exampleos"],
                candidate_limit=17,
                broad_candidate_limit=23,
                display_limit=2,
                context_budget_chars=1200,
                include_tenant_shared=False,
                include_broad_corpus=True,
            ),
        )
    )

    assert embedder.calls == ["exampleos memory"]
    assert scope_limits == [17, 17]
    assert broad_limits == [23]
    assert [result.title for result in response.results] == ["broad winner", "workspace duplicate"]
    assert response.trace.selected_scope_candidate_limit == 17
    assert response.trace.broad_candidate_limit == 23
    assert response.trace.display_limit == 2
    assert response.trace.selected_scope_result_count == 4
    assert response.trace.broad_result_count == 2
    assert response.trace.deduped_result_count == 5
    assert response.trace.context_budget_chars == 1200
    assert response.trace.budget_truncated is True


def test_retrieve_agent_memory_context_budget_truncates_first_result(monkeypatch) -> None:
    import app.services.memory as memory_service

    async def fake_retrieve_memory(db, *, embedder, tenant_id: str, body, query_vector=None):
        return memory_service.MemoryRetrieveResponse(
            scope=body.scope,
            routed_room_id=None,
            redirected_from_room_id=None,
            trace=PalaceRetrieveTrace(
                requested_scope_type=body.scope.type,
                requested_scope_key=body.scope.key,
                fallback_used=False,
            ),
            results=[
                _search_result(
                    "workspace-exampleos",
                    score=0.9,
                    chunk_text="x" * 500,
                )
            ],
            total=1,
        )

    monkeypatch.setattr(memory_service, "retrieve_memory", fake_retrieve_memory)

    response = asyncio.run(
        retrieve_agent_memory(
            FakeSession(),
            embedder=object(),
            tenant_id="tenant-a",
            body=AgentMemoryRetrieveRequest(
                query="exampleos memory",
                workspace_scope_keys=["exampleos"],
                include_tenant_shared=False,
                include_broad_corpus=False,
                context_budget_chars=220,
            ),
        )
    )

    assert len(response.results) == 1
    assert response.results[0].chunk_text.endswith("...")
    assert response.trace.context_budget_chars == 220
    assert response.trace.context_budget_truncated is True


def test_retrieve_agent_memory_demotes_stale_agent_conversation_when_workspace_selected(
    monkeypatch,
) -> None:
    import app.services.memory as memory_service

    async def fake_retrieve_memory(db, *, embedder, tenant_id: str, body, query_vector=None):
        if body.scope.type == "agent":
            results = [
                _search_result(
                    "default: [Andrew] asked about ExampleOS",
                    score=0.62,
                    chunk_text="# Conversation Turn\n\nUser: What is ExampleOS?\nAssistant: I cannot find it.",
                    tags=["codex-memory", "scope-agent", "agent-orchestrator"],
                )
            ]
        elif body.scope.type == "workspace":
            results = [
                _search_result(
                    "ExampleOS current-state documentation",
                    score=0.47,
                    chunk_text="ExampleOS is the current runtime and dispatch surface.",
                    tags=["codex-memory", "scope-workspace", "workspace-exampleos"],
                )
            ]
        else:
            results = []
        return memory_service.MemoryRetrieveResponse(
            scope=body.scope,
            routed_room_id=None,
            redirected_from_room_id=None,
            trace=PalaceRetrieveTrace(
                requested_scope_type=body.scope.type,
                requested_scope_key=body.scope.key,
                fallback_used=False,
            ),
            results=results,
            total=len(results),
        )

    monkeypatch.setattr(memory_service, "retrieve_memory", fake_retrieve_memory)

    response = asyncio.run(
        retrieve_agent_memory(
            FakeSession(),
            embedder=object(),
            tenant_id="tenant-a",
            body=AgentMemoryRetrieveRequest(
                query="What is ExampleOS?",
                agent_scope_key="orchestrator",
                workspace_scope_keys=["exampleos"],
                include_tenant_shared=False,
                include_broad_corpus=False,
                display_limit=2,
            ),
        )
    )

    assert [result.title for result in response.results] == [
        "ExampleOS current-state documentation",
        "default: [Andrew] asked about ExampleOS",
    ]


def test_retrieve_agent_memory_prefers_exact_workspace_over_tenant_shared(monkeypatch) -> None:
    import app.services.memory as memory_service

    async def fake_retrieve_memory(db, *, embedder, tenant_id: str, body, query_vector=None):
        if body.scope.type == "workspace":
            results = [
                _search_result(
                    "Receipt Shelf full project brief",
                    score=0.72,
                    item_id=uuid.UUID("3eaf5fe0-1855-4e19-8898-d110ebecf3ae"),
                    chunk_text="Receipt Shelf canonical project memory.",
                    tags=["codex-memory", "scope-workspace", "workspace-receipt-shelf"],
                )
            ]
        elif body.scope.type == "tenant_shared":
            results = [
                _search_result(
                    "Shared Codex project briefs",
                    score=0.94,
                    chunk_text="Shared brief mentions Receipt Shelf among many projects.",
                    tags=["codex-memory", "scope-tenant_shared", "receipt-shelf"],
                )
            ]
        else:
            results = []
        return memory_service.MemoryRetrieveResponse(
            scope=body.scope,
            routed_room_id=None,
            redirected_from_room_id=None,
            trace=PalaceRetrieveTrace(
                requested_scope_type=body.scope.type,
                requested_scope_key=body.scope.key,
                fallback_used=False,
            ),
            results=results,
            total=len(results),
        )

    monkeypatch.setattr(memory_service, "retrieve_memory", fake_retrieve_memory)

    response = asyncio.run(
        retrieve_agent_memory(
            FakeSession(),
            embedder=object(),
            tenant_id="tenant-a",
            body=AgentMemoryRetrieveRequest(
                query="receipt-shelf",
                workspace_scope_keys=["receipt-shelf"],
                include_tenant_shared=True,
                include_broad_corpus=False,
                display_limit=2,
            ),
        )
    )

    assert [result.title for result in response.results] == [
        "Receipt Shelf full project brief",
        "Shared Codex project briefs",
    ]


def test_retrieve_agent_memory_workspace_strict_skips_agent_session_and_shared_when_workspace_hits(
    monkeypatch,
) -> None:
    import app.services.memory as memory_service

    scope_calls: list[tuple[str, str | None]] = []

    async def fake_retrieve_memory(db, *, embedder, tenant_id: str, body, query_vector=None):
        scope_calls.append((body.scope.type, body.scope.key))
        assert body.scope.type == "workspace"
        return memory_service.MemoryRetrieveResponse(
            scope=body.scope,
            routed_room_id=None,
            redirected_from_room_id=None,
            trace=PalaceRetrieveTrace(
                requested_scope_type=body.scope.type,
                requested_scope_key=body.scope.key,
                fallback_used=False,
            ),
            results=[
                _search_result(
                    "ExampleOS strict project memory",
                    score=0.91,
                    tags=["codex-memory", "scope-workspace", "workspace-exampleos"],
                )
            ],
            total=1,
        )

    class FakeSearchService:
        def __init__(self, db, embedder, tenant_id: str) -> None:
            pass

        async def vector_search(self, **kwargs):
            raise AssertionError("strict workspace retrieval must not search broad corpus by default")

    monkeypatch.setattr(memory_service, "retrieve_memory", fake_retrieve_memory)
    monkeypatch.setattr(memory_service, "SearchService", FakeSearchService)

    response = asyncio.run(
        retrieve_agent_memory(
            FakeSession(),
            embedder=FakeEmbedder(),
            tenant_id="tenant-a",
            body=AgentMemoryRetrieveRequest(
                query="exampleos memory",
                agent_scope_key="orchestrator",
                workspace_scope_keys=["exampleos"],
                session_scope_key="run-123",
                workspace_strict=True,
                tenant_shared_policy="fallback_only",
                include_tenant_shared=True,
                include_broad_corpus=True,
            ),
        )
    )

    assert scope_calls == [("workspace", "exampleos")]
    assert response.trace.workspace_strict is True
    assert response.trace.workspace_scope_exhausted is False
    assert response.trace.tenant_shared_policy == "fallback_only"
    assert response.trace.tenant_shared_fallback_used is False
    assert response.trace.broad_corpus_searched is False
    assert response.trace.broad_corpus_skipped_reason == "workspace_strict_requires_explicit_broad_corpus_policy"


def test_retrieve_agent_memory_workspace_strict_uses_tenant_shared_fallback_only_when_empty(
    monkeypatch,
) -> None:
    import app.services.memory as memory_service

    scope_calls: list[tuple[str, str | None]] = []

    async def fake_retrieve_memory(db, *, embedder, tenant_id: str, body, query_vector=None):
        scope_calls.append((body.scope.type, body.scope.key))
        results = []
        if body.scope.type == "tenant_shared":
            results = [
                _search_result(
                    "Shared fallback memory",
                    score=0.62,
                    tags=["codex-memory", "scope-tenant_shared"],
                )
            ]
        return memory_service.MemoryRetrieveResponse(
            scope=body.scope,
            routed_room_id=None,
            redirected_from_room_id=None,
            trace=PalaceRetrieveTrace(
                requested_scope_type=body.scope.type,
                requested_scope_key=body.scope.key,
                fallback_used=False,
            ),
            results=results,
            total=len(results),
        )

    monkeypatch.setattr(memory_service, "retrieve_memory", fake_retrieve_memory)

    response = asyncio.run(
        retrieve_agent_memory(
            FakeSession(),
            embedder=FakeEmbedder(),
            tenant_id="tenant-a",
            body=AgentMemoryRetrieveRequest(
                query="exampleos memory",
                workspace_scope_keys=["exampleos"],
                workspace_strict=True,
                tenant_shared_policy="fallback_only",
                include_tenant_shared=True,
                include_broad_corpus=False,
            ),
        )
    )

    assert scope_calls == [("workspace", "exampleos"), ("tenant_shared", None)]
    assert [result.title for result in response.results] == ["Shared fallback memory"]
    assert response.trace.workspace_scope_exhausted is True
    assert response.trace.tenant_shared_fallback_used is True
    assert response.trace.searched_scopes == [
        MemoryScope(type="workspace", key="exampleos"),
        MemoryScope(type="tenant_shared"),
    ]


def test_retrieve_agent_memory_workspace_strict_broad_corpus_requires_explicit_policy(
    monkeypatch,
) -> None:
    import app.services.memory as memory_service

    scope_calls: list[tuple[str, str | None]] = []
    broad_calls: list[dict] = []

    async def fake_retrieve_memory(db, *, embedder, tenant_id: str, body, query_vector=None):
        scope_calls.append((body.scope.type, body.scope.key))
        return memory_service.MemoryRetrieveResponse(
            scope=body.scope,
            routed_room_id=None,
            redirected_from_room_id=None,
            trace=PalaceRetrieveTrace(
                requested_scope_type=body.scope.type,
                requested_scope_key=body.scope.key,
                fallback_used=False,
            ),
            results=[],
            total=0,
        )

    class FakeSearchService:
        def __init__(self, db, embedder, tenant_id: str) -> None:
            pass

        async def vector_search(self, **kwargs):
            broad_calls.append(kwargs)
            return [_search_result("Broad opt-in result", score=0.7)]

    monkeypatch.setattr(memory_service, "retrieve_memory", fake_retrieve_memory)
    monkeypatch.setattr(memory_service, "SearchService", FakeSearchService)

    response = asyncio.run(
        retrieve_agent_memory(
            FakeSession(),
            embedder=FakeEmbedder(),
            tenant_id="tenant-a",
            body=AgentMemoryRetrieveRequest(
                query="exampleos memory",
                workspace_scope_keys=["exampleos"],
                workspace_strict=True,
                tenant_shared_policy="never",
                include_broad_corpus=True,
                broad_corpus_policy="enabled",
            ),
        )
    )

    assert scope_calls == [("workspace", "exampleos")]
    assert broad_calls
    assert broad_calls[0]["exclude_private_memory_scopes"] is True
    assert [result.title for result in response.results] == ["Broad opt-in result"]
    assert response.trace.broad_corpus_searched is True
    assert response.trace.broad_corpus_policy == "enabled"


def test_retrieve_agent_memory_skips_broad_corpus_when_workspace_results_satisfy_display(
    monkeypatch,
) -> None:
    import app.services.memory as memory_service

    async def fake_retrieve_memory(db, *, embedder, tenant_id: str, body, query_vector=None):
        if body.scope.type != "workspace":
            results = []
        else:
            results = [
                _search_result(
                    "FeedValue current implementation notes",
                    score=0.91,
                    tags=["codex-memory", "scope-workspace", "workspace-feedvalue"],
                ),
                _search_result(
                    "FeedValue deployment handoff",
                    score=0.82,
                    tags=["codex-memory", "scope-workspace", "workspace-feedvalue"],
                ),
            ]
        return memory_service.MemoryRetrieveResponse(
            scope=body.scope,
            routed_room_id=None,
            redirected_from_room_id=None,
            trace=PalaceRetrieveTrace(
                requested_scope_type=body.scope.type,
                requested_scope_key=body.scope.key,
                fallback_used=False,
            ),
            results=results,
            total=len(results),
        )

    class FakeSearchService:
        def __init__(self, db, embedder, tenant_id: str) -> None:
            pass

        async def vector_search(self, **kwargs):
            raise AssertionError("broad corpus should be skipped")

    monkeypatch.setattr(memory_service, "retrieve_memory", fake_retrieve_memory)
    monkeypatch.setattr(memory_service, "SearchService", FakeSearchService)

    response = asyncio.run(
        retrieve_agent_memory(
            FakeSession(),
            embedder=FakeEmbedder(),
            tenant_id="tenant-a",
            body=AgentMemoryRetrieveRequest(
                query="feedvalue current status",
                workspace_scope_keys=["feedvalue"],
                include_tenant_shared=False,
                include_broad_corpus=True,
                display_limit=2,
            ),
        )
    )

    assert [result.title for result in response.results] == [
        "FeedValue current implementation notes",
        "FeedValue deployment handoff",
    ]
    assert response.trace.broad_corpus_searched is False
    assert response.trace.broad_corpus_skipped_reason == "preferred_workspace_results_satisfied_display_limit"
    assert response.trace.broad_result_count == 0
