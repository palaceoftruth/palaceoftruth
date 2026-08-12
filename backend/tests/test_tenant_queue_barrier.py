import asyncio

import pytest

from app.workers.queues import (
    TenantQueueClosedError,
    close_tenant_queue,
    enqueue_tenant_job,
    reopen_tenant_queue,
)


class BarrierRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.jobs: list[tuple[str, dict]] = []
        self.enqueue_entered = asyncio.Event()
        self.allow_enqueue = asyncio.Event()

    async def set(self, key, value, *, nx=False, px=None):
        del px
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def exists(self, key):
        return key in self.values

    async def delete(self, key):
        return int(self.values.pop(key, None) is not None)

    async def eval(self, _script, _numkeys, key, token):
        if self.values.get(key) == token:
            return await self.delete(key)
        return 0

    async def enqueue_job(self, name, **kwargs):
        self.enqueue_entered.set()
        await self.allow_enqueue.wait()
        self.jobs.append((name, kwargs))
        return object()


@pytest.mark.asyncio
async def test_tenant_erasure_waits_for_active_enqueue_then_closes_queue() -> None:
    redis = BarrierRedis()
    enqueue = asyncio.create_task(
        enqueue_tenant_job(
            redis,
            "process_note",
            tenant_id="tenant-a",
            job_id="job-a",
        )
    )
    await redis.enqueue_entered.wait()

    close = asyncio.create_task(close_tenant_queue(redis, "tenant-a"))
    await asyncio.sleep(0)
    assert close.done() is False

    redis.allow_enqueue.set()
    await enqueue
    await close

    with pytest.raises(TenantQueueClosedError, match="Tenant queue is closed"):
        await enqueue_tenant_job(
            redis,
            "process_note",
            tenant_id="tenant-a",
            job_id="job-b",
        )
    assert [job[1]["job_id"] for job in redis.jobs] == ["job-a"]

    await reopen_tenant_queue(redis, "tenant-a")
    await enqueue_tenant_job(
        redis,
        "process_note",
        tenant_id="tenant-a",
        job_id="job-c",
    )
    assert [job[1]["job_id"] for job in redis.jobs] == ["job-a", "job-c"]
