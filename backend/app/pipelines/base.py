import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.embedding_profile import resolve_embedding_profile
from app.models.item import Item
from app.models.job import Job
from app.services.chunker import chunk_text
from app.services.embedder import EmbeddingService
from app.services.embedding_storage import embedding_record_for_profile
from app.services.job_progress import job_event_status_for_job_status, record_job_progress_event
from app.services.item_dates import apply_effective_date
from app.services.llm import LLMService
from app.utils.hash import compute_content_hash

logger = logging.getLogger(__name__)


class PendingAvailabilityError(RuntimeError):
    """Raised when a provider says content exists but is not available yet."""

    provider: str = "provider"
    status_code: str = "pending_availability"
    fallback_retry_after_seconds: int = 3600

    def __init__(
        self,
        provider_message: str,
        *,
        retry_after_seconds: int | None = None,
        user_message: str | None = None,
    ) -> None:
        super().__init__(provider_message)
        self.provider_message = provider_message
        self.retry_after_seconds = max(60, retry_after_seconds or self.fallback_retry_after_seconds)
        self.user_message = user_message or f"{self.provider} content is not available yet; Palace will retry later."

    @property
    def retry_after_at(self) -> datetime:
        return datetime.now(timezone.utc) + timedelta(seconds=self.retry_after_seconds)


def _content_hash_for_pipeline(source_type: str, raw_text: str, metadata: dict[str, Any]) -> str:
    if source_type == "image":
        image_analysis = metadata.get("image_analysis")
        if isinstance(image_analysis, dict):
            byte_hash = image_analysis.get("byte_hash")
            if isinstance(byte_hash, str) and len(byte_hash) == 64:
                return byte_hash
    return compute_content_hash(raw_text)


def stable_merge_tags(*tag_groups: list[str] | tuple[str, ...] | None) -> list[str]:
    """Merge tag groups in order while trimming, lowercasing, and removing blanks."""
    merged: list[str] = []
    seen: set[str] = set()
    for tags in tag_groups:
        if not tags:
            continue
        for tag in tags:
            normalized = str(tag).strip().lower()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            merged.append(normalized)
    return merged


class BasePipeline:
    """Orchestrates: extract → chunk → embed → AI enrich → store."""

    def __init__(self, db: AsyncSession, embedder: EmbeddingService, llm: LLMService):
        self.db = db
        self.embedder = embedder
        self.llm = llm

    async def extract(self, **kwargs) -> tuple[str, dict[str, Any]]:
        """Override per pipeline. Returns (raw_text, metadata_dict)."""
        raise NotImplementedError

    async def _run_enrichment(
        self,
        text_preview: str,
        existing_tags: list[str],
        model: str | None = None,
    ) -> tuple[str, list[str], list[str], dict]:
        """Run summarize, generate_tags, and extract_entities in parallel.

        Returns (summary, tags, categories, entities_dict). Any individual failure
        returns a safe default — the other results are unaffected.
        Uses return_exceptions=True so a failed entity call does not kill the gather.
        """
        results = await asyncio.gather(
            self.llm.summarize(text_preview, model=model),
            self.llm.generate_tags(text_preview, existing_tags=existing_tags, model=model),
            self.llm.extract_entities(text_preview, model=model),
            return_exceptions=True,
        )
        summary = results[0] if not isinstance(results[0], BaseException) else ""
        tag_result = results[1] if not isinstance(results[1], BaseException) else ([], [])
        entities_model = results[2] if not isinstance(results[2], BaseException) else None
        tags, categories = tag_result
        entities_dict = entities_model.model_dump() if entities_model is not None else {}
        if isinstance(results[0], BaseException):
            logger.warning("Summarize failed during enrichment: %s", results[0])
        if isinstance(results[1], BaseException):
            logger.warning("generate_tags failed during enrichment: %s", results[1])
        if isinstance(results[2], BaseException):
            logger.warning("extract_entities failed during enrichment: %s", results[2])
        return summary, tags, categories, entities_dict

    async def process(self, job_id: uuid.UUID, tenant_id: str = "default", model: str | None = None, **kwargs) -> uuid.UUID:
        """Full pipeline: extract → chunk → embed → enrich → store."""
        job = await self.db.get(Job, job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")

        try:
            # --- Extract ---
            if job.status == PendingAvailabilityError.status_code:
                job.error_message = None
                payload = dict(job.payload or {})
                payload.pop("pending_availability", None)
                job.payload = payload
            await self._update_job(job, phase="extract", status="processing", progress=10)
            raw_text, metadata = await self.extract(job_id=str(job_id), **kwargs)
            await self._update_job(job, phase="extracted", progress=20)

            # --- Dedup: check for existing item with same content hash (scoped to tenant) ---
            content_hash = _content_hash_for_pipeline(job.job_type, raw_text, metadata)
            existing_id = await self.db.scalar(
                select(Item.id)
                .where(Item.content_hash == content_hash)
                .where(Item.id != job.item_id)
                .where(Item.tenant_id == tenant_id)
                .where(Item.status != "failed")
                .where(Item.status != "deleted")
                .where(Item.deleted_at.is_(None))
                .limit(1)
            )
            if existing_id:
                # Mark job as duplicate (not a failure — the content already exists)
                await self.db.rollback()
                item = await self.db.get(Item, job.item_id)
                if item:
                    item.status = "failed"
                job = await self.db.get(Job, job_id)
                if job:
                    job.status = "duplicate"
                    job.duplicate_of = existing_id
                    job.completed_at = datetime.now(timezone.utc)
                    await record_job_progress_event(
                        self.db,
                        job=job,
                        phase="dedupe",
                        status="completed",
                        progress=100,
                        message="Duplicate content detected",
                        metadata={"duplicate_of": str(existing_id)},
                    )
                await self.db.commit()
                logger.info(
                    "Job %s: duplicate of item %s (content hash collision)",
                    job_id,
                    existing_id,
                )
                return existing_id

            # --- Chunk ---
            chunks = chunk_text(raw_text)
            await self._update_job(job, phase="chunked", progress=40)

            # --- Embed ---
            chunk_texts = [c["text"] for c in chunks]
            embeddings_data = await self.embedder.embed_texts(chunk_texts) if chunk_texts else []
            await self._update_job(job, phase="embedded", progress=60)

            # --- AI Enrich (best-effort — item is still saved if LLMs are unavailable) ---
            summary = ""
            tags: list[str] = []
            categories: list[str] = []
            entities_dict: dict = {}
            try:
                text_preview = raw_text[:4000]
                vocab_result = await self.db.execute(
                    sa_text(
                        "SELECT DISTINCT unnest(tags) AS tag FROM items"
                        " WHERE status='ready' AND tenant_id=:tid AND cardinality(tags) > 0"
                    ).bindparams(tid=tenant_id)
                )
                existing_tags = [row.tag for row in vocab_result]
                summary, tags, categories, entities_dict = await self._run_enrichment(
                    text_preview, existing_tags, model=model
                )
            except Exception as enrich_exc:
                logger.warning("AI enrichment failed for job %s (continuing without): %s", job_id, enrich_exc)
            await self._update_job(job, phase="enriched", progress=80)

            # --- Store item ---
            item = await self.db.get(Item, job.item_id)
            if not item:
                raise ValueError(f"Item {job.item_id} not found for job {job_id}")

            item.raw_content = raw_text
            item.content_chunks = chunks
            item.summary = summary
            manual_tags = metadata.get("manual_tags") if isinstance(metadata.get("manual_tags"), list) else []
            item.tags = stable_merge_tags(item.tags, manual_tags, tags)
            item.categories = stable_merge_tags(categories)
            merged_metadata = {**(item.metadata_ or {}), **metadata}
            if entities_dict:
                merged_metadata["entities"] = entities_dict
            item.metadata_ = merged_metadata
            apply_effective_date(item, metadata=merged_metadata)
            item.content_hash = content_hash
            item.tenant_id = tenant_id
            item.status = "ready"
            await self.db.flush()

            # --- Store embeddings ---
            for i, (chunk, vector) in enumerate(zip(chunks, embeddings_data)):
                emb = embedding_record_for_profile(
                    item_id=item.id,
                    chunk_index=chunk["index"],
                    chunk_text=chunk["text"],
                    vector=vector,
                    profile=getattr(self.embedder, "profile", resolve_embedding_profile()),
                )
                self.db.add(emb)

            await self.db.commit()
            await self._update_job(job, phase="completed", status="completed", progress=100,
                                   completed_at=datetime.now(timezone.utc))
            logger.info("Job %s completed: item %s", job_id, item.id)
            return item.id

        except asyncio.CancelledError as exc:
            await self.db.rollback()
            logger.warning("Job %s cancelled during pipeline processing", job_id)
            job = await self.db.get(Job, job_id)
            if job:
                await self._update_job(
                    job,
                    phase="cancelled",
                    status="cancelled",
                    error_message="Worker cancelled the job before completion",
                    completed_at=datetime.now(timezone.utc),
                )
                if job.item_id:
                    item = await self.db.get(Item, job.item_id)
                    if item and item.status == "processing":
                        item.status = "failed"
                        await self.db.commit()
            raise

        except PendingAvailabilityError as exc:
            await self.db.rollback()
            retry_after_at = exc.retry_after_at
            logger.info(
                "Job %s pending %s availability until %s: %s",
                job_id,
                exc.provider,
                retry_after_at.isoformat(),
                exc.provider_message,
            )
            job = await self.db.get(Job, job_id)
            if job:
                payload = dict(job.payload or {})
                payload["pending_availability"] = {
                    "status": exc.status_code,
                    "provider": exc.provider,
                    "provider_message": exc.provider_message,
                    "retryable": True,
                    "retry_after_seconds": exc.retry_after_seconds,
                    "retry_after_at": retry_after_at.isoformat(),
                }
                job.payload = payload
                await self._update_job(
                    job,
                    phase=exc.status_code,
                    status=exc.status_code,
                    progress=10,
                    error_message=exc.user_message,
                    event_status="queued",
                    event_message=exc.user_message,
                    event_metadata=payload["pending_availability"],
                    completed_at=None,
                )
            if job and job.item_id:
                item = await self.db.get(Item, job.item_id)
                if item and item.status == "processing":
                    item.metadata_ = {
                        **(item.metadata_ or {}),
                        "pending_availability": payload["pending_availability"],
                    }
                    await self.db.commit()
            raise

        except Exception as exc:
            await self.db.rollback()
            logger.exception("Job %s failed: %s", job_id, exc)
            job = await self.db.get(Job, job_id)
            if job:
                await self._update_job(
                    job,
                    phase="failed",
                    status="failed",
                    error_message=str(exc)[:500],
                    event_metadata={"error_class": exc.__class__.__name__},
                    completed_at=datetime.now(timezone.utc),
                )
            # Also mark item as failed
            if job and job.item_id:
                item = await self.db.get(Item, job.item_id)
                if item:
                    item.status = "failed"
                    await self.db.commit()
            raise

    async def _update_job(
        self,
        job: Job,
        *,
        phase: str,
        status: str | None = None,
        progress: int | None = None,
        error_message: str | None = None,
        event_status: str | None = None,
        event_message: str | None = None,
        event_metadata: dict[str, Any] | None = None,
        completed_at: datetime | None = None,
    ) -> None:
        if status is not None:
            job.status = status
        if progress is not None:
            job.progress = progress
        if error_message is not None:
            job.error_message = error_message
        if completed_at is not None:
            job.completed_at = completed_at
        await record_job_progress_event(
            self.db,
            job=job,
            phase=phase,
            status=event_status or job_event_status_for_job_status(status),
            progress=job.progress if progress is None else progress,
            message=event_message or error_message,
            metadata=event_metadata or ({"error_class": "JobError"} if error_message else None),
        )
        await self.db.commit()
