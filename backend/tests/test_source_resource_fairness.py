import asyncio

import pytest

from app.services.source_resource_fairness import HostFairness


def test_origin_is_normalized_per_scheme_and_host() -> None:
    fairness = HostFairness()
    assert fairness.origin("HTTPS://Example.TEST:443/a") == "https://example.test:443"


@pytest.mark.asyncio
async def test_same_origin_respects_concurrency_bound() -> None:
    fairness = HostFairness(max_concurrency=1, minimum_interval_seconds=0)
    entered = asyncio.Event()
    release = asyncio.Event()
    second_entered = asyncio.Event()

    async def first() -> None:
        async with fairness.acquire("https://example.test/one"):
            entered.set()
            await release.wait()

    async def second() -> None:
        async with fairness.acquire("https://example.test/two"):
            second_entered.set()

    first_task = asyncio.create_task(first())
    await entered.wait()
    second_task = asyncio.create_task(second())
    await asyncio.sleep(0)
    assert not second_entered.is_set()
    release.set()
    await asyncio.gather(first_task, second_task)
    assert second_entered.is_set()
