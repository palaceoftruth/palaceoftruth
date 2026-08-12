import asyncio
import hashlib
import os
import re
import secrets

DEFAULT_WORKER_QUEUE = "arq:queue"
MEDIA_WORKER_QUEUE = "arq:queue:media"
PALACE_WORKER_QUEUE = "arq:queue:palace"
WORKER_HEALTH_CHECK_INTERVAL_SECONDS = 15
WORKER_HEALTH_CHECK_TTL_SECONDS = WORKER_HEALTH_CHECK_INTERVAL_SECONDS + 1

MEDIA_TASK_NAMES = frozenset({"process_media", "process_youtube"})
MEDIA_FAIR_DISPATCH_TASK_NAME = "dispatch_tenant_fair_media_jobs"
TENANT_QUEUE_LOCK_TTL_MS = 60_000
TENANT_QUEUE_LOCK_WAIT_SECONDS = 30


class TenantQueueClosedError(RuntimeError):
    """Raised when durable erasure has closed a tenant's queue boundary."""


def _supports_tenant_queue_barrier(arq_pool) -> bool:
    """Return false only for narrow test and integration queue facades."""
    return all(
        callable(getattr(arq_pool, method, None))
        for method in ("set", "exists", "delete", "eval")
    )


def _tenant_queue_key(tenant_id: str, suffix: str) -> str:
    digest = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()
    return f"palace:tenant-queue:{digest}:{suffix}"


async def _acquire_tenant_queue_lock(arq_pool, tenant_id: str) -> tuple[str, str]:
    lock_key = _tenant_queue_key(tenant_id, "lock")
    token = secrets.token_urlsafe(24)
    deadline = asyncio.get_running_loop().time() + TENANT_QUEUE_LOCK_WAIT_SECONDS
    while True:
        acquired = await arq_pool.set(
            lock_key,
            token,
            nx=True,
            px=TENANT_QUEUE_LOCK_TTL_MS,
        )
        if acquired:
            return lock_key, token
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"Timed out waiting for tenant queue barrier: {tenant_id}")
        await asyncio.sleep(0.05)


async def _release_tenant_queue_lock(arq_pool, lock_key: str, token: str) -> None:
    await arq_pool.eval(
        "if redis.call('get', KEYS[1]) == ARGV[1] then "
        "return redis.call('del', KEYS[1]) else return 0 end",
        1,
        lock_key,
        token,
    )


async def close_tenant_queue(arq_pool, tenant_id: str) -> None:
    """Wait for active enqueue work, then retain a durable erasure marker."""
    if not _supports_tenant_queue_barrier(arq_pool):
        return
    lock_key, token = await _acquire_tenant_queue_lock(arq_pool, tenant_id)
    try:
        await arq_pool.set(_tenant_queue_key(tenant_id, "closed"), "erased")
    finally:
        await _release_tenant_queue_lock(arq_pool, lock_key, token)


async def reopen_tenant_queue(arq_pool, tenant_id: str) -> None:
    """Remove a provisional barrier when database erasure rolls back."""
    if not _supports_tenant_queue_barrier(arq_pool):
        return
    lock_key, token = await _acquire_tenant_queue_lock(arq_pool, tenant_id)
    try:
        await arq_pool.delete(_tenant_queue_key(tenant_id, "closed"))
    finally:
        await _release_tenant_queue_lock(arq_pool, lock_key, token)


async def enqueue_tenant_job(arq_pool, name: str, *, tenant_id: str, **kwargs):
    """Atomically exclude enqueues that race with permanent tenant erasure."""
    if not _supports_tenant_queue_barrier(arq_pool):
        return await arq_pool.enqueue_job(name, tenant_id=tenant_id, **kwargs)
    lock_key, token = await _acquire_tenant_queue_lock(arq_pool, tenant_id)
    try:
        if await arq_pool.exists(_tenant_queue_key(tenant_id, "closed")):
            raise TenantQueueClosedError(f"Tenant queue is closed: {tenant_id}")
        async with asyncio.timeout(TENANT_QUEUE_LOCK_WAIT_SECONDS):
            return await arq_pool.enqueue_job(name, tenant_id=tenant_id, **kwargs)
    finally:
        await _release_tenant_queue_lock(arq_pool, lock_key, token)


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
    tenant_id = kwargs.get("tenant_id")
    if name in MEDIA_TASK_NAMES:
        job_id = singleton_job_id(MEDIA_FAIR_DISPATCH_TASK_NAME, "media")
        return await arq_pool.enqueue_job(
            MEDIA_FAIR_DISPATCH_TASK_NAME,
            _queue_name=DEFAULT_WORKER_QUEUE,
            _job_id=job_id,
        )
    if isinstance(tenant_id, str):
        kwargs.pop("tenant_id")
        return await enqueue_tenant_job(
            arq_pool,
            name,
            tenant_id=tenant_id,
            **kwargs,
            **queue_kwargs_for_task(name),
        )
    return await arq_pool.enqueue_job(name, **kwargs, **queue_kwargs_for_task(name))


async def enqueue_default_job(arq_pool, name: str, **kwargs):
    """Force follow-on enrichment back to the default worker queue."""
    tenant_id = kwargs.pop("tenant_id", None)
    if isinstance(tenant_id, str):
        return await enqueue_tenant_job(
            arq_pool,
            name,
            tenant_id=tenant_id,
            _queue_name=DEFAULT_WORKER_QUEUE,
            **kwargs,
        )
    return await arq_pool.enqueue_job(name, _queue_name=DEFAULT_WORKER_QUEUE, **kwargs)


async def enqueue_media_job(arq_pool, name: str, **kwargs):
    tenant_id = kwargs.pop("tenant_id", None)
    if isinstance(tenant_id, str):
        return await enqueue_tenant_job(
            arq_pool,
            name,
            tenant_id=tenant_id,
            _queue_name=MEDIA_WORKER_QUEUE,
            **kwargs,
        )
    return await arq_pool.enqueue_job(name, _queue_name=MEDIA_WORKER_QUEUE, **kwargs)


async def enqueue_palace_job(arq_pool, name: str, **kwargs):
    """Route Palace freshness work away from the default enrichment queue."""
    tenant_id = kwargs.pop("tenant_id", None)
    if isinstance(tenant_id, str):
        return await enqueue_tenant_job(
            arq_pool,
            name,
            tenant_id=tenant_id,
            _queue_name=PALACE_WORKER_QUEUE,
            **kwargs,
        )
    return await arq_pool.enqueue_job(name, _queue_name=PALACE_WORKER_QUEUE, **kwargs)


def singleton_job_id(name: str, *parts: object) -> str:
    raw = ":".join(str(part) for part in (name, *parts))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    readable = re.sub(r"[^a-zA-Z0-9_-]+", "-", raw).strip("-").lower()
    return f"singleton:{readable[:80]}:{digest}"


async def enqueue_singleton_job(arq_pool, name: str, *parts: object, **kwargs):
    job_id = singleton_job_id(name, *parts)
    tenant_id = kwargs.pop("tenant_id", None)
    if isinstance(tenant_id, str):
        job = await enqueue_tenant_job(
            arq_pool,
            name,
            tenant_id=tenant_id,
            _job_id=job_id,
            **kwargs,
        )
        return job, job_id
    job = await arq_pool.enqueue_job(name, _job_id=job_id, **kwargs)
    return job, job_id
