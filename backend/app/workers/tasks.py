"""ARQ task functions — one per ingestion pipeline type."""
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, text

from app.database import async_session
from app.models.embedding import Embedding
from app.models.item import Item
from app.models.job import Job
from app.pipelines.base import PendingAvailabilityError
from app.pipelines.base import stable_merge_tags
from app.pipelines.youtube import MediaPipeline, MediaTranscriptionLimitError
from app.pipelines.webpage import WebpagePipeline
from app.pipelines.pdf import PDFPipeline
from app.pipelines.doc import DocPipeline
from app.pipelines.image import ImagePipeline
from app.pipelines.note import NotePipeline
from app.services.bundle import run_restore_job
from app.services.image_analysis import ImageAnalysisError
from app.services.item_processing import process_prebuilt_item
from app.services.job_progress import record_job_progress_event
from app.services.memory import (
    MEMORY_JOB_TYPE,
    STALE_MEMORY_PROCESSING_MINUTES,
    STALE_MEMORY_QUEUED_MINUTES,
)
from app.services.source_subscriptions import reflect_source_subscription_entry_for_job
from app.workers.queues import (
    DEFAULT_WORKER_QUEUE,
    MEDIA_FAIR_DISPATCH_TASK_NAME,
    enqueue_default_job,
    enqueue_palace_job,
    enqueue_worker_job,
    singleton_job_id,
)
from app.utils.webhook import maybe_dispatch_webhook

logger = logging.getLogger(__name__)

_RELATIONSHIP_BACKFILL_DEFAULT_LIMIT = 50
_RELATIONSHIP_BACKFILL_MAX_LIMIT = 500
_RELATIONSHIP_BACKFILL_DEFAULT_DEFER_SECONDS = 15
_RELATIONSHIP_BACKFILL_MAX_DEFER_SECONDS = 3600
_RELATIONSHIP_POLICIES = {"immediate", "deferred", "skip"}
_TAXONOMY_BACKFILL_DEFAULT_LIMIT = 50
_TAXONOMY_BACKFILL_MAX_LIMIT = 500
_TAXONOMY_BACKFILL_SOURCE_TYPES = ("media", "webpage", "pdf", "doc", "image", "note")


def _empty_taxonomy_condition_sql() -> str:
    return "(cardinality(i.tags) = 0 OR cardinality(i.categories) = 0)"


def _increment_nested_count(
    report: dict[str, object],
    key: str,
    outer: str | None,
    inner: str | None = None,
) -> None:
    bucket = report.setdefault(key, {})
    assert isinstance(bucket, dict)
    outer_key = outer or "unknown"
    if inner is None:
        bucket[outer_key] = int(bucket.get(outer_key, 0)) + 1
        return
    inner_bucket = bucket.setdefault(outer_key, {})
    assert isinstance(inner_bucket, dict)
    inner_key = inner or "unknown"
    inner_bucket[inner_key] = int(inner_bucket.get(inner_key, 0)) + 1


def _taxonomy_backfill_error_payload(exc: Exception) -> dict[str, str]:
    return {
        "error_class": exc.__class__.__name__,
        "error": "taxonomy generation failed",
    }


async def _record_image_analysis_failure(job_id: str, exc: ImageAnalysisError) -> None:
    async with async_session() as db:
        job = await db.get(Job, uuid.UUID(job_id))
        if job is None:
            return

        payload = dict(job.payload or {})
        retry_task = payload.get("retry_task")
        task_kwargs = retry_task.get("kwargs") if isinstance(retry_task, dict) else None
        image_metadata = task_kwargs.get("image_metadata") if isinstance(task_kwargs, dict) else None
        if not isinstance(image_metadata, dict):
            image_metadata = {}
        analysis = image_metadata.get("image_analysis")
        if not isinstance(analysis, dict):
            analysis = {}
        vision = analysis.get("vision")
        if not isinstance(vision, dict):
            vision = {}
        error_payload = {
            "message": str(exc),
            "retryable": exc.retryable,
            "provider_status_code": exc.provider_status_code,
        }
        analysis = {
            **analysis,
            "status": "failed",
            "vision": {
                **vision,
                "error": error_payload,
            },
        }
        image_metadata = {**image_metadata, "image_analysis": analysis}
        if isinstance(task_kwargs, dict):
            task_kwargs["image_metadata"] = image_metadata
        job.payload = payload
        job.status = "failed"
        job.progress = 100
        job.error_message = str(exc)[:500]
        job.completed_at = datetime.now(timezone.utc)

        if job.item_id:
            item = await db.get(Item, job.item_id)
            if item is not None:
                item.status = "failed"
                item.metadata_ = {**(item.metadata_ or {}), **image_metadata}

        await record_job_progress_event(
            db,
            job=job,
            phase="vision",
            status="failed",
            progress=100,
            message=str(exc),
            metadata={
                "error_class": exc.__class__.__name__,
                "retryable": exc.retryable,
                "provider_status_code": exc.provider_status_code,
            },
        )
        await db.commit()


def _relationship_policy_from_job(job: Job) -> str:
    policy = str((job.payload or {}).get("relationship_policy", "immediate"))
    if policy not in _RELATIONSHIP_POLICIES:
        logger.warning(
            "memory_artifact: unknown relationship_policy=%s for job %s; using immediate",
            policy,
            job.id,
        )
        return "immediate"
    return policy


async def process_media(ctx: dict, job_id: str, url: str, tenant_id: str = "default", model: str | None = None, **_ignored_future_kwargs) -> None:
    item_id = None
    try:
        async with async_session() as db:
            pipeline = MediaPipeline(db, ctx["embedder"], ctx["llm"])
            item_id = await pipeline.process(uuid.UUID(job_id), url=url, tenant_id=tenant_id, model=model)
        await enqueue_default_job(ctx["redis"], "extract_relationships", item_id=str(item_id), tenant_id=tenant_id)
        await enqueue_palace_job(ctx["redis"], "mark_item_dirty_and_schedule", item_id=str(item_id), tenant_id=tenant_id, reason="ingest")
    except MediaTranscriptionLimitError as exc:
        logger.info("process_media marked job %s failed without retry: %s", job_id, exc)
    except PendingAvailabilityError as exc:
        logger.info(
            "process_media deferred job %s for %s pending availability retry in %ss",
            job_id,
            exc.provider,
            exc.retry_after_seconds,
        )
        try:
            await ctx["redis"].enqueue_job(
                MEDIA_FAIR_DISPATCH_TASK_NAME,
                _queue_name=DEFAULT_WORKER_QUEUE,
                _job_id=singleton_job_id(MEDIA_FAIR_DISPATCH_TASK_NAME, "media", "pending-availability", job_id),
                _defer_by=exc.retry_after_seconds,
            )
        except Exception:
            logger.exception("process_media could not schedule pending availability retry wake for job %s", job_id)
    finally:
        try:
            async with async_session() as db:
                await reflect_source_subscription_entry_for_job(db, job_id=uuid.UUID(job_id))
        except Exception:
            logger.exception("process_media could not reflect source subscription entry for job %s", job_id)
        try:
            await enqueue_worker_job(ctx["redis"], "process_media")
        except Exception:
            logger.exception("process_media could not wake tenant-fair media dispatcher after job %s", job_id)
        await maybe_dispatch_webhook(ctx["redis"], job_id)


# Keep old name registered so any queued jobs still in Redis drain cleanly.
async def process_youtube(ctx: dict, job_id: str, url: str, tenant_id: str = "default", model: str | None = None, **ignored_future_kwargs) -> None:
    await process_media(ctx, job_id=job_id, url=url, tenant_id=tenant_id, model=model, **ignored_future_kwargs)


async def process_webpage(ctx: dict, job_id: str, url: str, tenant_id: str = "default", model: str | None = None, **_ignored_future_kwargs) -> None:
    item_id = None
    try:
        async with async_session() as db:
            pipeline = WebpagePipeline(db, ctx["embedder"], ctx["llm"])
            item_id = await pipeline.process(uuid.UUID(job_id), url=url, tenant_id=tenant_id, model=model)
        await ctx["redis"].enqueue_job("extract_relationships", item_id=str(item_id), tenant_id=tenant_id)
        await enqueue_palace_job(ctx["redis"], "mark_item_dirty_and_schedule", item_id=str(item_id), tenant_id=tenant_id, reason="ingest")
    finally:
        await maybe_dispatch_webhook(ctx["redis"], job_id)


async def process_pdf(ctx: dict, job_id: str, extracted_text: str, pdf_metadata: dict | None = None, content_hash: str | None = None, tenant_id: str = "default", webhook_url: str | None = None, signing_key: str | None = None, model: str | None = None, **_ignored_future_kwargs) -> None:
    item_id = None
    try:
        async with async_session() as db:
            pipeline = PDFPipeline(db, ctx["embedder"], ctx["llm"])
            item_id = await pipeline.process(
                uuid.UUID(job_id),
                extracted_text=extracted_text,
                pdf_metadata=pdf_metadata or {},
                tenant_id=tenant_id,
                model=model,
            )
        await ctx["redis"].enqueue_job("extract_relationships", item_id=str(item_id), tenant_id=tenant_id)
        await enqueue_palace_job(ctx["redis"], "mark_item_dirty_and_schedule", item_id=str(item_id), tenant_id=tenant_id, reason="ingest")
    finally:
        await maybe_dispatch_webhook(ctx["redis"], job_id)


async def process_doc(ctx: dict, job_id: str, extracted_text: str, doc_metadata: dict | None = None, tenant_id: str = "default", model: str | None = None, **_ignored_future_kwargs) -> None:
    item_id = None
    try:
        async with async_session() as db:
            pipeline = DocPipeline(db, ctx["embedder"], ctx["llm"])
            item_id = await pipeline.process(
                uuid.UUID(job_id),
                extracted_text=extracted_text,
                doc_metadata=doc_metadata or {},
                tenant_id=tenant_id,
                model=model,
            )
        await ctx["redis"].enqueue_job("extract_relationships", item_id=str(item_id), tenant_id=tenant_id)
        await enqueue_palace_job(ctx["redis"], "mark_item_dirty_and_schedule", item_id=str(item_id), tenant_id=tenant_id, reason="ingest")
    finally:
        await maybe_dispatch_webhook(ctx["redis"], job_id)


async def process_image(ctx: dict, job_id: str, description: str = "", image_metadata: dict | None = None, tenant_id: str = "default", **_ignored_future_kwargs) -> None:
    # model override does not apply to image pipeline (uses vision model separately)
    item_id = None
    try:
        async with async_session() as db:
            pipeline = ImagePipeline(db, ctx["embedder"], ctx["llm"])
            item_id = await pipeline.process(
                uuid.UUID(job_id),
                description=description,
                image_metadata=image_metadata or {},
                tenant_id=tenant_id,
            )
        await ctx["redis"].enqueue_job("extract_relationships", item_id=str(item_id), tenant_id=tenant_id)
        await enqueue_palace_job(ctx["redis"], "mark_item_dirty_and_schedule", item_id=str(item_id), tenant_id=tenant_id, reason="ingest")
    except ImageAnalysisError as exc:
        await _record_image_analysis_failure(job_id, exc)
        if exc.retryable:
            raise
        logger.info("process_image marked job %s failed without retry: %s", job_id, exc)
    finally:
        await maybe_dispatch_webhook(ctx["redis"], job_id)


async def process_note(ctx: dict, job_id: str, title: str, content: str, tags: list | None = None, tenant_id: str = "default", model: str | None = None, **_ignored_future_kwargs) -> None:
    item_id = None
    try:
        async with async_session() as db:
            pipeline = NotePipeline(db, ctx["embedder"], ctx["llm"])
            item_id = await pipeline.process(uuid.UUID(job_id), title=title, content=content, tags=tags, tenant_id=tenant_id, model=model)
        await ctx["redis"].enqueue_job("extract_relationships", item_id=str(item_id), tenant_id=tenant_id)
        await enqueue_palace_job(ctx["redis"], "mark_item_dirty_and_schedule", item_id=str(item_id), tenant_id=tenant_id, reason="ingest")
    finally:
        await maybe_dispatch_webhook(ctx["redis"], job_id)


async def extract_relationships(ctx: dict, item_id: str, tenant_id: str = "default") -> None:
    from app.services.relationships import RelationshipService
    async with async_session() as db:
        service = RelationshipService(db, ctx["embedder"], ctx["llm"])
        await service.find_relationships(uuid.UUID(item_id), tenant_id=tenant_id)


async def backfill_deferred_relationships(
    ctx: dict,
    tenant_id: str = "default",
    limit: int = _RELATIONSHIP_BACKFILL_DEFAULT_LIMIT,
    defer_seconds: int = _RELATIONSHIP_BACKFILL_DEFAULT_DEFER_SECONDS,
) -> int:
    try:
        limit = max(1, min(int(limit), _RELATIONSHIP_BACKFILL_MAX_LIMIT))
        defer_seconds = max(0, min(int(defer_seconds), _RELATIONSHIP_BACKFILL_MAX_DEFER_SECONDS))
    except (TypeError, ValueError):
        logger.warning(
            "backfill_deferred_relationships: invalid limits for tenant %s; using defaults",
            tenant_id,
        )
        limit = _RELATIONSHIP_BACKFILL_DEFAULT_LIMIT
        defer_seconds = _RELATIONSHIP_BACKFILL_DEFAULT_DEFER_SECONDS

    async with async_session() as db:
        lock_acquired = (
            await db.execute(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": f"relationship-backfill:{tenant_id}"},
            )
        ).scalar_one()
        if not lock_acquired:
            logger.info(
                "backfill_deferred_relationships: skipped duplicate active lease for tenant %s",
                tenant_id,
            )
            return 0

        rows = (
            await db.execute(
                text("""
                    SELECT i.id
                    FROM items i
                    WHERE i.tenant_id = :tenant_id
                      AND i.status = 'ready'
                      AND i.deleted_at IS NULL
                      AND i.summary IS NOT NULL
                      AND i.metadata ? 'memory_entry'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM item_relationships r
                          WHERE r.source_item_id = i.id
                             OR r.target_item_id = i.id
                      )
                    ORDER BY i.updated_at ASC, i.id ASC
                    LIMIT :limit
                """),
                {"tenant_id": tenant_id, "limit": limit},
            )
        ).fetchall()

    for index, row in enumerate(rows):
        enqueue_kwargs = {
            "item_id": str(row.id),
            "tenant_id": tenant_id,
        }
        defer_by = index * defer_seconds
        if defer_by > 0:
            enqueue_kwargs["_defer_by"] = defer_by
        await ctx["redis"].enqueue_job("extract_relationships", **enqueue_kwargs)

    logger.info(
        "backfill_deferred_relationships: queued %d relationship job(s) for tenant %s",
        len(rows),
        tenant_id,
    )
    return len(rows)


async def backfill_missing_taxonomy(
    ctx: dict,
    tenant_id: str = "default",
    limit: int = _TAXONOMY_BACKFILL_DEFAULT_LIMIT,
    dry_run: bool = True,
    source_types: tuple[str, ...] | list[str] | None = None,
) -> dict:
    """Populate missing tags/categories from existing raw_content only."""
    try:
        limit = max(1, min(int(limit), _TAXONOMY_BACKFILL_MAX_LIMIT))
    except (TypeError, ValueError):
        logger.warning(
            "backfill_missing_taxonomy: invalid limit for tenant %s; using default",
            tenant_id,
        )
        limit = _TAXONOMY_BACKFILL_DEFAULT_LIMIT

    requested_source_types = tuple(
        source_type.strip()
        for source_type in (source_types or _TAXONOMY_BACKFILL_SOURCE_TYPES)
        if isinstance(source_type, str) and source_type.strip()
    )
    if not requested_source_types:
        requested_source_types = _TAXONOMY_BACKFILL_SOURCE_TYPES

    report: dict[str, object] = {
        "tenant_id": tenant_id,
        "dry_run": bool(dry_run),
        "source_types": list(requested_source_types),
        "candidate_count": 0,
        "changed_count": 0,
        "skipped_count": 0,
        "failure_count": 0,
        "candidate_breakdown": {"source_type": {}, "source_type_job_type": {}},
        "changed_breakdown": {"source_type": {}, "source_type_job_type": {}},
        "samples": [],
        "failures": [],
    }
    changed_item_ids: list[str] = []

    async with async_session() as db:
        candidate_filter = _empty_taxonomy_condition_sql()
        rows = (
            await db.execute(
                text(f"""
                    SELECT i.id, i.title, i.source_type, COALESCE(j.job_type, 'unknown') AS job_type
                    FROM items i
                    LEFT JOIN LATERAL (
                        SELECT jobs.job_type
                        FROM jobs
                        WHERE jobs.item_id = i.id
                          AND jobs.tenant_id = i.tenant_id
                        ORDER BY jobs.created_at DESC, jobs.id DESC
                        LIMIT 1
                    ) j ON TRUE
                    WHERE i.tenant_id = :tenant_id
                      AND i.status = 'ready'
                      AND i.deleted_at IS NULL
                      AND i.source_type = ANY(CAST(:source_types AS text[]))
                      AND i.raw_content IS NOT NULL
                      AND length(i.raw_content) > 0
                      AND {candidate_filter}
                    ORDER BY i.updated_at ASC, i.id ASC
                    LIMIT :limit
                """),
                {
                    "tenant_id": tenant_id,
                    "source_types": list(requested_source_types),
                    "limit": limit,
                },
            )
        ).fetchall()
        report["candidate_count"] = len(rows)
        for row in rows:
            _increment_nested_count(report["candidate_breakdown"], "source_type", row.source_type)  # type: ignore[arg-type]
            _increment_nested_count(
                report["candidate_breakdown"],  # type: ignore[arg-type]
                "source_type_job_type",
                row.source_type,
                row.job_type,
            )

        vocab_result = await db.execute(
            text(
                "SELECT DISTINCT unnest(tags) AS tag FROM items"
                " WHERE status='ready' AND tenant_id=:tenant_id AND cardinality(tags) > 0"
            ),
            {"tenant_id": tenant_id},
        )
        existing_tags = [row.tag for row in vocab_result]

        for row in rows:
            item_id = row.id
            title = row.title
            source_type = getattr(row, "source_type", None)
            job_type = getattr(row, "job_type", None)
            sample = {"item_id": str(item_id), "title": title}
            try:
                item = await db.get(Item, item_id)
                if item is None or item.tenant_id != tenant_id or item.status != "ready" or not item.raw_content:
                    report["skipped_count"] = int(report["skipped_count"]) + 1
                    continue

                generated_tags, generated_categories = await ctx["llm"].generate_tags(
                    item.raw_content[:4000],
                    existing_tags=existing_tags,
                )
                merged_tags = item.tags or stable_merge_tags(generated_tags)
                merged_categories = item.categories or stable_merge_tags(generated_categories)
                would_change = merged_tags != (item.tags or []) or merged_categories != (item.categories or [])
                samples = report["samples"]
                assert isinstance(samples, list)
                if len(samples) < 10:
                    samples.append(sample)
                if not would_change:
                    report["skipped_count"] = int(report["skipped_count"]) + 1
                    continue
                report["changed_count"] = int(report["changed_count"]) + 1
                _increment_nested_count(report["changed_breakdown"], "source_type", source_type)  # type: ignore[arg-type]
                _increment_nested_count(
                    report["changed_breakdown"],  # type: ignore[arg-type]
                    "source_type_job_type",
                    source_type,
                    job_type,
                )
                if dry_run:
                    continue

                previous_tag_count = len(item.tags or [])
                previous_category_count = len(item.categories or [])
                item.tags = merged_tags
                item.categories = merged_categories
                item.metadata_ = {
                    **(item.metadata_ or {}),
                    "taxonomy_backfill": {
                        "source": "backfill_missing_taxonomy",
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                        "raw_content_chars_used": min(len(item.raw_content), 4000),
                        "previous_tag_count": previous_tag_count,
                        "previous_category_count": previous_category_count,
                    },
                }
                changed_item_ids.append(str(item.id))
            except Exception as exc:
                report["failure_count"] = int(report["failure_count"]) + 1
                failures = report["failures"]
                assert isinstance(failures, list)
                failures.append(
                    {
                        "item_id": str(item_id),
                        "title": title,
                        **_taxonomy_backfill_error_payload(exc),
                    }
                )

        if not dry_run and changed_item_ids:
            await db.commit()

    if not dry_run and changed_item_ids:
        await enqueue_palace_job(
            ctx["redis"],
            "mark_items_dirty_and_schedule",
            item_ids=changed_item_ids,
            tenant_id=tenant_id,
            reason="taxonomy-backfill",
        )

    logger.info("backfill_missing_taxonomy: %s", report)
    return report


async def embed_item(ctx: dict, item_id: str, skip_ai_enrichment: bool = False, tenant_id: str = "default") -> None:
    """Chunk, embed, and optionally AI-enrich an item created via POST /items."""
    async with async_session() as db:
        item = await db.get(Item, uuid.UUID(item_id))
        if not item or not item.raw_content:
            return
        await db.execute(delete(Embedding).where(Embedding.item_id == item.id))
        item.content_chunks = None
        item.content_hash = None
        item.status = "processing"
        result = await process_prebuilt_item(
            db,
            item=item,
            embedder=ctx["embedder"],
            llm=ctx["llm"],
            tenant_id=tenant_id,
            enable_ai_enrichment=not skip_ai_enrichment,
        )
    if result.status == "completed":
        await ctx["redis"].enqueue_job("extract_relationships", item_id=item_id, tenant_id=tenant_id)
    await enqueue_palace_job(ctx["redis"], "mark_item_dirty_and_schedule", item_id=item_id, tenant_id=tenant_id, reason="ingest")


async def memory_artifact(ctx: dict, job_id: str, **_ignored_future_kwargs) -> None:
    try:
        async with async_session() as db:
            job = await db.get(Job, uuid.UUID(job_id))
            if not job:
                raise ValueError(f"Job {job_id} not found")
            if job.job_type != MEMORY_JOB_TYPE:
                raise ValueError(f"Job {job_id} is not a memory artifact job")
            if not job.item_id:
                raise ValueError(f"Job {job_id} has no item_id")

            item = await db.get(Item, job.item_id)
            if not item:
                raise ValueError(f"Item {job.item_id} not found for job {job_id}")

            result = await process_prebuilt_item(
                db,
                item=item,
                embedder=ctx["embedder"],
                llm=ctx["llm"],
                tenant_id=job.tenant_id,
                job=job,
                enable_ai_enrichment=bool((job.payload or {}).get("enable_ai_enrichment", False)),
            )
        if result.status == "completed":
            relationship_policy = _relationship_policy_from_job(job)
            if relationship_policy == "immediate":
                await ctx["redis"].enqueue_job(
                    "extract_relationships",
                    item_id=str(result.item_id),
                    tenant_id=job.tenant_id,
                )
            else:
                logger.info(
                    "memory_artifact: relationship extraction %s for job %s item %s",
                    relationship_policy,
                    job.id,
                    result.item_id,
                )
            await enqueue_palace_job(
                ctx["redis"],
                "mark_item_dirty_and_schedule",
                item_id=str(result.item_id),
                tenant_id=job.tenant_id,
                reason="memory-write",
            )
    finally:
        await maybe_dispatch_webhook(ctx["redis"], job_id)


async def recover_stale_memory_jobs(ctx: dict) -> None:
    """Re-enqueue durable memory jobs orphaned by worker restarts."""
    async with async_session() as db:
        result = await db.execute(
            text(
                f"""
                SELECT j.id, j.status, j.item_id, j.tenant_id
                FROM jobs j
                WHERE (
                    (j.status = 'processing' AND j.created_at < NOW() - INTERVAL '{STALE_MEMORY_PROCESSING_MINUTES} minutes')
                    OR
                    (j.status = 'queued' AND j.created_at < NOW() - INTERVAL '{STALE_MEMORY_QUEUED_MINUTES} minutes')
                )
                AND j.job_type = :job_type
                """
            ),
            {"job_type": MEMORY_JOB_TYPE},
        )
        stale_rows = result.fetchall()

    if not stale_rows:
        return

    logger.warning("recover_stale_memory_jobs: found %d stale memory job(s)", len(stale_rows))

    for row in stale_rows:
        job_id = str(row.id)
        async with async_session() as db:
            job = await db.get(Job, row.id)
            if job is None:
                continue
            item = await db.get(Item, job.item_id) if job.item_id else None
            if item is None or not item.raw_content:
                job.status = "failed"
                job.error_message = "Stale memory job lost its source note content and cannot be retried"
                job.completed_at = datetime.now(timezone.utc)
                await db.commit()
                await maybe_dispatch_webhook(ctx["redis"], job_id)
                continue

            job.status = "queued"
            job.progress = 0
            job.error_message = None
            job.completed_at = None
            item.status = "processing"
            await db.commit()

        await ctx["redis"].enqueue_job("memory_artifact", job_id=job_id)
        logger.info("recover_stale_memory_jobs: requeued job %s for tenant %s", job_id, row.tenant_id)


async def restore_bundle(ctx: dict, job_id: str, **_ignored_future_kwargs) -> None:
    async with async_session() as db:
        await run_restore_job(db, ctx["embedder"], uuid.UUID(job_id))
