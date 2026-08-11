from types import SimpleNamespace

import pytest

from app.workers import tasks
from app.workers.worker import WorkerSettings


class ReaperDb:
    def __init__(self) -> None:
        self.sql = ""
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, statement):
        self.sql = " ".join(str(statement).lower().split())
        return SimpleNamespace(rowcount=7)

    async def commit(self) -> None:
        self.commits += 1


@pytest.mark.asyncio
async def test_reap_browser_sessions_removes_only_expired_or_old_revoked_rows(monkeypatch) -> None:
    db = ReaperDb()
    monkeypatch.setattr(tasks, "async_session", lambda: db)

    assert await tasks.reap_browser_sessions() == 7
    assert "expires_at <= current_timestamp" in db.sql
    assert "revoked_at < current_timestamp - interval '7 days'" in db.sql
    assert db.commits == 1


def test_default_worker_schedules_browser_session_reaping() -> None:
    assert "reap_browser_sessions" in {function.__name__ for function in WorkerSettings.functions}
    assert "cron:reap_browser_sessions" in {job.name for job in WorkerSettings.cron_jobs}
