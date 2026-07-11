import asyncio

import pytest

from app.wait_for_database import wait_for_writable_database


def test_waits_through_restart_until_primary_is_writable(monkeypatch, capsys):
    outcomes = iter([ConnectionRefusedError("primary is restarting"), False, True])
    sleeps: list[float] = []

    async def check(_database_url: str, _connect_timeout: float) -> bool:
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("app.wait_for_database.asyncio.sleep", sleep)

    asyncio.run(
        wait_for_writable_database(
            "postgresql+asyncpg://user:secret@postgres.test/palace",
            timeout_seconds=30,
            interval_seconds=1,
            connect_timeout_seconds=2,
            check=check,
        )
    )

    assert sleeps == [1, 1]
    output = capsys.readouterr().out
    assert "ConnectionRefusedError: primary is restarting" in output
    assert "ready after 3 attempt(s)" in output


def test_timeout_preserves_last_connection_error(monkeypatch):
    times = iter([0.0, 0.0, 2.0])

    async def check(_database_url: str, _connect_timeout: float) -> bool:
        raise ConnectionRefusedError("primary is fenced")

    async def sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("app.wait_for_database.asyncio.sleep", sleep)

    with pytest.raises(TimeoutError, match="ConnectionRefusedError: primary is fenced"):
        asyncio.run(
            wait_for_writable_database(
                "postgresql+asyncpg://user:secret@postgres.test/palace",
                timeout_seconds=1,
                interval_seconds=1,
                connect_timeout_seconds=1,
                check=check,
                clock=lambda: next(times),
            )
        )
