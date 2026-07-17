from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.models.item import Item
from app.pipelines.feed import FeedPipeline


class _Result:
    def all(self):
        return []

    def __iter__(self):
        return iter(())


class _Session:
    def __init__(self, scalar_values):
        self.scalar_values = iter(scalar_values)
        self.added = []
        self.executed = []
        self.commits = 0

    async def scalar(self, _statement):
        return next(self.scalar_values)

    async def execute(self, statement):
        self.executed.append(statement)
        return _Result()

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1


class _Embedder:
    async def embed_texts(self, values):
        return [[0.1] for _ in values]


@pytest.fixture
def feed():
    return SimpleNamespace(
        id=uuid.uuid4(),
        url="https://example.test/feed.xml",
        name="Example feed",
        auto_tags=[],
    )


@pytest.fixture
def patch_pipeline(monkeypatch):
    monkeypatch.setattr(
        "app.pipelines.feed.WebpagePipeline._scrape",
        staticmethod(lambda _url: ("<html>updated</html>", "updated article", {})),
    )

    async def _enrich(self, _text, _tags):
        return "summary", [], [], {}

    monkeypatch.setattr(FeedPipeline, "_run_enrichment", _enrich)


@pytest.mark.asyncio
async def test_feed_guid_refreshes_existing_item_and_replaces_derived_embeddings(feed, patch_pipeline) -> None:
    existing = Item(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        source_type="feed_article",
        source_url="https://example.test/old-url",
        title="Old title",
        status="ready",
        content_hash="old-hash",
        metadata_={"feed_id": str(feed.id), "feed_guid": "stable-guid"},
    )
    session = _Session([existing, None])
    pipeline = FeedPipeline(session, _Embedder(), SimpleNamespace())

    item_id = await pipeline.process_entry(
        feed,
        "https://example.test/new-url",
        "Updated title",
        entry_guid="stable-guid",
        tenant_id="tenant-a",
    )

    assert item_id == existing.id
    assert existing.source_url == "https://example.test/new-url"
    assert existing.title == "Updated title"
    assert existing.metadata_["feed_guid"] == "stable-guid"
    assert existing.status == "ready"
    assert session.commits == 1
    assert not any(value is existing for value in session.added)
    assert any("DELETE FROM embeddings" in str(statement) for statement in session.executed)


@pytest.mark.asyncio
async def test_feed_guid_skips_unchanged_content_without_embedding_work(feed, patch_pipeline) -> None:
    existing = Item(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        source_type="feed_article",
        source_url="https://example.test/article",
        title="Existing title",
        status="ready",
        content_hash="426af61b374c94cf55d482862f49afddd9f260cfb8728d1496f995365715bf88",
        metadata_={"feed_id": str(feed.id), "feed_guid": "stable-guid"},
    )
    # The normalized SHA-256 for "updated article" is fixed to prove that an
    # unchanged refresh exits before deleting embeddings or committing changes.
    session = _Session([existing])
    pipeline = FeedPipeline(session, _Embedder(), SimpleNamespace())

    item_id = await pipeline.process_entry(
        feed,
        "https://example.test/article",
        "Existing title",
        entry_guid="stable-guid",
        tenant_id="tenant-a",
    )

    assert item_id is None
    assert session.executed == []
    assert session.added == []
    assert session.commits == 0
