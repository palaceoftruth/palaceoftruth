"""The API must not reach ARQ's create_pool() before Sentinel has a primary.

Sentinel pods roll at the same time as the API. Without this gate, create_pool()
raised MasterNotFoundError during startup, Uvicorn exited, and the pod
crash-looped until the quorum happened to settle first.
"""

from __future__ import annotations

import pytest

from app import main as app_main


@pytest.mark.asyncio
async def test_lifespan_waits_for_sentinel_before_creating_the_arq_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    async def fake_database_wait() -> None:
        events.append("database")

    def fake_migrations() -> None:
        events.append("migrations")

    async def fake_seed_api_key() -> None:
        events.append("seed-api-key")

    async def fake_seed_sync_source() -> None:
        events.append("seed-sync-source")

    async def fake_sentinel_wait(config) -> tuple[str, int]:
        assert config is not None
        events.append("sentinel")
        return ("valkey-primary", 6379)

    async def fake_create_pool(*args, **kwargs):
        events.append("create-pool")

        class FakePool:
            async def close(self) -> None:
                events.append("close-pool")

        return FakePool()

    monkeypatch.setattr(app_main, "wait_for_database_startup", fake_database_wait)
    monkeypatch.setattr(app_main, "run_migrations", fake_migrations)
    monkeypatch.setattr(app_main, "_seed_default_api_key", fake_seed_api_key)
    monkeypatch.setattr(app_main, "_seed_default_palace_sync_source", fake_seed_sync_source)
    monkeypatch.setattr(app_main, "load_sentinel_startup_config", lambda: object())
    monkeypatch.setattr(app_main, "wait_for_sentinel_master", fake_sentinel_wait)
    monkeypatch.setattr(app_main, "create_pool", fake_create_pool)
    monkeypatch.setattr(app_main, "EmbeddingService", lambda: object())
    monkeypatch.setattr(app_main, "LLMService", lambda: object())

    async with app_main.lifespan(app_main.app):
        pass

    assert events.index("sentinel") < events.index("create-pool")
    assert events == [
        "database",
        "migrations",
        "seed-api-key",
        "seed-sync-source",
        "sentinel",
        "create-pool",
        "close-pool",
    ]


@pytest.mark.asyncio
async def test_sentinel_gate_is_skipped_without_sentinel_hosts(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """A plain REDIS_URL deployment has no discovery step, so it must not block."""
    called = False

    async def fail_if_called(config):  # pragma: no cover - must never run
        nonlocal called
        called = True
        raise AssertionError("sentinel wait ran without REDIS_SENTINEL_HOSTS")

    monkeypatch.setattr(app_main, "load_sentinel_startup_config", lambda: None)
    monkeypatch.setattr(app_main, "wait_for_sentinel_master", fail_if_called)

    await app_main.wait_for_sentinel_startup()

    assert called is False


@pytest.mark.asyncio
async def test_sentinel_gate_surfaces_timeout_rather_than_hanging_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate is bounded: a permanently broken quorum must still fail loudly."""

    async def timing_out_wait(config):
        raise TimeoutError("Redis Sentinel master 'mymaster' did not become writable within 180s")

    monkeypatch.setattr(app_main, "load_sentinel_startup_config", lambda: object())
    monkeypatch.setattr(app_main, "wait_for_sentinel_master", timing_out_wait)

    with pytest.raises(TimeoutError, match="did not become writable"):
        await app_main.wait_for_sentinel_startup()
