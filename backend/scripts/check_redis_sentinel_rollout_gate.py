from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from redis.asyncio import Redis
from redis.asyncio.sentinel import Sentinel

from scripts.wait_for_redis_sentinel import load_config_from_env


logger = logging.getLogger("palaceoftruth.redis_sentinel_rollout_gate")


@dataclass(frozen=True)
class RolloutGateResult:
    master_host: str
    master_port: int
    connected_replicas: int
    queue_key: str


def _expected_replicas() -> int:
    raw_value = os.getenv("REDIS_SENTINEL_EXPECTED_REPLICAS", "1").strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError("REDIS_SENTINEL_EXPECTED_REPLICAS must be a non-negative integer") from exc
    if value < 0:
        raise ValueError("REDIS_SENTINEL_EXPECTED_REPLICAS must be a non-negative integer")
    return value


async def check_rollout_gate(*, sentinel_factory=Sentinel, redis_factory=Redis) -> RolloutGateResult:
    config = load_config_from_env()
    if config is None:
        raise RuntimeError("REDIS_SENTINEL_HOSTS must be set for the rollout gate")

    sentinel = sentinel_factory(config.hosts, socket_timeout=2.0, socket_connect_timeout=2.0)
    redis = None
    queue_key = f"palaceoftruth:rollout-gate:{os.getpid()}"
    payload = b"sentinel-ready"
    try:
        master_host, master_port = await sentinel.discover_master(config.master_name)
        redis = redis_factory(host=master_host, port=master_port, socket_timeout=2.0, socket_connect_timeout=2.0)
        await redis.ping()
        replication_info = await redis.info("replication")
        role = str(replication_info.get("role", "")).lower()
        if role != "master":
            raise RuntimeError(f"Sentinel master {config.master_name!r} resolved to role {role!r}")

        connected_replicas = int(replication_info.get("connected_slaves", 0))
        expected_replicas = _expected_replicas()
        if connected_replicas < expected_replicas:
            raise RuntimeError(
                f"Redis master has {connected_replicas} connected replicas; expected at least {expected_replicas}"
            )

        await redis.lpush(queue_key, payload)
        dequeued = await redis.rpop(queue_key)
        if dequeued != payload:
            raise RuntimeError("Redis enqueue/dequeue smoke returned unexpected payload")
    finally:
        if redis is not None:
            await redis.delete(queue_key)
            await redis.aclose()
        sentinel_close = getattr(sentinel, "aclose", None)
        if sentinel_close is not None:
            await sentinel_close()

    return RolloutGateResult(
        master_host=str(master_host),
        master_port=int(master_port),
        connected_replicas=connected_replicas,
        queue_key=queue_key,
    )


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    try:
        result = asyncio.run(check_rollout_gate())
    except Exception:
        logger.exception("Redis Sentinel rollout gate failed")
        return 1
    logger.info(
        "Redis Sentinel rollout gate passed: master=%s:%s connected_replicas=%s queue_key=%s",
        result.master_host,
        result.master_port,
        result.connected_replicas,
        result.queue_key,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
