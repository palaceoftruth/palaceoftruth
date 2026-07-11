"""Block ARQ worker startup until its database and Sentinel dependencies are usable."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    # Helm invokes this file directly, so include the backend package root.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.wait_for_database import wait_for_writable_database
from scripts.wait_for_redis_sentinel import (
    _positive_float_env,
    _split_command,
    load_config_from_env,
    wait_for_sentinel_master,
)


logger = logging.getLogger("palaceoftruth.worker_startup")


async def async_main(argv: list[str]) -> int:
    """Wait for durable dependencies before replacing this process with ARQ."""
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    command = _split_command(argv)
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise ValueError("DATABASE_URL is required for ARQ worker startup")

    await wait_for_writable_database(
        database_url,
        timeout_seconds=_positive_float_env("WORKER_DB_WAIT_TIMEOUT_SECONDS", 300.0),
        interval_seconds=_positive_float_env("WORKER_DB_WAIT_INTERVAL_SECONDS", 5.0),
        connect_timeout_seconds=_positive_float_env("WORKER_DB_CONNECT_TIMEOUT_SECONDS", 5.0),
    )
    logger.info("Worker database startup dependency ready")

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
        logger.exception("Worker runtime dependency gate failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
