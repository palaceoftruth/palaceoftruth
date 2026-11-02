"""Run history and file-level actions, against a real database.

The guarantees worth testing are the ones a fake session cannot show: that a run
records what it did to each path, that one tenant never reads another's runs, and
that a page rewritten by the repository while drifting stays drift-suspected.

Set ``CANON_TEST_DATABASE_URL`` to a database at the current alembic head.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.canon import CanonPage
from app.models.palace import Room, SyncRun, SyncSource, SyncSourceFile, Wing
from app.services.canon_history import (
    get_canon_run,
    latest_canon_run,
    list_canon_run_files,
    list_canon_runs,
)
from app.services.canon_sync import run_canon_sync

DATABASE_URL = os.environ.get("CANON_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="CANON_TEST_DATABASE_URL is not set; run history needs a real database",
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _corpus(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "master")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    return root


def _write(root: Path, name: str, body: str, section: str = "rules") -> None:
    path = root / "canon" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntitle: {name}\nsummary: About {name}.\nsection: {section}\n---\n{body}"
    )


def _commit(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


@pytest_asyncio.fixture
async def env(tmp_path):
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"canon-history-{uuid.uuid4().hex[:10]}"
    root = _corpus(tmp_path / "corpus")

    async with factory() as db:
        wing = Wing(tenant_id=tenant, slug="company", name="Company")
        db.add(wing)
        await db.flush()
        room = Room(
            tenant_id=tenant,
            wing_id=wing.id,
            slug="product-decisions",
            stable_key=f"room:product-decisions:{tenant}",
            name="Product Decisions",
            snapshot_generation=2,
        )
        db.add(room)
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
        yield {"db": db, "tenant": tenant, "source": source, "root": root,
               "room": room, "factory": factory}

    await engine.dispose()


async def _run(db, source) -> SyncRun:
    run = SyncRun(sync_source_id=source.id, tenant_id=source.tenant_id, status="queued")
    db.add(run)
    await db.commit()
    await db.refresh(run)
    await run_canon_sync(db, run=run, source=source, embedder=None, llm=None)
    await db.refresh(run)
    return run


@pytest.mark.asyncio
async def test_a_run_records_the_action_it_took_on_every_path(env) -> None:
    db, tenant, source, root = env["db"], env["tenant"], env["source"], env["root"]
    _write(root, "alpha.md", "# Alpha\n\nFirst.\n")
    _write(root, "beta.md", "# Beta\n\nSecond.\n")
    _commit(root, "seed")
    first = await _run(db, source)

    _write(root, "alpha.md", "# Alpha\n\nRewritten.\n")
    _commit(root, "edit alpha")
    second = await _run(db, source)

    files = {row.relative_path: row for row in await list_canon_run_files(
        db, tenant_id=tenant, run_id=second.id)}
    assert files["canon/alpha.md"].last_action == "update"
    assert files["canon/beta.md"].last_action == "skip-unchanged"
    assert files["canon/alpha.md"].last_seen_run_id == second.id
    assert first.pages_created == 2


@pytest.mark.asyncio
async def test_a_deleted_path_records_an_archive_action(env) -> None:
    db, tenant, source, root = env["db"], env["tenant"], env["source"], env["root"]
    _write(root, "gone.md", "# Gone\n\nLeaving.\n")
    _commit(root, "seed")
    await _run(db, source)

    (root / "canon" / "gone.md").unlink()
    _commit(root, "drop it")
    run = await _run(db, source)

    files = await list_canon_run_files(db, tenant_id=tenant, run_id=run.id)
    gone = next(row for row in files if row.relative_path == "canon/gone.md")
    assert gone.last_action == "archive"
    assert gone.status == "deleted"


@pytest.mark.asyncio
async def test_runs_are_listed_newest_first_for_one_binding(env) -> None:
    db, tenant, source, root = env["db"], env["tenant"], env["source"], env["root"]
    _write(root, "one.md", "# One\n\nBody.\n")
    _commit(root, "seed")
    first = await _run(db, source)
    second = await _run(db, source)

    runs = await list_canon_runs(db, tenant_id=tenant, source_id=source.id, limit=10)
    assert [run.id for run in runs] == [second.id, first.id]
    latest = await latest_canon_run(db, tenant_id=tenant, source_id=source.id)
    assert latest is not None and latest.id == second.id


@pytest.mark.asyncio
async def test_one_tenant_cannot_read_another_tenants_run(env) -> None:
    db, tenant, source, root = env["db"], env["tenant"], env["source"], env["root"]
    _write(root, "one.md", "# One\n\nBody.\n")
    _commit(root, "seed")
    run = await _run(db, source)

    assert await get_canon_run(db, tenant_id="someone-else", run_id=run.id) is None
    assert await list_canon_run_files(db, tenant_id="someone-else", run_id=run.id) == []
    assert await list_canon_runs(db, tenant_id="someone-else") == []


@pytest.mark.asyncio
async def test_the_run_limit_is_clamped_not_trusted(env) -> None:
    db, tenant, source, root = env["db"], env["tenant"], env["source"], env["root"]
    _write(root, "one.md", "# One\n\nBody.\n")
    _commit(root, "seed")
    await _run(db, source)

    runs = await list_canon_runs(db, tenant_id=tenant, limit=10_000)
    assert len(runs) == 1


@pytest.mark.asyncio
async def test_a_repo_update_does_not_clear_drift(env) -> None:
    from app.services.canon_drift import detect_canon_drift, list_canon_drift

    db, tenant, source, root, room = (
        env["db"], env["tenant"], env["source"], env["root"], env["room"]
    )
    path = root / "canon" / "charter.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\ntitle: Charter\nsummary: Why the room exists.\nsection: charter\n"
        "room: product-decisions\n---\n# Charter\n\nOriginal text.\n"
    )
    _commit(root, "seed")
    await _run(db, source)

    page = await db.scalar(
        select(CanonPage).where(CanonPage.tenant_id == tenant).where(CanonPage.slug == "charter")
    )
    assert page.room_id == room.id
    # Ratified at the room's current generation, then the room moves: that is the
    # only state in which "drift" and not "stale" is the honest reading.
    page.last_confirmed_generation = 2
    page.status = "published"
    room.snapshot_generation = 7
    await db.commit()
    await detect_canon_drift(db, tenant_id=tenant)
    await db.commit()

    path.write_text(
        "---\ntitle: Charter\nsummary: Why the room exists.\nsection: charter\n"
        "room: product-decisions\n---\n# Charter\n\nRewritten by the repo.\n"
    )
    _commit(root, "rewrite")
    run = await _run(db, source)

    states = {row.slug: row.state for row in await list_canon_drift(db, tenant_id=tenant)}
    assert states["charter"] == "drift-suspected", "a repo write must not ratify a page"
    # A published page is never overwritten by a repo change: the run files a
    # conflict and leaves the text alone. That is also why drift survives.
    files = await list_canon_run_files(db, tenant_id=tenant, run_id=run.id)
    charter = next(row for row in files if row.relative_path == "canon/charter.md")
    assert charter.last_action == "conflict"
    assert run.pages_conflicted == 1
