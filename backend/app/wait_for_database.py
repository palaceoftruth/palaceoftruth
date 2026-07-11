"""Bounded readiness gate for database migrations."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


WritableCheck = Callable[[str, float], Awaitable[bool]]
Clock = Callable[[], float]


async def database_is_writable(database_url: str, connect_timeout: float) -> bool:
    """Return whether the target is the writable PostgreSQL primary."""
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with asyncio.timeout(connect_timeout):
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        "SELECT NOT pg_is_in_recovery() "
                        "AND current_setting('transaction_read_only') = 'off'"
                    )
                )
                return bool(result.scalar_one())
    finally:
        await engine.dispose()


async def wait_for_writable_database(
    database_url: str,
    *,
    timeout_seconds: float,
    interval_seconds: float,
    connect_timeout_seconds: float,
    check: WritableCheck = database_is_writable,
    clock: Clock = time.monotonic,
) -> None:
    """Wait until PostgreSQL accepts writes, or raise after a bounded timeout."""
    deadline = clock() + timeout_seconds
    attempt = 0
    last_error = "database reported read-only"

    while True:
        attempt += 1
        try:
            if await check(database_url, connect_timeout_seconds):
                print(f"Writable database primary is ready after {attempt} attempt(s).")
                return
            last_error = "database reported read-only"
        except Exception as exc:  # The final error is retained for hook diagnostics.
            last_error = f"{type(exc).__name__}: {exc}"

        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError(
                "Timed out waiting for a writable database primary after "
                f"{timeout_seconds:g}s ({attempt} attempt(s)); last error: {last_error}"
            )

        print(
            f"Database primary is not writable (attempt {attempt}; {last_error}); "
            f"retrying in {min(interval_seconds, remaining):g}s.",
            flush=True,
        )
        await asyncio.sleep(min(interval_seconds, remaining))


def _positive_float(name: str, default: str) -> float:
    raw_value = os.getenv(name, default)
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw_value!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero, got {raw_value!r}")
    return value


async def main() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise ValueError("DATABASE_URL is required")

    await wait_for_writable_database(
        database_url,
        timeout_seconds=_positive_float("MIGRATION_DB_WAIT_TIMEOUT_SECONDS", "240"),
        interval_seconds=_positive_float("MIGRATION_DB_WAIT_INTERVAL_SECONDS", "5"),
        connect_timeout_seconds=_positive_float(
            "MIGRATION_DB_CONNECT_TIMEOUT_SECONDS", "5"
        ),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print(f"Migration database readiness gate failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
