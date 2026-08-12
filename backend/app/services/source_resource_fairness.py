"""Process-local per-origin fairness for bounded source refresh workers.

The dispatcher lease prevents duplicate resource jobs across workers; this gate
adds a conservative local concurrency and start-rate bound for a single worker
process.  It is intentionally not a replacement for the durable scheduler.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass
class _OriginGate:
    semaphore: asyncio.Semaphore
    users: int = 0


class HostFairness:
    def __init__(
        self,
        *,
        max_concurrency: int = 2,
        minimum_interval_seconds: float = 0.5,
        max_origins: int = 256,
    ) -> None:
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        if minimum_interval_seconds < 0:
            raise ValueError("minimum_interval_seconds must not be negative")
        if max_origins <= 0:
            raise ValueError("max_origins must be positive")
        self._max_concurrency = max_concurrency
        self._minimum_interval_seconds = minimum_interval_seconds
        self._max_origins = max_origins
        self._gates: OrderedDict[str, _OriginGate] = OrderedDict()
        self._next_start: dict[str, float] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def origin(url: str) -> str:
        parsed = urlsplit(url)
        return f"{parsed.scheme}://{parsed.netloc}".lower()

    @asynccontextmanager
    async def acquire(self, url: str):
        origin = self.origin(url)
        async with self._lock:
            gate = self._gates.get(origin)
            if gate is None:
                gate = _OriginGate(asyncio.Semaphore(self._max_concurrency))
                self._gates[origin] = gate
            gate.users += 1
            self._gates.move_to_end(origin)
            self._evict_idle_origins()
        try:
            async with gate.semaphore:
                async with self._lock:
                    now = time.monotonic()
                    start_at = max(now, self._next_start.get(origin, now))
                    self._next_start[origin] = start_at + self._minimum_interval_seconds
                if (delay := start_at - time.monotonic()) > 0:
                    await asyncio.sleep(delay)
                yield
        finally:
            async with self._lock:
                gate.users -= 1
                self._evict_idle_origins()

    def _evict_idle_origins(self) -> None:
        if len(self._gates) <= self._max_origins:
            return
        for candidate, gate in tuple(self._gates.items()):
            if len(self._gates) <= self._max_origins:
                break
            if gate.users == 0:
                self._gates.pop(candidate, None)
                self._next_start.pop(candidate, None)
