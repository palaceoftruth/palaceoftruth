from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.asyncio.sentinel import Sentinel


logger = logging.getLogger("palaceoftruth.redis_sentinel_startup")


@dataclass(frozen=True)
class SentinelStartupConfig:
    hosts: list[tuple[str, int]]
    master_name: str
    timeout_seconds: float
    initial_backoff_seconds: float
    max_backoff_seconds: float


def parse_sentinel_hosts(raw_hosts: str) -> list[tuple[str, int]]:
    hosts: list[tuple[str, int]] = []
    for raw_host in raw_hosts.split(","):
        entry = raw_host.strip()
        if not entry:
            continue
        host, separator, port = entry.rpartition(":")
        if not separator:
            host = entry
            port = "26379"
        if not host:
            raise ValueError("REDIS_SENTINEL_HOSTS contains an empty host")
        try:
            parsed_port = int(port)
        except ValueError as exc:
            raise ValueError(f"REDIS_SENTINEL_HOSTS has invalid port for {entry!r}") from exc
        if parsed_port < 1:
            raise ValueError(f"REDIS_SENTINEL_HOSTS has invalid port for {entry!r}")
        hosts.append((host, parsed_port))
    if not hosts:
        raise ValueError("REDIS_SENTINEL_HOSTS must include at least one host")
    return hosts


def _positive_float_env(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return default
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive number") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive number")
    return value


def load_config_from_env() -> SentinelStartupConfig | None:
    raw_hosts = os.getenv("REDIS_SENTINEL_HOSTS", "").strip()
    if not raw_hosts:
        return None
    return SentinelStartupConfig(
        hosts=parse_sentinel_hosts(raw_hosts),
        master_name=os.getenv("REDIS_SENTINEL_MASTER", "mymaster").strip() or "mymaster",
        timeout_seconds=_positive_float_env("REDIS_SENTINEL_STARTUP_TIMEOUT_SECONDS", 180.0),
        initial_backoff_seconds=_positive_float_env("REDIS_SENTINEL_STARTUP_INITIAL_BACKOFF_SECONDS", 1.0),
        max_backoff_seconds=_positive_float_env("REDIS_SENTINEL_STARTUP_MAX_BACKOFF_SECONDS", 10.0),
    )


async def verify_sentinel_master(config: SentinelStartupConfig, *, sentinel_factory=Sentinel, redis_factory=Redis) -> tuple[str, int]:
    sentinel = sentinel_factory(config.hosts, socket_timeout=2.0, socket_connect_timeout=2.0)
    redis = None
    try:
        master_host, master_port = await sentinel.discover_master(config.master_name)
        redis = redis_factory(host=master_host, port=master_port, socket_timeout=2.0, socket_connect_timeout=2.0)
        await redis.ping()
        info = await redis.info("replication")
        role = str(info.get("role", "")).lower()
        if role != "master":
            raise RuntimeError(f"Sentinel master {config.master_name!r} resolved to role {role!r}")
        probe_key = f"palaceoftruth:startup-probe:{os.getpid()}"
        wrote = await redis.set(probe_key, "1", ex=30, nx=True)
        if not wrote:
            raise RuntimeError("startup probe key already exists")
        await redis.delete(probe_key)
    finally:
        if redis is not None:
            await redis.aclose()
        sentinel_close = getattr(sentinel, "aclose", None)
        if sentinel_close is not None:
            await sentinel_close()
    return str(master_host), int(master_port)


async def wait_for_sentinel_master(config: SentinelStartupConfig, *, verifier=verify_sentinel_master) -> tuple[str, int]:
    deadline = time.monotonic() + config.timeout_seconds
    backoff = config.initial_backoff_seconds
    attempt = 0
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        attempt += 1
        try:
            master = await verifier(config)
            logger.info(
                "Redis Sentinel startup dependency ready: master=%s:%s sentinel_master=%s attempts=%s",
                master[0],
                master[1],
                config.master_name,
                attempt,
            )
            return master
        except Exception as exc:  # noqa: BLE001 - dependency warm-up errors are retried and logged.
            last_error = exc
            remaining = max(0.0, deadline - time.monotonic())
            logger.warning(
                "Waiting for Redis Sentinel master discovery before ARQ startup: sentinel_master=%s attempt=%s remaining=%.1fs error=%s",
                config.master_name,
                attempt,
                remaining,
                exc,
            )
            await asyncio.sleep(min(backoff, remaining))
            backoff = min(backoff * 2, config.max_backoff_seconds)
    raise TimeoutError(
        f"Redis Sentinel master {config.master_name!r} did not become writable within "
        f"{config.timeout_seconds:g}s; last error: {last_error}"
    )


def _split_command(argv: list[str]) -> list[str]:
    if "--" in argv:
        index = argv.index("--")
        command = argv[index + 1 :]
    else:
        command = argv
    if not command:
        raise ValueError("usage: wait_for_redis_sentinel.py -- <command> [args...]")
    return command


async def async_main(argv: list[str]) -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    command = _split_command(argv)
    config = load_config_from_env()
    if config is not None:
        await wait_for_sentinel_master(config)
    else:
        logger.info("REDIS_SENTINEL_HOSTS is unset; skipping Redis Sentinel startup dependency gate")
    os.execvp(command[0], command)
    return 127


def main() -> int:
    try:
        return asyncio.run(async_main(sys.argv[1:]))
    except Exception:
        logger.exception("Redis Sentinel startup dependency gate failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
