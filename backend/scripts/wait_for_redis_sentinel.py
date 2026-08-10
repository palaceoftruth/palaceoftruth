"""CLI wrapper that blocks a command until the Sentinel primary is writable.

The wait logic itself lives in app.wait_for_redis_sentinel so the API lifespan
can share it. This module keeps the exec-style entrypoint used by Helm and
re-exports the shared names, which several scripts and tests import from here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    # Helm invokes this file directly, so include the backend package root.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.wait_for_redis_sentinel import (  # noqa: E402 - must follow the sys.path fix above.
    SentinelStartupConfig,
    _positive_float_env,
    load_config_from_env,
    logger,
    parse_sentinel_hosts,
    redis_auth_kwargs,
    verify_sentinel_master,
    wait_for_sentinel_master,
)

__all__ = [
    "SentinelStartupConfig",
    "_positive_float_env",
    "load_config_from_env",
    "logger",
    "parse_sentinel_hosts",
    "redis_auth_kwargs",
    "verify_sentinel_master",
    "wait_for_sentinel_master",
]


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
