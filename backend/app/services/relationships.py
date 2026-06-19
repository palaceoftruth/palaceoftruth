"""Relationship extraction service — centroid similarity + LLM classification."""
import logging
import uuid

from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.embedding_profile import resolve_embedding_profile
from app.models.item import Item
from app.services.embedder import EmbeddingService
from app.services.llm import LLMService
from app.services.search import _embedding_search_plan

logger = logging.getLogger(__name__)

_MIN_CONFIDENCE = 0.5
_CANDIDATE_LIMIT = 5


class RelationshipService:
    def __init__(self, db: AsyncSession, embedder: EmbeddingService, llm: LLMService):
        self.db = db
        self.embedder = embedder
        self.embedding_profile = getattr(embedder, "profile", resolve_embedding_profile())
        self.llm = llm

    async def find_relationships(self, item_id: uuid.UUID, tenant_id: str | None = None) -> None:
        """Find and store relationships between item_id and existing items.

        Steps:
        1. Skip if fewer than 2 ready items exist.
        2. Load the source item; skip if no summary.
        3. Find top-5 similar items by embedding centroid cosine similarity.
        4. For each candidate, classify relationship via LLM and store if confidence >= 0.5.
        """
        item = await self.db.get(Item, item_id)
        if not item or not item.summary:
            logger.debug("Skipping relationship extraction: item %s missing or has no summary", item_id)
            return
        tenant_id = tenant_id or str(item.tenant_id)
        if str(item.tenant_id) != tenant_id:
            logger.debug(
                "Skipping relationship extraction: item %s belongs to tenant %s, not %s",
                item_id,
                item.tenant_id,
                tenant_id,
            )
            return

        # Need at least 2 items (source + at least one candidate) within the same tenant.
        count = (
            await self.db.execute(
                sa_text("SELECT COUNT(*) FROM items WHERE status='ready' AND tenant_id = :tenant_id"),
                {"tenant_id": tenant_id},
            )
        ).scalar_one()
        if count < 2:
            logger.debug("Skipping relationship extraction: fewer than 2 ready items in tenant %s", tenant_id)
            return

        # Find top-N similar items by centroid cosine similarity for the active profile.
        # AVG(vector) on pgvector columns returns vector — no explicit CAST needed.
        # Use CAST(:item_id AS uuid) for the UUID parameter.
        embedding_plan = _embedding_search_plan(self.embedding_profile)
        sql = sa_text(f"""
            WITH centroid AS (
                SELECT AVG({embedding_plan.vector_column}) AS vec
                FROM {embedding_plan.table_name} e
                WHERE item_id = CAST(:item_id AS uuid)
                  {embedding_plan.profile_filter}
            )
            SELECT i.id, i.title, i.summary,
                   1 - (AVG(e.{embedding_plan.vector_column}) <=> (SELECT vec FROM centroid)) AS similarity
            FROM {embedding_plan.table_name} e
            JOIN items i ON e.item_id = i.id
            WHERE i.status = 'ready'
              AND i.deleted_at IS NULL
              AND i.tenant_id = :tenant_id
              AND e.item_id != CAST(:item_id AS uuid)
              {embedding_plan.profile_filter}
            GROUP BY i.id, i.title, i.summary
            ORDER BY similarity DESC
            LIMIT :limit
        """)
        rows = (
            await self.db.execute(
                sql,
                {
                    "item_id": str(item_id),
                    "limit": _CANDIDATE_LIMIT,
                    "tenant_id": tenant_id,
                    "embedding_profile_name": embedding_plan.profile_name,
                    "embedding_dimensions": embedding_plan.dimensions,
                },
            )
        ).fetchall()

        for row in rows:
            if not row.summary:
                continue

            rel_type, confidence = await self.llm.classify_relationship(
                item.title, item.summary, row.title, row.summary
            )

            if confidence < _MIN_CONFIDENCE or rel_type == "none":
                logger.debug(
                    "Skipping relationship %s→%s: type=%s confidence=%.2f",
                    item_id, row.id, rel_type, confidence,
                )
                continue

            # Candidate rows can disappear while the LLM call is running, especially
            # during benchmark cleanup. Lock surviving endpoints and no-op if either
            # side is gone before inserting the FK-backed relationship row.
            result = await self.db.execute(sa_text("""
                WITH endpoints AS (
                    SELECT src.id AS source_item_id, dst.id AS target_item_id
                    FROM items src
                    JOIN items dst ON dst.id = CAST(:target AS uuid)
                    WHERE src.id = CAST(:source AS uuid)
                      AND src.tenant_id = :tenant_id
                      AND dst.tenant_id = :tenant_id
                      AND src.status = 'ready'
                      AND dst.status = 'ready'
                      AND src.deleted_at IS NULL
                      AND dst.deleted_at IS NULL
                    FOR KEY SHARE OF src, dst
                )
                INSERT INTO item_relationships (source_item_id, target_item_id, relationship, confidence)
                SELECT source_item_id, target_item_id, :rel, :conf
                FROM endpoints
                ON CONFLICT (source_item_id, target_item_id, relationship)
                DO UPDATE SET confidence = EXCLUDED.confidence
                RETURNING 1
            """), {
                "source": str(item_id),
                "target": str(row.id),
                "tenant_id": tenant_id,
                "rel": rel_type,
                "conf": confidence,
            })
            if result.scalar_one_or_none() is None:
                logger.info(
                    "Skipped relationship %s→%s: endpoint missing or no longer ready",
                    item_id,
                    row.id,
                )
                continue
            logger.info(
                "Stored relationship %s→%s: %s (confidence=%.2f)",
                item_id, row.id, rel_type, confidence,
            )

        await self.db.commit()
