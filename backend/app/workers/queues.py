import hashlib
import os
import re

DEFAULT_WORKER_QUEUE = "arq:queue"
MEDIA_WORKER_QUEUE = "arq:queue:media"
PALACE_WORKER_QUEUE = "arq:queue:palace"
WORKER_HEALTH_CHECK_INTERVAL_SECONDS = 15
WORKER_HEALTH_CHECK_TTL_SECONDS = WORKER_HEALTH_CHECK_INTERVAL_SECONDS + 1

MEDIA_TASK_NAMES = frozenset({"process_media", "process_youtube"})
MEDIA_FAIR_DISPATCH_TASK_NAME = "dispatch_tenant_fair_media_jobs"


def worker_health_check_key(queue_name: str, instance_name: str | None = None) -> str:
    """Return a pod-specific ARQ health key so sibling workers cannot satisfy readiness."""
    raw_instance = instance_name if instance_name is not None else os.getenv("HOSTNAME", "local")
    safe_instance = re.sub(r"[^a-zA-Z0-9_.-]+", "-", raw_instance.strip()).strip("-") or "local"
    return f"{queue_name}:health-check:{safe_instance[:128]}"


def queue_kwargs_for_task(name: str) -> dict[str, str]:
    if name in MEDIA_TASK_NAMES:
        return {"_queue_name": MEDIA_WORKER_QUEUE}
    return {}


async def enqueue_worker_job(arq_pool, name: str, **kwargs):
    if name in MEDIA_TASK_NAMES:
        job_id = singleton_job_id(MEDIA_FAIR_DISPATCH_TASK_NAME, "media")
        return await arq_pool.enqueue_job(
            MEDIA_FAIR_DISPATCH_TASK_NAME,
            _queue_name=DEFAULT_WORKER_QUEUE,
            _job_id=job_id,
        )
    return await arq_pool.enqueue_job(name, **kwargs, **queue_kwargs_for_task(name))


async def enqueue_default_job(arq_pool, name: str, **kwargs):
    """Force follow-on enrichment back to the default worker queue."""
    return await arq_pool.enqueue_job(name, _queue_name=DEFAULT_WORKER_QUEUE, **kwargs)


async def enqueue_media_job(arq_pool, name: str, **kwargs):
    return await arq_pool.enqueue_job(name, _queue_name=MEDIA_WORKER_QUEUE, **kwargs)


async def enqueue_palace_job(arq_pool, name: str, **kwargs):
    """Route Palace freshness work away from the default enrichment queue."""
    return await arq_pool.enqueue_job(name, _queue_name=PALACE_WORKER_QUEUE, **kwargs)


def singleton_job_id(name: str, *parts: object) -> str:
    raw = ":".join(str(part) for part in (name, *parts))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    readable = re.sub(r"[^a-zA-Z0-9_-]+", "-", raw).strip("-").lower()
    return f"singleton:{readable[:80]}:{digest}"


async def enqueue_singleton_job(arq_pool, name: str, *parts: object, **kwargs):
    job_id = singleton_job_id(name, *parts)
    job = await arq_pool.enqueue_job(name, _job_id=job_id, **kwargs)
    return job, job_id
