from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app import enforce_tenant_rls
from app.config import Settings

SOURCE = Path(enforce_tenant_rls.__file__).read_text()

# Read the portable default, not an environment-specific 840-second override.
import yaml
CHART_HOOK_DEADLINE_SECONDS = yaml.safe_load(
    (Path(__file__).parents[2] / "chart/values.yaml").read_text()
)["migrations"]["activeDeadlineSeconds"]


def _settings(overrides: dict[str, object]) -> Settings:
    base: dict[str, object] = {
        "database_url": "postgresql+asyncpg://user:pass@localhost/testdb",
        "redis_url": "redis://localhost:6379/0",
        "openai_api_key": "test-openai-key",
        "openrouter_api_key": "test-openrouter-key",
        "api_key": "test-api-key",
    }
    base.update(overrides)
    return Settings.model_validate(base)


def test_rls_hook_uses_bounded_retrying_lock_waits_not_a_fixed_timeout() -> None:
    """Regression: the hook must not depend on a single fixed lock wait.

    A live semantic-search AccessShareLock on the 2 GiB embeddings table outlasted
    the former one-shot `SET LOCAL lock_timeout = '5s'`, failing the post-upgrade
    hook with LockNotAvailableError and leaving the release Stalled. The per-table
    wait is now configurable and retried.
    """

    assert "lock_timeout" in SOURCE
    assert "SET LOCAL lock_timeout = '5s'" not in SOURCE
    assert "database_rls_lock_timeout_ms" in SOURCE
    assert "database_rls_lock_attempts" in SOURCE
    assert "database_rls_total_budget_seconds" in SOURCE
    assert "_enforce_table" in SOURCE
    assert "await asyncio.sleep(" in SOURCE


def test_rls_hook_retries_only_lock_timeouts() -> None:
    assert "55P03" in SOURCE
    assert "_is_lock_timeout" in SOURCE


def test_rls_lock_settings_validate_floors() -> None:
    assert _settings({"database_rls_lock_timeout_ms": 10_000}).database_rls_lock_timeout_ms == 10_000
    assert _settings({"database_rls_lock_attempts": 5}).database_rls_lock_attempts == 5
    assert (
        _settings({"database_rls_total_budget_seconds": 720}).database_rls_total_budget_seconds
        == 720
    )

    for field in (
        "database_rls_lock_timeout_ms",
        "database_rls_lock_attempts",
        "database_rls_total_budget_seconds",
    ):
        with pytest.raises(ValueError, match=field.upper()):
            _settings({field: 0})


def test_total_budget_ceiling_guarantees_the_hook_fits_its_deadline() -> None:
    """The one bound that actually protects the release deadline.

    Per-table attempts x timeout is not a hook bound (52 tables retrying linearly
    would exceed 840s), so the whole-hook budget must on its own sit under the
    chart's migrations.activeDeadlineSeconds.
    """

    settings = _settings({})
    assert settings.database_rls_total_budget_seconds < CHART_HOOK_DEADLINE_SECONDS
    # Even if every retry slept its maximum backoff, the hook stops at the budget.
    worst_case_per_table = sum(
        settings.database_rls_lock_timeout_ms / 1000 * (1 + attempt)
        for attempt in range(1, settings.database_rls_lock_attempts)
    ) + settings.database_rls_lock_timeout_ms / 1000
    assert worst_case_per_table > settings.database_rls_total_budget_seconds / len(
        enforce_tenant_rls.TENANT_TABLES
    )


def test_lock_timeout_detection_matches_asyncpg_lock_not_available() -> None:
    class _Wrapped(Exception):
        sqlstate = "55P03"

    assert enforce_tenant_rls._is_lock_timeout(_Wrapped())

    class _Inner(Exception):
        pgcode = "55P03"

    outer = RuntimeError("outer")
    outer.__cause__ = _Inner()
    assert enforce_tenant_rls._is_lock_timeout(outer)

    class _Origin(Exception):
        sqlstate = "55P03"

    class _Dbapi(Exception):
        def __init__(self) -> None:
            self.orig = _Origin()

    assert enforce_tenant_rls._is_lock_timeout(_Dbapi())

    assert not enforce_tenant_rls._is_lock_timeout(RuntimeError("not a lock"))


class _FakeDBAPI(Exception):
    def __init__(self, sqlstate: str = "55P03") -> None:
        super().__init__("lock timeout")
        self.sqlstate = sqlstate


class _Txn:
    async def __aenter__(self) -> "_Txn":
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _Conn:
    def __init__(self, fail_times: int) -> None:
        self.calls = 0
        self.fail_times = fail_times

    async def execute(self, *_args: object, **_kwargs: object) -> None:
        return None

    def begin(self) -> _Txn:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise _FakeDBAPI()
        return _Txn()


def _patch(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    sleeps: list[float] = []

    async def _no_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(enforce_tenant_rls, "DBAPIError", _FakeDBAPI)
    return sleeps


@pytest.mark.asyncio
async def test_enforce_table_retries_lock_timeouts_then_succeeds(monkeypatch) -> None:
    sleeps = _patch(monkeypatch)
    connection = _Conn(fail_times=2)
    deadline = asyncio.get_event_loop().time() + 720
    await enforce_tenant_rls._enforce_table(connection, "embeddings", 10_000, 5, deadline)

    assert connection.calls == 3
    assert sleeps == [10.0, 20.0]


@pytest.mark.asyncio
async def test_enforce_table_fails_loudly_when_attempts_are_exhausted(monkeypatch) -> None:
    _patch(monkeypatch)
    connection = _Conn(fail_times=99)
    deadline = asyncio.get_event_loop().time() + 720
    with pytest.raises(RuntimeError, match="embeddings"):
        await enforce_tenant_rls._enforce_table(connection, "embeddings", 10_000, 3, deadline)
    assert connection.calls == 3


@pytest.mark.asyncio
async def test_enforce_table_stops_at_the_total_budget(monkeypatch) -> None:
    """An already-spent budget must stop retrying instead of running past 840s."""

    _patch(monkeypatch)
    connection = _Conn(fail_times=99)
    deadline = asyncio.get_event_loop().time() - 1
    with pytest.raises(TimeoutError, match="total lock budget"):
        await enforce_tenant_rls._enforce_table(connection, "embeddings", 10_000, 5, deadline)
    assert connection.calls == 0


@pytest.mark.asyncio
async def test_enforce_table_does_not_retry_non_lock_errors(monkeypatch) -> None:
    _patch(monkeypatch)

    class _ConnOtherError(_Conn):
        def begin(self) -> _Txn:
            self.calls += 1
            raise _FakeDBAPI(sqlstate="23505")

    connection = _ConnOtherError(fail_times=0)
    deadline = asyncio.get_event_loop().time() + 720
    with pytest.raises(_FakeDBAPI):
        await enforce_tenant_rls._enforce_table(connection, "embeddings", 10_000, 5, deadline)
    assert connection.calls == 1
