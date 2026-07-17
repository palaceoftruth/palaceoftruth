"""Process-local per-origin fairness for bounded source refresh workers.

The dispatcher lease prevents duplicate resource jobs across workers; this gate
adds a conservative local concurrency and start-rate bound for a single worker
process.  It is intentionally not a replacement for the durable scheduler.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from urllib.parse import urlsplit


class HostFairness:
    def __init__(self, *, max_concurrency: int = 2, minimum_interval_seconds: float = 0.5) -> None:
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        if minimum_interval_seconds < 0:
            raise ValueError("minimum_interval_seconds must not be negative")
        self._max_concurrency = max_concurrency
        self._minimum_interval_seconds = minimum_interval_seconds
        self._semaphores: dict[str, asyncio.Semaphore] = defaultdict(lambda: asyncio.Semaphore(max_concurrency))
        self._next_start: dict[str, float] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def origin(url: str) -> str:
        parsed = urlsplit(url)
        return f"{parsed.scheme}://{parsed.netloc}".lower()

    @asynccontextmanager
    async def acquire(self, url: str):
        origin = self.origin(url)
        async with self._semaphores[origin]:
            async with self._lock:
                now = time.monotonic()
                start_at = max(now, self._next_start.get(origin, now))
                self._next_start[origin] = start_at + self._minimum_interval_seconds
            if delay := start_at - time.monotonic():
                await asyncio.sleep(delay)
            yield
