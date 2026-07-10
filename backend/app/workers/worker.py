import asyncio
import logging
import os
from contextlib import suppress

from arq import cron

from app.config import make_redis_settings, settings
from app.services.embedder import EmbeddingService
from app.services.llm import LLMService
from app.workers.tasks import process_media, process_youtube, process_webpage, process_pdf, process_doc, process_image, process_note, extract_relationships, backfill_deferred_relationships, backfill_missing_taxonomy, embed_item, memory_artifact, recover_stale_memory_jobs, restore_bundle
from app.workers.feed_tasks import poll_all_feeds, poll_feed, process_feed_item, requeue_stale_jobs
from app.workers.media_fairness import dispatch_tenant_fair_media_jobs
from app.workers.source_subscription_tasks import poll_all_source_subscriptions, poll_source_subscription_task, queue_discovered_source_subscription_entries, diagnose_stale_queued_source_subscription_entries_task
from app.workers.source_resource_tasks import dispatch_due_source_resources, refresh_source_resource
from app.workers.palace_tasks import palace_run_build, run_sync_source, poll_sync_sources, recover_palace_backlog, refresh_dirty_palace_rooms, run_palace_maintenance, repair_palace_artifacts, recompute_palace_tunnel_strengths, refresh_caught_up_wakeup_briefs, run_diary_rollup_maintenance, run_fact_registry_extraction, run_fact_registry_contradiction_sweep, run_wakeup_story_refresh, run_memory_dream_refresh, sweep_palace_index_integrity, mark_item_dirty_and_schedule, mark_items_dirty_and_schedule, watch_local_sync_sources
from app.workers.queues import DEFAULT_WORKER_QUEUE, MEDIA_WORKER_QUEUE, PALACE_WORKER_QUEUE
from app.workers.webhook_tasks import deliver_webhook

logger = logging.getLogger(__name__)


def _env_string(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    if not value:
        raise ValueError(f"{name} must not be blank")
    return value


def _positive_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


async def startup(ctx: dict) -> None:
    """Initialize shared services for all tasks."""
    ctx["embedder"] = EmbeddingService()
    ctx["llm"] = LLMService()
    logger.info("ARQ worker started — embedder and LLM ready")


async def shutdown(ctx: dict) -> None:
    """Cleanup on worker shutdown."""
    logger.info("ARQ worker shutting down")


async def palace_startup(ctx: dict) -> None:
    """Initialize Palace worker services and optional local sync watcher."""
    await startup(ctx)
    if settings.palace_sync_watcher_enabled:
        ctx["palace_sync_watcher_task"] = asyncio.create_task(watch_local_sync_sources(ctx))


async def palace_shutdown(ctx: dict) -> None:
    """Stop optional Palace worker background tasks before shared cleanup."""
    watcher_task = ctx.pop("palace_sync_watcher_task", None)
    if watcher_task is not None:
        watcher_task.cancel()
        with suppress(asyncio.CancelledError):
            await watcher_task
    await shutdown(ctx)


class WorkerSettings:
    queue_name = DEFAULT_WORKER_QUEUE
    functions = [
        process_webpage, process_pdf, process_doc, process_image, process_note,
        extract_relationships,
        backfill_deferred_relationships,
        backfill_missing_taxonomy,
        embed_item,
        memory_artifact,
        recover_stale_memory_jobs,
        restore_bundle,
        poll_feed, process_feed_item,
        poll_source_subscription_task, queue_discovered_source_subscription_entries, diagnose_stale_queued_source_subscription_entries_task,
        refresh_source_resource,
        dispatch_tenant_fair_media_jobs,
        requeue_stale_jobs,
        deliver_webhook,
    ]
    cron_jobs = [
        cron(poll_all_feeds, minute={0, 15, 30, 45}),
        cron(poll_all_source_subscriptions, minute={3, 18, 33, 48}),
        cron(queue_discovered_source_subscription_entries, minute={8, 23, 38, 53}),
        cron(diagnose_stale_queued_source_subscription_entries_task, minute={13, 28, 43, 58}),
        cron(dispatch_due_source_resources, minute={16, 31, 46}),
        cron(dispatch_tenant_fair_media_jobs),
        cron(requeue_stale_jobs, minute={5, 20, 35, 50}),  # offset from feed polls
        cron(recover_stale_memory_jobs, minute={7, 22, 37, 52}),  # offset from feed + sync recovery
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = make_redis_settings()
    job_timeout = 1800  # 30 min — long YouTube videos can take >5 min to download + transcribe


class MediaWorkerSettings:
    queue_name = _env_string("ARQ_QUEUE_NAME", MEDIA_WORKER_QUEUE)
    functions = [
        process_media,
        process_youtube,
    ]
    cron_jobs = []
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = make_redis_settings()
    job_timeout = 1800  # 30 min — long YouTube videos can take >5 min to download + transcribe
    max_jobs = _positive_int_env("ARQ_MAX_JOBS", 1)


class PalaceWorkerSettings:
    queue_name = PALACE_WORKER_QUEUE
    functions = [
        palace_run_build,
        run_sync_source,
        poll_sync_sources,
        refresh_dirty_palace_rooms,
        recover_palace_backlog,
        run_palace_maintenance,
        repair_palace_artifacts,
        recompute_palace_tunnel_strengths,
        refresh_caught_up_wakeup_briefs,
        run_diary_rollup_maintenance,
        run_fact_registry_extraction,
        run_fact_registry_contradiction_sweep,
        run_wakeup_story_refresh,
        run_memory_dream_refresh,
        sweep_palace_index_integrity,
        mark_item_dirty_and_schedule,
        mark_items_dirty_and_schedule,
    ]
    cron_jobs = [
        cron(poll_sync_sources),
        cron(run_palace_maintenance, minute={12, 27, 42, 57}),
        cron(run_diary_rollup_maintenance, hour={0}, minute={23}),
        cron(run_fact_registry_extraction, hour={1}, minute={11}),
        cron(run_fact_registry_contradiction_sweep, hour={1}, minute={23}),
        cron(run_wakeup_story_refresh, hour={1}, minute={37}),
        cron(run_memory_dream_refresh, hour={1}, minute={49}),
        cron(sweep_palace_index_integrity, hour={3}, minute={17}),
    ]
    on_startup = palace_startup
    on_shutdown = palace_shutdown
    redis_settings = make_redis_settings()
    job_timeout = 1800  # 30 min — long YouTube videos can take >5 min to download + transcribe
