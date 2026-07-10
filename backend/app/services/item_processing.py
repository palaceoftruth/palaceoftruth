from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, select, text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.embedding_profile import is_default_embedding_profile, resolve_embedding_profile
from app.models.embedding import Embedding, EmbeddingProfileVector
from app.models.item import Item
from app.models.job import Job
from app.services.chunker import chunk_text
from app.services.embedder import EmbeddingRequestError, EmbeddingService
from app.services.embedding_storage import embedding_record_for_profile
from app.services.item_dates import apply_effective_date
from app.services.job_progress import job_event_status_for_job_status, record_job_progress_event
from app.services.llm import LLMService
from app.pipelines.base import stable_merge_tags
from app.utils.hash import compute_content_hash

logger = logging.getLogger(__name__)


@dataclass
class PrebuiltItemProcessResult:
    status: str
    item_id: uuid.UUID
    duplicate_of: uuid.UUID | None = None


async def _set_job_progress(
    db: AsyncSession,
    job: Job | None,
    *,
    phase: str,
    status: str | None = None,
    progress: int | None = None,
    message: str | None = None,
) -> None:
    if job is None:
        return
    if status is not None:
        job.status = status
    if progress is not None:
        job.progress = progress
    await record_job_progress_event(
        db,
        job=job,
        phase=phase,
        status=job_event_status_for_job_status(status),
        progress=job.progress if progress is None else progress,
        message=message,
    )


async def process_prebuilt_item(
    db: AsyncSession,
    *,
    item: Item,
    embedder: EmbeddingService,
    llm: LLMService,
    tenant_id: str,
    job: Job | None = None,
    enable_ai_enrichment: bool = False,
) -> PrebuiltItemProcessResult:
    """Chunk, embed, and optionally enrich a pre-created item.

    Used by the generic POST /items background embed flow and the memory facade's
    tracked memory-artifact worker. Caller-owned fields win: AI enrichment only
    fills summary/tags/categories when they are missing.
    """
    item_id = item.id
    job_id = job.id if job is not None else None
    if not item.raw_content:
        raise ValueError(f"Item {item_id} has no raw_content")

    try:
        await _set_job_progress(db, job, phase="started", status="processing", progress=15)
        await db.commit()

        raw_content = item.raw_content
        content_hash = compute_content_hash(raw_content)
        existing_id = await db.scalar(
            select(Item.id)
            .where(Item.content_hash == content_hash)
            .where(Item.id != item.id)
            .where(Item.tenant_id == tenant_id)
            .where(Item.status != "failed")
            .where(Item.status != "deleted")
            .where(Item.deleted_at.is_(None))
            .limit(1)
        )
        if existing_id:
            item.status = "failed"
            if job is not None:
                job.status = "duplicate"
                job.progress = 100
                job.duplicate_of = existing_id
                job.completed_at = datetime.now(timezone.utc)
                await record_job_progress_event(
                    db,
                    job=job,
                    phase="dedupe",
                    status="completed",
                    progress=100,
                    message="Duplicate content detected",
                    metadata={"duplicate_of": str(existing_id)},
                )
            await db.commit()
            logger.info(
                "prebuilt item %s is duplicate of item %s",
                item.id,
                existing_id,
            )
            return PrebuiltItemProcessResult(
                status="duplicate",
                item_id=item.id,
                duplicate_of=existing_id,
            )

        item.content_hash = content_hash
        chunks = chunk_text(raw_content)
        embeddings_data = await embedder.embed_texts([chunk["text"] for chunk in chunks])

        await _set_job_progress(db, job, phase="embedded", progress=70)
        await db.commit()

        if enable_ai_enrichment:
            text_preview = item.raw_content[:4000]
            vocab_result = await db.execute(
                sa_text(
                    "SELECT DISTINCT unnest(tags) AS tag FROM items"
                    " WHERE status='ready' AND tenant_id=:tid AND cardinality(tags) > 0"
                ).bindparams(tid=tenant_id)
            )
            existing_tags = [row.tag for row in vocab_result]
            summary, (llm_tags, categories) = await asyncio.gather(
                llm.summarize(text_preview),
                llm.generate_tags(text_preview, existing_tags=existing_tags),
            )
            if not item.summary:
                item.summary = summary
            if not item.tags:
                item.tags = stable_merge_tags(llm_tags)
            if not item.categories:
                item.categories = stable_merge_tags(categories)

        item.content_chunks = chunks
        apply_effective_date(item)
        item.status = "ready"

        # Retries and stale-job recovery can legitimately replay the same worker path.
        # Clear only the active profile so side-by-side comparison vectors survive.
        embedding_profile = getattr(embedder, "profile", resolve_embedding_profile())
        if is_default_embedding_profile(embedding_profile):
            await db.execute(delete(Embedding).where(Embedding.item_id == item.id))
        else:
            await db.execute(
                delete(EmbeddingProfileVector)
                .where(EmbeddingProfileVector.item_id == item.id)
                .where(EmbeddingProfileVector.profile_name == embedding_profile.profile_name)
            )

        for chunk, vector in zip(chunks, embeddings_data):
            db.add(
                embedding_record_for_profile(
                    item_id=item.id,
                    chunk_index=chunk["index"],
                    chunk_text=chunk["text"],
                    vector=vector,
                    profile=embedding_profile,
                )
            )

        if job is not None:
            job.status = "completed"
            job.progress = 100
            job.error_message = None
            job.completed_at = datetime.now(timezone.utc)
            await record_job_progress_event(
                db,
                job=job,
                phase="completed",
                status="completed",
                progress=100,
                message="Item processing completed",
            )

        await db.commit()
        return PrebuiltItemProcessResult(status="completed", item_id=item.id)

    except Exception as exc:
        await db.rollback()
        logger.exception("prebuilt item processing failed for item %s: %s", item_id, exc)
        try:
            # Rollback expires ORM state in async sessions. Reload by scalar IDs so
            # failure persistence cannot mask the provider error with MissingGreenlet.
            failed_item = await db.get(Item, item_id)
            failed_job = await db.get(Job, job_id) if job_id is not None else None
            if failed_item is not None:
                failed_item.status = "failed"
            if failed_job is not None:
                failed_job.status = "failed"
                failed_job.error_message = str(exc)[:500]
                failed_job.completed_at = datetime.now(timezone.utc)
                metadata: dict[str, object] = {"error_class": exc.__class__.__name__}
                if isinstance(exc, EmbeddingRequestError):
                    metadata.update(
                        {
                            "failure_kind": exc.failure_kind,
                            "retryable": exc.retryable,
                            "provider_status_code": exc.provider_status_code,
                        }
                    )
                await record_job_progress_event(
                    db,
                    job=failed_job,
                    phase="failed",
                    status="failed",
                    progress=failed_job.progress,
                    message=str(exc),
                    metadata=metadata,
                )
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("failed to persist processing error for item %s job %s", item_id, job_id)
        raise
