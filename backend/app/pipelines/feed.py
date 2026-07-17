"""FeedPipeline — processes a single RSS/Atom feed article into an Item."""
import asyncio
import logging
import uuid

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.embedding_profile import resolve_embedding_profile
from app.models.feed import Feed
from app.models.embedding import Embedding
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

        try:
            # Extract before deciding whether an existing identity changed.  A
            # permanent URL skip would hide edited feed entries forever.
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
            logger.warning("FeedPipeline: failed to extract content for %s: %s", entry_url, exc)
            return None

        content_hash = compute_content_hash(article_text)
        identity_filters = [Item.source_url == entry_url]
        if entry_guid:
            identity_filters.append(
                (Item.metadata_["feed_id"].astext == str(feed.id))
                & (Item.metadata_["feed_guid"].astext == entry_guid)
            )
        existing = await self.db.scalar(
            select(Item)
            .where(Item.tenant_id == tenant_id)
            .where(or_(*identity_filters))
            .order_by(Item.updated_at.desc())
            .limit(1)
        )
        if existing is not None and existing.content_hash == content_hash:
            logger.debug("Skipping unchanged feed entry: %s", entry_url)
            return None

        # A changed GUID/canonical URL reuses its stable item identity and
        # replaces its derived embeddings only after the new extraction works.
        item = existing or Item(
            source_type="feed_article",
            source_url=entry_url,
            title=entry_title or entry_url,
            status="processing",
            tenant_id=tenant_id,
            metadata_={},
        )
        if existing is None:
            self.db.add(item)
            await self.db.flush()  # populate item.id
        else:
            await self.db.execute(delete(Embedding).where(Embedding.item_id == item.id))

        # Deduplication by content still protects the corpus, but never treats
        # the item being refreshed as its own duplicate.
        existing_by_hash = await self.db.scalar(
            select(Item.id)
            .where(Item.content_hash == content_hash)
            .where(Item.tenant_id == tenant_id)
            .where(Item.status != "failed")
            .where(Item.status != "deleted")
            .where(Item.deleted_at.is_(None))
            .where(Item.id != item.id)
            .limit(1)
        )
        if existing_by_hash:
            if existing is None:
                # The new row is not useful when another current item already
                # owns identical content; preserve existing records instead.
                await self.db.delete(item)
                await self.db.commit()
            logger.info(
                "Feed article duplicate (hash collision): matches item %s", existing_by_hash
            )
            return None

        # Chunk → embed → AI enrich.
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

        item.source_url = entry_url
        item.title = entry_title or entry_url
        item.source_type = "feed_article"
        item.raw_content = article_text
        item.content_chunks = chunks
        item.summary = summary
        item.tags = merged_tags
        item.categories = stable_merge_tags(categories)
        merged_metadata = {
            **(item.metadata_ or {}),
            "feed_id": str(feed.id),
            "feed_url": feed.url,
            "feed_name": feed.name,
            "feed_guid": entry_guid,
            "author": entry_author,
            "published": entry_published,
            **scrape_meta,
        }
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
