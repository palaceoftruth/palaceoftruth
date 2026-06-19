"""ARQ task functions for RSS/Atom feed ingestion."""
import logging
import os
import uuid
from datetime import datetime, timezone

from arq.jobs import Job as ArqJob, JobStatus
from sqlalchemy import text

from app.config import settings
from app.database import async_session
from app.models.feed import Feed
from app.models.item import Item
from app.models.job import Job
from app.pipelines.feed import FeedPipeline
from app.workers.queues import DEFAULT_WORKER_QUEUE
from app.workers.queues import enqueue_palace_job, enqueue_worker_job, queue_kwargs_for_task
from app.utils.job_payloads import load_retry_task_from_payload
from app.utils.webhook import maybe_dispatch_webhook

logger = logging.getLogger(__name__)

_JOB_TYPE_TO_TASK = {
    "media": "process_media",
    "video": "process_media",
    "youtube": "process_media",
    "webpage": "process_webpage",
    "pdf": "process_pdf",
    "doc": "process_doc",
    "image": "process_image",
    "note": "process_note",
}

# Jobs stuck in these states longer than these thresholds are considered orphaned
_STALE_PROCESSING_MINUTES = max(35, settings.media_download_timeout_seconds // 60 + 5)
_STALE_QUEUED_MINUTES = 30       # ARQ dropped the queue on worker restart
_ACTIVE_ARQ_STATUSES = {JobStatus.queued, JobStatus.deferred, JobStatus.in_progress}


async def _stale_job_is_requeueable(redis, job_id: str, *, queue_name: str = DEFAULT_WORKER_QUEUE) -> bool:
    """Return true only when Redis no longer has an active ARQ job for the DB row."""
    try:
        status = await ArqJob(job_id, redis=redis, _queue_name=queue_name).status()
    except Exception as exc:
        logger.warning(
            "requeue_stale_jobs: could not inspect ARQ status for job %s; leaving DB row unchanged: %s",
            job_id,
            exc,
        )
        return False
    if status in _ACTIVE_ARQ_STATUSES:
        logger.info("requeue_stale_jobs: job %s is still %s in ARQ; skipping", job_id, status.value)
        return False
    if status == JobStatus.complete:
        logger.warning("requeue_stale_jobs: job %s has a completed ARQ result but stale DB state; skipping duplicate enqueue", job_id)
        return False
    return True


def _extract_pdf_retry_payload(file_path: str) -> tuple[str, dict]:
    """Rebuild the PDF worker payload from the original temp file."""
    import fitz

    doc = fitz.open(file_path)
    try:
        metadata: dict[str, object] = {
            "page_count": len(doc),
            "file_size_bytes": os.path.getsize(file_path),
        }
        info = doc.metadata or {}
        if info.get("title"):
            metadata["doc_title"] = info["title"]
        if info.get("author"):
            metadata["doc_author"] = info["author"]
            metadata["author"] = info["author"]
        pages = [doc[index].get_text() for index in range(len(doc))]
    finally:
        doc.close()

    extracted_text = "\n\n".join(page for page in pages if page.strip())
    metadata["word_count"] = len(extracted_text.split())
    if not extracted_text.strip():
        raise ValueError("No text could be extracted from stale PDF job")
    return extracted_text, metadata


async def requeue_stale_jobs(ctx: dict) -> None:
    """Detect jobs orphaned by worker restarts and re-enqueue them.

    ARQ does not recover in-flight jobs after a worker crash — they remain
    stuck in 'processing' or 'queued' forever. This cron runs every 15 minutes
    and resets + re-enqueues any job that has been in those states too long.
    """
    async with async_session() as db:
        result = await db.execute(text(f"""
            SELECT j.id, j.job_type, j.status, j.item_id, j.tenant_id, j.payload,
                   i.source_url, i.title, i.raw_content
            FROM jobs j
            LEFT JOIN items i ON j.item_id = i.id
            WHERE (
                (j.status = 'processing' AND j.created_at < NOW() - INTERVAL '{_STALE_PROCESSING_MINUTES} minutes')
                OR
                (j.status = 'queued'     AND j.created_at < NOW() - INTERVAL '{_STALE_QUEUED_MINUTES} minutes')
            )
            AND j.job_type IN ('media', 'video', 'youtube', 'webpage', 'pdf', 'doc', 'image', 'note')
        """))
        stale = result.fetchall()

    if not stale:
        return

    logger.warning("requeue_stale_jobs: found %d stale job(s)", len(stale))

    for row in stale:
        job_id = str(row.id)
        task_name = _JOB_TYPE_TO_TASK.get(row.job_type)
        if not task_name:
            logger.warning("requeue_stale_jobs: unknown job_type %s for job %s", row.job_type, job_id)
            continue
        queue_kwargs = queue_kwargs_for_task(task_name)
        queue_names = [queue_kwargs.get("_queue_name", DEFAULT_WORKER_QUEUE)]
        if DEFAULT_WORKER_QUEUE not in queue_names:
            queue_names.append(DEFAULT_WORKER_QUEUE)
        if not all(
            [
                await _stale_job_is_requeueable(ctx["redis"], job_id, queue_name=queue_name)
                for queue_name in queue_names
            ]
        ):
            continue

        is_url_job = row.job_type in ("media", "video", "youtube", "webpage")
        if is_url_job and not row.source_url:
            logger.warning("requeue_stale_jobs: job %s has no source_url, marking failed", job_id)
            async with async_session() as db:
                job = await db.get(Job, row.id)
                if job:
                    job.status = "failed"
                    job.error_message = "Stale job with no source URL — cannot requeue"
                    job.completed_at = datetime.now(timezone.utc)
                    await db.commit()
            await maybe_dispatch_webhook(ctx["redis"], job_id)
            continue

        restored = load_retry_task_from_payload(
            job_type=row.job_type,
            job_id=row.id,
            tenant_id=row.tenant_id,
            payload=row.payload,
            expected_task_name=task_name,
        )
        if restored is not None:
            task_name, task_kwargs = restored
            if is_url_job:
                task_kwargs["url"] = row.source_url
        else:
            task_kwargs: dict = {"job_id": job_id, "tenant_id": row.tenant_id}

            if is_url_job:
                task_kwargs["url"] = row.source_url

            elif row.job_type == "pdf":
                file_path = f"/tmp/palaceoftruth/{job_id}.pdf"
                if row.raw_content:
                    task_kwargs["extracted_text"] = row.raw_content
                    task_kwargs["pdf_metadata"] = {}
                elif not os.path.exists(file_path):
                    logger.warning("requeue_stale_jobs: PDF file gone for job %s, marking failed", job_id)
                    async with async_session() as db:
                        job = await db.get(Job, row.id)
                        if job:
                            job.status = "failed"
                            job.error_message = "Stale PDF job — source file no longer on disk, please re-upload"
                            job.completed_at = datetime.now(timezone.utc)
                            await db.commit()
                    await maybe_dispatch_webhook(ctx["redis"], job_id)
                    continue
                else:
                    try:
                        extracted_text, pdf_metadata = _extract_pdf_retry_payload(file_path)
                    except Exception as exc:
                        logger.warning("requeue_stale_jobs: PDF retry extraction failed for job %s: %s", job_id, exc)
                        async with async_session() as db:
                            job = await db.get(Job, row.id)
                            if job:
                                job.status = "failed"
                                job.error_message = "Stale PDF job — could not rebuild retry payload from source file"
                                job.completed_at = datetime.now(timezone.utc)
                                await db.commit()
                        await maybe_dispatch_webhook(ctx["redis"], job_id)
                        continue
                    task_kwargs["extracted_text"] = extracted_text
                    task_kwargs["pdf_metadata"] = pdf_metadata

            elif row.job_type in ("doc", "image"):
                # doc/image: temp file is gone after background task; retry from raw_content if available
                if not row.raw_content:
                    logger.warning("requeue_stale_jobs: job %s (%s) has no raw_content, marking failed", job_id, row.job_type)
                    async with async_session() as db:
                        job = await db.get(Job, row.id)
                        if job:
                            job.status = "failed"
                            job.error_message = f"Stale {row.job_type} job — source file no longer on disk, please re-upload"
                            job.completed_at = datetime.now(timezone.utc)
                            await db.commit()
                    await maybe_dispatch_webhook(ctx["redis"], job_id)
                    continue
                if row.job_type == "doc":
                    task_kwargs["extracted_text"] = row.raw_content
                    task_kwargs["doc_metadata"] = {}
                else:
                    task_kwargs["description"] = row.raw_content
                    task_kwargs["image_metadata"] = {}

            elif row.job_type == "note":
                if not row.raw_content:
                    async with async_session() as db:
                        job = await db.get(Job, row.id)
                        if job:
                            job.status = "failed"
                            job.error_message = "Stale note job with no content — re-ingest required"
                            job.completed_at = datetime.now(timezone.utc)
                            await db.commit()
                    await maybe_dispatch_webhook(ctx["redis"], job_id)
                    continue
                task_kwargs["title"] = row.title or ""
                task_kwargs["content"] = row.raw_content

        # Reset job state and re-enqueue
        async with async_session() as db:
            job = await db.get(Job, row.id)
            if job:
                requeued_at = datetime.now(timezone.utc)
                job.status = "queued"
                job.progress = 0
                job.error_message = None
                job.completed_at = None
                job.duplicate_of = None
                job.created_at = requeued_at
                item_id = getattr(job, "item_id", None)
                if item_id:
                    item = await db.get(Item, item_id)
                    if item:
                        item.status = "processing"
                await db.commit()

        if task_name in ("process_media", "process_youtube"):
            await enqueue_worker_job(ctx["redis"], task_name, _job_id=job_id, **task_kwargs)
        else:
            await ctx["redis"].enqueue_job(task_name, _job_id=job_id, **queue_kwargs, **task_kwargs)
        logger.info("requeue_stale_jobs: requeued job %s (%s) → %s", job_id, row.job_type, task_name)


async def poll_all_feeds(ctx: dict) -> None:
    """Cron dispatcher: query feeds due for polling and enqueue poll_feed for each."""
    async with async_session() as db:
        result = await db.execute(text("""
            SELECT id, tenant_id FROM feeds
            WHERE enabled = true
            AND deleted_at IS NULL
            AND (
                last_fetched_at IS NULL
                OR last_fetched_at < NOW() - (poll_interval || ' seconds')::interval
            )
        """))
        feeds_due = [(str(row.id), row.tenant_id) for row in result]

    for feed_id, tenant_id in feeds_due:
        await ctx["redis"].enqueue_job("poll_feed", feed_id=feed_id, tenant_id=tenant_id)

    logger.info("poll_all_feeds: dispatched %d jobs", len(feeds_due))


async def poll_feed(ctx: dict, feed_id: str, tenant_id: str = "default") -> None:
    """Fetch feed XML, parse entries, enqueue per-article jobs."""
    import feedparser

    async with async_session() as db:
        feed = await db.get(Feed, uuid.UUID(feed_id))
        if not feed or not feed.enabled or feed.deleted_at is not None:
            logger.info("poll_feed: skipping feed %s (missing or disabled)", feed_id)
            return

        headers = {}
        if feed.etag:
            headers["If-None-Match"] = feed.etag
        if feed.last_modified:
            headers["If-Modified-Since"] = feed.last_modified

        try:
            parsed = feedparser.parse(feed.url, request_headers=headers)
            status = getattr(parsed, "status", 200)
            if status == 304:
                # Not modified — update timestamp and return
                feed.last_fetched_at = datetime.now(timezone.utc)
                await db.commit()
                logger.info("poll_feed: feed %s not modified (304)", feed_id)
                return
            if status >= 400:
                raise ValueError(f"HTTP {status}")
        except Exception as exc:
            feed.consecutive_failures += 1
            feed.last_error = str(exc)[:500]
            if feed.consecutive_failures >= settings.feed_max_failures:
                feed.enabled = False
                feed.paused_reason = "auto_disabled"
                logger.warning(
                    "poll_feed: feed %s auto-disabled after %d consecutive failures",
                    feed_id,
                    feed.consecutive_failures,
                )
            await db.commit()
            raise

        # Update conditional request headers for next poll
        if parsed.get("etag"):
            feed.etag = parsed.etag
        if parsed.get("modified"):
            feed.last_modified = parsed.modified

        feed.feed_metadata = {
            "feed_title": parsed.feed.get("title"),
            "site_url": parsed.feed.get("link"),
            "description": parsed.feed.get("description") or parsed.feed.get("subtitle"),
        }
        feed.consecutive_failures = 0
        feed.last_error = None
        feed.last_fetched_at = datetime.now(timezone.utc)
        await db.commit()

        # Enqueue per-article jobs (cap at 50 for historical backfill)
        entries = parsed.entries[:50]
        enqueued = 0
        for entry in entries:
            entry_url = entry.get("link") or entry.get("id")
            if not entry_url:
                continue
            await ctx["redis"].enqueue_job(
                "process_feed_item",
                feed_id=feed_id,
                entry_url=entry_url,
                entry_title=entry.get("title", ""),
                entry_summary=entry.get("summary", ""),
                entry_author=entry.get("author"),
                entry_published=entry.get("published"),
                entry_guid=entry.get("id"),
                tenant_id=tenant_id,
            )
            enqueued += 1

        logger.info("poll_feed: feed %s — enqueued %d article jobs", feed_id, enqueued)


async def process_feed_item(
    ctx: dict,
    feed_id: str,
    entry_url: str,
    entry_title: str = "",
    entry_summary: str = "",
    entry_author: str | None = None,
    entry_published: str | None = None,
    entry_guid: str | None = None,
    tenant_id: str = "default",
) -> None:
    """Run FeedPipeline for one article; enqueue relationship extraction on success."""
    async with async_session() as db:
        feed = await db.get(Feed, uuid.UUID(feed_id))
        if not feed:
            logger.warning("process_feed_item: feed %s not found", feed_id)
            return

        pipeline = FeedPipeline(db, ctx["embedder"], ctx["llm"])
        item_id = await pipeline.process_entry(
            feed=feed,
            entry_url=entry_url,
            entry_title=entry_title,
            entry_summary=entry_summary,
            entry_author=entry_author,
            entry_published=entry_published,
            entry_guid=entry_guid,
            tenant_id=tenant_id,
        )

    if item_id:
        await ctx["redis"].enqueue_job("extract_relationships", item_id=str(item_id), tenant_id=tenant_id)
        await enqueue_palace_job(ctx["redis"], "mark_item_dirty_and_schedule", item_id=str(item_id), tenant_id=tenant_id, reason="ingest")
        logger.info("process_feed_item: item %s ready, relationships enqueued", item_id)
