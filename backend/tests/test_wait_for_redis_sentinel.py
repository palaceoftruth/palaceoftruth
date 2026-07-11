from __future__ import annotations

import pytest

from scripts.wait_for_redis_sentinel import (
    SentinelStartupConfig,
    load_config_from_env,
    parse_sentinel_hosts,
    verify_sentinel_master,
    wait_for_sentinel_master,
)
from scripts import wait_for_worker_dependencies
from scripts.check_redis_sentinel_rollout_gate import check_rollout_gate


def test_parse_sentinel_hosts_accepts_defaults_and_multiple_hosts() -> None:
    assert parse_sentinel_hosts("valkey-sentinel, backup-sentinel:26380") == [
        ("valkey-sentinel", 26379),
        ("backup-sentinel", 26380),
    ]


def test_load_config_skips_gate_without_sentinel_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDIS_SENTINEL_HOSTS", raising=False)

    assert load_config_from_env() is None


def test_load_config_uses_extended_startup_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_SENTINEL_HOSTS", "valkey-sentinel:26379")
    monkeypatch.setenv("REDIS_SENTINEL_MASTER", "mymaster")
    monkeypatch.setenv("REDIS_SENTINEL_STARTUP_TIMEOUT_SECONDS", "240")
    monkeypatch.setenv("REDIS_SENTINEL_STARTUP_INITIAL_BACKOFF_SECONDS", "2")
    monkeypatch.setenv("REDIS_SENTINEL_STARTUP_MAX_BACKOFF_SECONDS", "12")

    config = load_config_from_env()

    assert config == SentinelStartupConfig(
        hosts=[("valkey-sentinel", 26379)],
        master_name="mymaster",
        timeout_seconds=240,
        initial_backoff_seconds=2,
        max_backoff_seconds=12,
    )


@pytest.mark.asyncio
async def test_verify_sentinel_master_requires_writable_primary() -> None:
    class FakeSentinel:
        def __init__(self, hosts, **kwargs) -> None:
            self.hosts = hosts
            self.kwargs = kwargs

        async def discover_master(self, master_name: str):
            assert master_name == "mymaster"
            return "valkey-primary", 6379

        async def aclose(self) -> None:
            self.closed = True

    class FakeRedis:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.deleted: list[str] = []

        async def ping(self) -> bool:
            return True

        async def info(self, section: str) -> dict[str, str]:
            assert section == "replication"
            return {"role": "master"}

        async def set(self, key: str, value: str, *, ex: int, nx: bool) -> bool:
            assert key.startswith("palaceoftruth:startup-probe:")
            assert value == "1"
            assert ex == 30
            assert nx is True
            return True

        async def delete(self, key: str) -> int:
            self.deleted.append(key)
            return 1

        async def aclose(self) -> None:
            self.closed = True

    config = SentinelStartupConfig(
        hosts=[("valkey-sentinel", 26379)],
        master_name="mymaster",
        timeout_seconds=1,
        initial_backoff_seconds=0.01,
        max_backoff_seconds=0.01,
    )

    assert await verify_sentinel_master(config, sentinel_factory=FakeSentinel, redis_factory=FakeRedis) == (
        "valkey-primary",
        6379,
    )


@pytest.mark.asyncio
async def test_verify_sentinel_master_closes_sentinel_when_discovery_fails() -> None:
    closed = False

    class BrokenSentinel:
        def __init__(self, hosts, **kwargs) -> None:
            self.hosts = hosts
            self.kwargs = kwargs

        async def discover_master(self, master_name: str):
            raise RuntimeError(f"No master found for {master_name!r}")

        async def aclose(self) -> None:
            nonlocal closed
            closed = True

    config = SentinelStartupConfig(
        hosts=[("valkey-sentinel", 26379)],
        master_name="mymaster",
        timeout_seconds=1,
        initial_backoff_seconds=0.01,
        max_backoff_seconds=0.01,
    )

    with pytest.raises(RuntimeError, match="No master found"):
        await verify_sentinel_master(config, sentinel_factory=BrokenSentinel)

    assert closed is True


@pytest.mark.asyncio
async def test_wait_for_sentinel_master_retries_dependency_startup_errors() -> None:
    config = SentinelStartupConfig(
        hosts=[("valkey-sentinel", 26379)],
        master_name="mymaster",
        timeout_seconds=1,
        initial_backoff_seconds=0.01,
        max_backoff_seconds=0.01,
    )
    attempts = 0

    async def flaky_verifier(_config: SentinelStartupConfig):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("No master found for 'mymaster'")
        return ("valkey-primary", 6379)

    assert await wait_for_sentinel_master(config, verifier=flaky_verifier) == ("valkey-primary", 6379)
    assert attempts == 2


@pytest.mark.asyncio
async def test_worker_gate_waits_for_database_before_sentinel_and_arq(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://example")

    async def fake_database_wait(*args, **kwargs) -> None:
        events.append("database")

    async def fake_sentinel_wait(config) -> None:
        assert config is not None
        events.append("sentinel")

    def fake_execvp(command: str, args: list[str]) -> None:
        events.append(f"exec:{command}:{args[-1]}")
        raise RuntimeError("exec called")

    monkeypatch.setattr(wait_for_worker_dependencies, "wait_for_writable_database", fake_database_wait)
    monkeypatch.setattr(wait_for_worker_dependencies, "load_config_from_env", lambda: object())
    monkeypatch.setattr(wait_for_worker_dependencies, "wait_for_sentinel_master", fake_sentinel_wait)
    monkeypatch.setattr(wait_for_worker_dependencies.os, "execvp", fake_execvp)

    with pytest.raises(RuntimeError, match="exec called"):
        await wait_for_worker_dependencies.async_main(["--", "arq", "app.workers.worker.WorkerSettings"])

    assert events == ["database", "sentinel", "exec:arq:app.workers.worker.WorkerSettings"]


@pytest.mark.asyncio
async def test_rollout_gate_checks_replica_link_and_enqueue_dequeue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_SENTINEL_HOSTS", "valkey-sentinel:26379")
    monkeypatch.setenv("REDIS_SENTINEL_MASTER", "mymaster")
    monkeypatch.setenv("REDIS_SENTINEL_EXPECTED_REPLICAS", "1")

    class FakeSentinel:
        def __init__(self, hosts, **kwargs) -> None:
            self.hosts = hosts
            self.kwargs = kwargs

        async def discover_master(self, master_name: str):
            assert master_name == "mymaster"
            return "valkey-primary", 6379

        async def aclose(self) -> None:
            self.closed = True

    class FakeRedis:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.queue: list[bytes] = []
            self.deleted: list[str] = []

        async def ping(self) -> bool:
            return True

        async def info(self, section: str) -> dict[str, object]:
            assert section == "replication"
            return {"role": "master", "connected_slaves": 1}

        async def lpush(self, key: str, value: bytes) -> int:
            assert key.startswith("palaceoftruth:rollout-gate:")
            self.queue.insert(0, value)
            return len(self.queue)

        async def rpop(self, key: str) -> bytes | None:
            assert key.startswith("palaceoftruth:rollout-gate:")
            return self.queue.pop() if self.queue else None

        async def delete(self, key: str) -> int:
            self.deleted.append(key)
            return 1

        async def aclose(self) -> None:
            self.closed = True

    result = await check_rollout_gate(sentinel_factory=FakeSentinel, redis_factory=FakeRedis)

    assert result.master_host == "valkey-primary"
    assert result.master_port == 6379
    assert result.connected_replicas == 1


@pytest.mark.asyncio
async def test_rollout_gate_fails_when_replica_link_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_SENTINEL_HOSTS", "valkey-sentinel:26379")
    monkeypatch.setenv("REDIS_SENTINEL_EXPECTED_REPLICAS", "1")

    class FakeSentinel:
        def __init__(self, hosts, **kwargs) -> None:
            self.hosts = hosts
            self.kwargs = kwargs

        async def discover_master(self, master_name: str):
            return "valkey-primary", 6379

        async def aclose(self) -> None:
            self.closed = True

    class FakeRedis:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        async def ping(self) -> bool:
            return True

        async def info(self, section: str) -> dict[str, object]:
            return {"role": "master", "connected_slaves": 0}

        async def delete(self, key: str) -> int:
            return 0

        async def aclose(self) -> None:
            self.closed = True

    with pytest.raises(RuntimeError, match="expected at least 1"):
        await check_rollout_gate(sentinel_factory=FakeSentinel, redis_factory=FakeRedis)
