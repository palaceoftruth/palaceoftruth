"""Starting a canon sync run: the refusals, and the scheduled path.

Each refusal has to leave the database untouched, because a person who is told
"no" must not later find a run they did not ask for.

Set ``CANON_TEST_DATABASE_URL`` to a database at the current alembic head.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.palace import SyncRun, SyncSource, Wing
from app.services.canon import CanonValidationError
from app.services.canon_sync import (
    CanonSyncInFlightError,
    get_canon_binding,
    start_canon_sync_run,
)

DATABASE_URL = os.environ.get("CANON_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="CANON_TEST_DATABASE_URL is not set; starting a run needs a real database",
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "master")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "canon").mkdir(exist_ok=True)
    (root / "canon" / "one.md").write_text(
        "---\ntitle: One\nsummary: One page.\nsection: rules\n---\n# One\n\nBody.\n"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return root


@pytest_asyncio.fixture
async def env(tmp_path):
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"canon-start-{uuid.uuid4().hex[:10]}"
    root = _repo(tmp_path / "corpus")

    async with factory() as db:
        wing = Wing(tenant_id=tenant, slug="company", name="Company")
        db.add(wing)
        await db.commit()
        source = SyncSource(
            tenant_id=tenant,
            name="corpus",
            root_path=str(root),
            source_kind="folder",
            credential_type="none",
            status="active",
            scan_interval_seconds=900,
            target_kind="canon",
            canon_section_default="reference",
            canon_owns_pages=True,
        )
        db.add(source)
        await db.commit()
        yield {"db": db, "tenant": tenant, "source": source, "root": root, "factory": factory}

    await engine.dispose()


async def _runs(db, tenant) -> int:
    return int(
        await db.scalar(
            select(func.count()).select_from(SyncRun).where(SyncRun.tenant_id == tenant)
        )
    )


@pytest.mark.asyncio
async def test_a_ready_binding_starts_one_queued_run(env) -> None:
    db, tenant, source = env["db"], env["tenant"], env["source"]

    run = await start_canon_sync_run(db, tenant_id=tenant, source=source)

    assert run.status == "queued"
    assert run.triggered_by == "manual"
    assert run.sync_source_id == source.id
    assert await _runs(db, tenant) == 1


@pytest.mark.asyncio
async def test_a_second_start_is_refused_and_creates_nothing(env) -> None:
    db, tenant, source = env["db"], env["tenant"], env["source"]
    first = await start_canon_sync_run(db, tenant_id=tenant, source=source)

    with pytest.raises(CanonSyncInFlightError) as excinfo:
        await start_canon_sync_run(db, tenant_id=tenant, source=source)

    assert excinfo.value.run.id == first.id
    assert await _runs(db, tenant) == 1, "a refused start must not queue a second run"


@pytest.mark.asyncio
async def test_an_unreachable_repo_is_refused_before_any_row_exists(env, tmp_path) -> None:
    db, tenant, source = env["db"], env["tenant"], env["source"]
    source.root_path = str(tmp_path / "not-a-repo")
    await db.commit()

    with pytest.raises(HTTPException) as excinfo:
        await start_canon_sync_run(db, tenant_id=tenant, source=source)

    assert excinfo.value.status_code == 422
    assert "repo unreachable" in excinfo.value.detail
    assert await _runs(db, tenant) == 0, "an unreachable repo must leave no run behind"


@pytest.mark.asyncio
async def test_another_tenants_binding_cannot_be_started(env) -> None:
    db, source = env["db"], env["source"]

    with pytest.raises(CanonValidationError) as excinfo:
        await start_canon_sync_run(db, tenant_id="someone-else", source=source)

    assert "sync source not found" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_room_source_is_not_a_canon_binding(env) -> None:
    db, tenant, source = env["db"], env["tenant"], env["source"]
    source.target_kind = "rooms"
    await db.commit()

    with pytest.raises(CanonValidationError) as excinfo:
        await start_canon_sync_run(db, tenant_id=tenant, source=source)

    assert "not a canon binding" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_paused_binding_does_not_run(env) -> None:
    db, tenant, source = env["db"], env["tenant"], env["source"]
    source.status = "paused"
    await db.commit()

    with pytest.raises(CanonValidationError) as excinfo:
        await start_canon_sync_run(db, tenant_id=tenant, source=source)

    assert "not active" in str(excinfo.value)


@pytest.mark.asyncio
async def test_the_loader_never_returns_another_tenants_binding(env) -> None:
    db, tenant, source = env["db"], env["tenant"], env["source"]

    assert (await get_canon_binding(db, tenant_id=tenant, source_id=source.id)) is not None
    assert (await get_canon_binding(db, tenant_id="someone-else", source_id=source.id)) is None


@pytest.mark.asyncio
async def test_the_scheduled_poller_picks_up_a_canon_binding(env, monkeypatch) -> None:
    """A canon binding is scheduled by the existing poller, not by a new one."""
    from app.workers import palace_tasks

    db, tenant, source = env["db"], env["tenant"], env["source"]
    enqueued: list[tuple[str, str]] = []

    @contextlib.asynccontextmanager
    async def _session():
        yield db

    async def _enqueue(_pool, job_name, *, sync_run_id, tenant_id):
        enqueued.append((job_name, sync_run_id))

    monkeypatch.setattr(palace_tasks, "system_async_session", _session)
    monkeypatch.setattr(palace_tasks, "enqueue_palace_job", _enqueue)

    await palace_tasks.poll_sync_sources({"redis": object()})

    assert enqueued and enqueued[0][0] == "run_sync_source"
    assert await _runs(db, tenant) == 1
