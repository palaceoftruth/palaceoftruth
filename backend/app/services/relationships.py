"""Relationship extraction service — centroid similarity + LLM classification."""
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic
from typing import Collection

from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.embedding_profile import resolve_embedding_profile
from app.config import settings
from app.models.item import Item
from app.services.embedder import EmbeddingService
from app.services.llm import LLMService
from app.services.relationship_telemetry import record_relationship_extraction
from app.services.search import _embedding_search_plan

logger = logging.getLogger(__name__)

_CANDIDATE_LIMIT = 5
RELATIONSHIP_EXTRACTION_MARKER_KEY = "_palace_relationship_extraction"
RELATIONSHIP_EXTRACTION_MARKER_VERSION = "1"


@dataclass(frozen=True)
class RelationshipOperationResult:
    """Bounded relationship outcome suitable for canary evidence."""

    relationship: str
    confidence: float
    validation_outcome: str
    provider: str
    upstream_provider: str
    requested_model: str
    model: str
    retry_provider: str
    fallback_used: bool
    retry_count: int
    prompt_version: str
    temperature: float | None
    seed: int | None
    persistence_min_confidence: float
    persistence_threshold_rejected: bool
    duration_seconds: float
    edge_persisted: bool


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

        attempted_candidates = 0
        for row in rows:
            if row.summary:
                await self._classify_candidate_records(
                    source=item,
                    target=row,
                    tenant_id=tenant_id,
                )
                attempted_candidates += 1

        if attempted_candidates:
            # Persist successful no-match attempts too. Without this marker,
            # deferred backfills continually reselect the same oldest items
            # whenever every candidate is classified as unrelated.
            metadata = dict(item.metadata_ or {})
            metadata[RELATIONSHIP_EXTRACTION_MARKER_KEY] = {
                "version": RELATIONSHIP_EXTRACTION_MARKER_VERSION,
                "content_hash": item.content_hash,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "candidate_count": attempted_candidates,
            }
            item.metadata_ = metadata

        await self.db.commit()

    async def classify_candidate(
        self,
        source_item_id: uuid.UUID,
        target_item_id: uuid.UUID,
        *,
        tenant_id: str,
        persist: bool = True,
        allowed_relationships: Collection[str] | None = None,
    ) -> RelationshipOperationResult:
        """Classify one exact tenant-scoped pair without candidate discovery."""

        source = await self.db.get(Item, source_item_id)
        target = await self.db.get(Item, target_item_id)
        for label, item in (("source", source), ("target", target)):
            if (
                item is None
                or str(item.tenant_id) != tenant_id
                or item.status != "ready"
                or item.deleted_at is not None
                or not item.summary
            ):
                raise ValueError(f"{label} item is not a ready canary endpoint")
        if source_item_id == target_item_id:
            raise ValueError("relationship endpoints must be different")

        result = await self._classify_candidate_records(
            source=source,
            target=target,
            tenant_id=tenant_id,
            persist=persist,
            allowed_relationships=allowed_relationships,
        )
        await self.db.commit()
        return result

    async def _classify_candidate_records(
        self,
        *,
        source,
        target,
        tenant_id: str,
        persist: bool = True,
        allowed_relationships: Collection[str] | None = None,
    ) -> RelationshipOperationResult:
        started_at = monotonic()
        detailed_classifier = getattr(self.llm, "classify_relationship_detailed", None)
        if callable(detailed_classifier):
            classification = await detailed_classifier(
                source.title,
                source.summary,
                target.title,
                target.summary,
            )
            rel_type = classification.relationship
            confidence = classification.confidence
            provider = classification.provider
            upstream_provider = getattr(classification, "upstream_provider", "unknown")
            requested_model = getattr(classification, "requested_model", "unknown")
            model = getattr(classification, "model", "unknown")
            retry_provider = classification.retry_provider
            validation_outcome = classification.validation_outcome
            fallback_used = classification.fallback_used
            retry_count = classification.retry_count
            prompt_version = getattr(classification, "prompt_version", "unknown")
            temperature = getattr(classification, "temperature", None)
            seed = getattr(classification, "seed", None)
        else:
            # Compatibility for test doubles and custom LLM implementations.
            rel_type, confidence = await self.llm.classify_relationship(
                source.title,
                source.summary,
                target.title,
                target.summary,
            )
            provider = "unknown"
            upstream_provider = "unknown"
            requested_model = "unknown"
            model = "unknown"
            retry_provider = "unknown"
            validation_outcome = "empty" if rel_type == "none" else "valid"
            fallback_used = False
            retry_count = 0
            prompt_version = "unknown"
            temperature = None
            seed = None

        duration_seconds = monotonic() - started_at
        relationship_allowed = allowed_relationships is None or rel_type in allowed_relationships
        persistence_min_confidence = settings.relationship_extraction_min_confidence
        persistence_threshold_rejected = (
            rel_type != "none" and confidence < persistence_min_confidence
        )
        should_persist = (
            persist
            and relationship_allowed
            and not persistence_threshold_rejected
            and rel_type != "none"
        )
        edge_persisted = False
        if should_persist:
            # Candidate rows can disappear while the LLM call is running. Lock
            # surviving endpoints and no-op if either side is no longer ready.
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
                INSERT INTO item_relationships (tenant_id, source_item_id, target_item_id, relationship, confidence)
                SELECT :tenant_id, source_item_id, target_item_id, :rel, :conf
                FROM endpoints
                ON CONFLICT (source_item_id, target_item_id, relationship)
                DO UPDATE SET confidence = EXCLUDED.confidence
                RETURNING 1
            """), {
                "source": str(source.id),
                "target": str(target.id),
                "tenant_id": tenant_id,
                "rel": rel_type,
                "conf": confidence,
            })
            edge_persisted = result.scalar_one_or_none() is not None

        record_relationship_extraction(
            provider=provider,
            retry_provider=retry_provider,
            validation_outcome=validation_outcome,
            fallback_used=fallback_used,
            retry_count=retry_count,
            duration_seconds=duration_seconds,
            edges_extracted=1 if edge_persisted else 0,
        )
        if edge_persisted:
            logger.info(
                "Stored relationship %s→%s: %s (confidence=%.2f)",
                source.id,
                target.id,
                rel_type,
                confidence,
            )
        else:
            logger.debug(
                "Skipped relationship %s→%s: type=%s confidence=%.2f persist=%s allowed=%s",
                source.id,
                target.id,
                rel_type,
                confidence,
                persist,
                relationship_allowed,
            )
        return RelationshipOperationResult(
            relationship=rel_type,
            confidence=confidence,
            validation_outcome=validation_outcome,
            provider=provider,
            upstream_provider=upstream_provider,
            requested_model=requested_model,
            model=model,
            retry_provider=retry_provider,
            fallback_used=fallback_used,
            retry_count=retry_count,
            prompt_version=prompt_version,
            temperature=temperature,
            seed=seed,
            persistence_min_confidence=persistence_min_confidence,
            persistence_threshold_rejected=persistence_threshold_rejected,
            duration_seconds=duration_seconds,
            edge_persisted=edge_persisted,
        )
