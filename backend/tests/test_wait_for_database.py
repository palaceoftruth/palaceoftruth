import asyncio
from types import SimpleNamespace

import pytest

from app.wait_for_database import database_is_writable, wait_for_writable_database


def test_readiness_converts_verify_full_url_to_asyncpg_ssl_context(monkeypatch, tmp_path):
    ca_file = tmp_path / "ca.crt"
    ca_file.write_text("test CA")
    captured = {}

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _statement):
            return SimpleNamespace(scalar_one=lambda: True)

    class Engine:
        def connect(self):
            return Connection()

        async def dispose(self):
            return None

    def fake_ssl_context(*, cafile):
        captured["cafile"] = cafile
        return SimpleNamespace(check_hostname=False, verify_mode=None)

    def fake_engine(url, **options):
        captured["url"] = url
        captured["options"] = options
        return Engine()

    monkeypatch.setattr("app.wait_for_database.ssl.create_default_context", fake_ssl_context)
    monkeypatch.setattr("app.wait_for_database.create_async_engine", fake_engine)

    assert asyncio.run(database_is_writable(
        f"postgresql+asyncpg://user:secret@postgres.test/palace"
        f"?sslmode=verify-full&sslrootcert={ca_file}",
        2,
    ))
    assert "sslmode" not in captured["url"]
    assert "sslrootcert" not in captured["url"]
    assert captured["cafile"] == str(ca_file)
    assert "ssl" in captured["options"]["connect_args"]


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
