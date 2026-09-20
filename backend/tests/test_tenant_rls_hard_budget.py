"""A total enforcement budget must interrupt work, not only check errors."""
import asyncio
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.exc import DBAPIError
from app import enforce_tenant_rls as hook


class LockError(Exception):
    sqlstate = "55P03"


class Connection:
    def __init__(self, *, lock_error=False):
        self.started = self.rolled_back = 0
        self.lock_error = lock_error

    @asynccontextmanager
    async def begin(self):
        self.started += 1
        try:
            yield
        except BaseException:
            self.rolled_back += 1
            raise

    async def execute(self, statement):
        if self.lock_error:
            raise DBAPIError("DDL", None, LockError())
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_expired_budget_starts_no_transaction():
    conn = Connection(lock_error=True)
    with pytest.raises(TimeoutError):
        await hook._enforce_table(conn, "embeddings", 10000, 5, asyncio.get_running_loop().time() - 1)
    assert conn.started == 0


@pytest.mark.asyncio
async def test_budget_interrupts_stalled_sql_and_rolls_back():
    conn = Connection()
    task = asyncio.create_task(hook._enforce_table(
        conn, "embeddings", 10000, 5, asyncio.get_running_loop().time() + .02
    ))
    done, _ = await asyncio.wait({task}, timeout=.3)
    if not done:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert done, "budget did not interrupt the stalled SQL"
    with pytest.raises(TimeoutError):
        task.result()
    assert conn.rolled_back == 1


@pytest.mark.asyncio
async def test_budget_interrupts_backoff(monkeypatch):
    async def sleep(_):
        await asyncio.Event().wait()
    monkeypatch.setattr(hook.asyncio, "sleep", sleep)
    conn = Connection(lock_error=True)
    task = asyncio.create_task(hook._enforce_table(
        conn, "embeddings", 10000, 5, asyncio.get_running_loop().time() + .02
    ))
    done, _ = await asyncio.wait({task}, timeout=.3)
    if not done:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert done, "budget did not interrupt backoff"
    with pytest.raises(TimeoutError):
        task.result()
    assert conn.started == conn.rolled_back == 1
