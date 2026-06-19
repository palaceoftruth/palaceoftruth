"""FeedPipeline — processes a single RSS/Atom feed article into an Item."""
import asyncio
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.embedding_profile import resolve_embedding_profile
from app.models.feed import Feed
from app.models.item import Item
from app.pipelines.base import BasePipeline, stable_merge_tags
from app.pipelines.webpage import WebpagePipeline
from app.services.chunker import chunk_text
from app.services.embedder import EmbeddingService
from app.services.embedding_storage import embedding_record_for_profile
from app.services.item_dates import apply_effective_date
from app.services.llm import LLMService
from app.utils.hash import compute_content_hash
from sqlalchemy import text as sa_text

logger = logging.getLogger(__name__)


class FeedPipeline(BasePipeline):
    """Processes a single feed entry: scrape → chunk → embed → enrich → store."""

    def __init__(self, db: AsyncSession, embedder: EmbeddingService, llm: LLMService):
        super().__init__(db, embedder, llm)

    async def process_entry(
        self,
        feed: Feed,
        entry_url: str,
        entry_title: str,
        entry_summary: str = "",
        entry_author: str | None = None,
        entry_published: str | None = None,
        entry_guid: str | None = None,
        tenant_id: str = "default",
    ) -> uuid.UUID | None:
        """Process a single feed entry. Returns item.id on success, None if skipped/failed."""

        # 1. Deduplication check by source_url (scoped to tenant)
        existing = (await self.db.execute(
            select(Item).where(Item.source_url == entry_url).where(Item.tenant_id == tenant_id)
        )).scalar_one_or_none()
        if existing:
            logger.debug("Skipping duplicate entry: %s", entry_url)
            return None

        # 2. Create item with status=processing
        item = Item(
            source_type="feed_article",
            source_url=entry_url,
            title=entry_title or entry_url,
            status="processing",
            tenant_id=tenant_id,
            metadata_={
                "feed_id": str(feed.id),
                "feed_url": feed.url,
                "feed_name": feed.name,
                "feed_guid": entry_guid,
                "author": entry_author,
                "published": entry_published,
            },
        )
        apply_effective_date(item)
        self.db.add(item)
        await self.db.flush()  # populate item.id

        try:
            # 3. Extract full article text via trafilatura; fallback to feed summary
            loop = asyncio.get_event_loop()
            _html, article_text, scrape_meta = await loop.run_in_executor(
                None, WebpagePipeline._scrape, entry_url
            )
            if not article_text:
                article_text = entry_summary
                scrape_meta = {"content_source": "feed_summary"}
            else:
                scrape_meta["content_source"] = "full_article"

            if not article_text:
                raise ValueError(f"No content extractable for {entry_url}")

        except Exception as exc:
            item.status = "failed"
            item.metadata_["extract_error"] = str(exc)[:500]
            await self.db.commit()
            logger.warning("FeedPipeline: failed to extract content for %s: %s", entry_url, exc)
            return None

        # Dedup: check for existing item with same content hash (scoped to tenant)
        content_hash = compute_content_hash(article_text)
        existing_by_hash = await self.db.scalar(
            select(Item.id)
            .where(Item.content_hash == content_hash)
            .where(Item.tenant_id == tenant_id)
            .where(Item.status != "failed")
            .where(Item.status != "deleted")
            .where(Item.deleted_at.is_(None))
            .limit(1)
        )
        if existing_by_hash:
            # Silent skip — consistent with URL dedup above; no job to mark
            item.status = "failed"
            await self.db.commit()
            logger.info(
                "Feed article duplicate (hash collision): matches item %s", existing_by_hash
            )
            return None

        # 4. Chunk → embed → AI enrich
        chunks = chunk_text(article_text)
        chunk_texts_list = [c["text"] for c in chunks]
        embeddings_data = await self.embedder.embed_texts(chunk_texts_list) if chunk_texts_list else []

        # Query existing tag vocabulary for reuse (vocabulary-aware tagging, scoped to tenant)
        vocab_result = await self.db.execute(
            sa_text(
                "SELECT DISTINCT unnest(tags) AS tag FROM items"
                " WHERE status='ready' AND tenant_id=:tid AND cardinality(tags) > 0"
            ).bindparams(tid=tenant_id)
        )
        existing_tags = [row.tag for row in vocab_result]

        summary, llm_tags, categories, entities_dict = await self._run_enrichment(
            article_text[:4000], existing_tags
        )

        # 5. Merge feed auto_tags with LLM tags in stable provenance order.
        merged_tags = stable_merge_tags(feed.auto_tags or [], llm_tags)

        item.raw_content = article_text
        item.content_chunks = chunks
        item.summary = summary
        item.tags = merged_tags
        item.categories = stable_merge_tags(categories)
        merged_metadata = {**item.metadata_, **scrape_meta}
        if entities_dict:
            merged_metadata["entities"] = entities_dict
        item.metadata_ = merged_metadata
        apply_effective_date(item, metadata=merged_metadata)
        item.content_hash = content_hash
        item.status = "ready"
        await self.db.flush()

        # 6. Store embeddings
        for chunk, vector in zip(chunks, embeddings_data):
            emb = embedding_record_for_profile(
                item_id=item.id,
                chunk_index=chunk["index"],
                chunk_text=chunk["text"],
                vector=vector,
                profile=getattr(self.embedder, "profile", resolve_embedding_profile()),
            )
            self.db.add(emb)

        await self.db.commit()
        logger.info("FeedPipeline: processed item %s from feed %s", item.id, feed.id)
        return item.id
