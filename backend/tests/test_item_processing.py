from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.models.embedding import Embedding
from app.models.item import Item
from app.models.job import Job, JobProgressEvent
from app.embedding_profile import resolve_embedding_profile
from app.services.embedder import EmbeddingRequestError
from app.services.item_processing import process_prebuilt_item


class FakeEmbedder:
    profile = resolve_embedding_profile()

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[float(index + 1)] * self.profile.dimensions for index, _text in enumerate(texts)]


class FakeLlm:
    async def summarize(self, _text: str) -> str:
        return "summary"

    async def generate_tags(self, _text: str, *, existing_tags: list[str]) -> tuple[list[str], list[str]]:
        return (existing_tags or ["memory"], ["notes"])


class ScalarResult:
    def __init__(self, value) -> None:
        self.value = value

    def scalars(self):
        return self

    def all(self):
        return []

    def __iter__(self):
        return iter([])


class FakeSession:
    def __init__(self, item: Item, job: Job) -> None:
        self.item = item
        self.job = job
        self.embeddings: list[Embedding] = []
        self.commit_count = 0
        self.rollback_count = 0
        self.executed_sql: list[str] = []
        self.added: list[object] = []

    async def scalar(self, _statement):
        return None

    async def execute(self, statement):
        sql = str(statement)
        self.executed_sql.append(sql)
        if "DELETE FROM embeddings" in sql:
            self.embeddings = [embedding for embedding in self.embeddings if embedding.item_id != self.item.id]
        return ScalarResult(None)

    def add(self, value) -> None:
        self.added.append(value)
        if isinstance(value, Embedding):
            self.embeddings.append(value)

    async def commit(self) -> None:
        self.commit_count += 1

    async def rollback(self) -> None:
        self.rollback_count += 1

    async def get(self, model, key):
        if model is Item and key == self.item.id:
            return self.item
        if model is Job and key == self.job.id:
            return self.job
        return None


@pytest.mark.asyncio
async def test_process_prebuilt_item_replaces_existing_embeddings_on_retry() -> None:
    item_id = uuid.uuid4()
    item = Item(
        id=item_id,
        tenant_id="tenant-a",
        title="Recovered note",
        source_type="note",
        status="processing",
        raw_content="Recovered memory note content.\n\nSecond line for chunking.",
        metadata_={},
        tags=[],
        categories=[],
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    job = Job(
        id=uuid.uuid4(),
        item_id=item_id,
        tenant_id="tenant-a",
        job_type="memory_artifact",
        status="queued",
        progress=0,
        created_at=datetime.now(timezone.utc),
    )
    session = FakeSession(item, job)

    first = await process_prebuilt_item(
        session,
        item=item,
        embedder=FakeEmbedder(),
        llm=FakeLlm(),
        tenant_id="tenant-a",
        job=job,
        enable_ai_enrichment=False,
    )
    first_embedding_count = len(session.embeddings)

    second = await process_prebuilt_item(
        session,
        item=item,
        embedder=FakeEmbedder(),
        llm=FakeLlm(),
        tenant_id="tenant-a",
        job=job,
        enable_ai_enrichment=False,
    )

    assert first.status == "completed"
    assert second.status == "completed"
    assert first_embedding_count > 0
    assert len(session.embeddings) == first_embedding_count
    assert all(embedding.item_id == item_id for embedding in session.embeddings)
    assert {embedding.profile_name for embedding in session.embeddings} == {
        "openai-text-embedding-3-small-1536"
    }
    assert {embedding.provider for embedding in session.embeddings} == {"openai"}
    assert {embedding.dimensions for embedding in session.embeddings} == {1536}
    assert any("DELETE FROM embeddings" in sql for sql in session.executed_sql)


@pytest.mark.asyncio
async def test_process_prebuilt_item_memory_categories_are_opt_in() -> None:
    item_id = uuid.uuid4()
    item = Item(
        id=item_id,
        tenant_id="tenant-a",
        title="Scoped memory",
        source_type="note",
        status="processing",
        raw_content="Memory content about project taxonomy.",
        metadata_={"memory_entry": {"scope": {"type": "agent", "key": "codex"}}},
        tags=["caller-tag"],
        categories=[],
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    job = Job(
        id=uuid.uuid4(),
        item_id=item_id,
        tenant_id="tenant-a",
        job_type="memory_artifact",
        status="queued",
        progress=0,
        created_at=datetime.now(timezone.utc),
    )
    session = FakeSession(item, job)

    await process_prebuilt_item(
        session,
        item=item,
        embedder=FakeEmbedder(),
        llm=FakeLlm(),
        tenant_id="tenant-a",
        job=job,
        enable_ai_enrichment=False,
    )

    assert item.tags == ["caller-tag"]
    assert item.categories == []

    item.status = "processing"
    item.content_hash = None
    await process_prebuilt_item(
        session,
        item=item,
        embedder=FakeEmbedder(),
        llm=FakeLlm(),
        tenant_id="tenant-a",
        job=job,
        enable_ai_enrichment=True,
    )

    assert item.tags == ["caller-tag"]
    assert item.categories == ["notes"]
    assert item.raw_content == "Memory content about project taxonomy."


@pytest.mark.asyncio
async def test_process_prebuilt_item_ai_enrichment_fills_only_missing_tags() -> None:
    item_id = uuid.uuid4()
    item = Item(
        id=item_id,
        tenant_id="tenant-a",
        title="Categorized memory",
        source_type="note",
        status="processing",
        raw_content="Memory content with caller category.",
        metadata_={"memory_entry": {"scope": {"type": "agent", "key": "codex"}}},
        tags=[],
        categories=["operator-memory"],
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    job = Job(
        id=uuid.uuid4(),
        item_id=item_id,
        tenant_id="tenant-a",
        job_type="memory_artifact",
        status="queued",
        progress=0,
        created_at=datetime.now(timezone.utc),
    )
    session = FakeSession(item, job)

    await process_prebuilt_item(
        session,
        item=item,
        embedder=FakeEmbedder(),
        llm=FakeLlm(),
        tenant_id="tenant-a",
        job=job,
        enable_ai_enrichment=True,
    )

    assert item.tags == ["memory"]
    assert item.categories == ["operator-memory"]


@pytest.mark.asyncio
async def test_process_prebuilt_item_persists_original_embedding_error_on_fresh_rows() -> None:
    item_id = uuid.uuid4()
    job_id = uuid.uuid4()
    original_item = Item(
        id=item_id,
        tenant_id="tenant-a",
        title="Poison memory",
        source_type="note",
        status="processing",
        raw_content="Content that reaches a deterministic provider validation error.",
        metadata_={},
        tags=[],
        categories=[],
    )
    original_job = Job(
        id=job_id,
        item_id=item_id,
        tenant_id="tenant-a",
        job_type="memory_artifact",
        status="queued",
        progress=0,
    )
    fresh_item = Item(
        id=item_id,
        tenant_id="tenant-a",
        title="Poison memory",
        source_type="note",
        status="processing",
        raw_content=original_item.raw_content,
    )
    fresh_job = Job(
        id=job_id,
        item_id=item_id,
        tenant_id="tenant-a",
        job_type="memory_artifact",
        status="processing",
        progress=15,
    )
    failure = EmbeddingRequestError(
        "maximum request size is 300000 tokens",
        retryable=False,
        failure_kind="validation",
        provider_status_code=400,
    )

    class FailingEmbedder(FakeEmbedder):
        async def embed_texts(self, texts: list[str]) -> list[list[float]]:
            raise failure

    class FreshFailureSession(FakeSession):
        async def get(self, model, key):
            if model is Item and key == item_id:
                return fresh_item
            if model is Job and key == job_id:
                return fresh_job
            return None

    session = FreshFailureSession(original_item, original_job)

    with pytest.raises(EmbeddingRequestError) as captured:
        await process_prebuilt_item(
            session,
            item=original_item,
            embedder=FailingEmbedder(),
            llm=FakeLlm(),
            tenant_id="tenant-a",
            job=original_job,
            enable_ai_enrichment=False,
        )

    assert captured.value is failure
    assert session.rollback_count == 1
    assert fresh_item.status == "failed"
    assert fresh_job.status == "failed"
    assert fresh_job.error_message == "maximum request size is 300000 tokens"
    assert fresh_job.completed_at is not None
    failure_events = [value for value in session.added if isinstance(value, JobProgressEvent)]
    assert failure_events[-1].status == "failed"
    assert failure_events[-1].progress == 15
