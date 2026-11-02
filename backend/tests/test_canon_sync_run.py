"""The ten named behaviours of a canon sync run, against a real database.

These need Postgres because the thing under test is the write path: revisions,
item status, archiving and artifacts. A fake session would prove that a function
was called and nothing about what a run does to a tenant's canon.

Set ``CANON_TEST_DATABASE_URL`` to a database at the current alembic head and they
run; without it they skip, so an ordinary CI run is unaffected.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.canon import CanonPage, CanonPageRevision
from app.models.item import Item
from app.models.palace import (
    CandidateCurationArtifact,
    Room,
    SyncRun,
    SyncSource,
    SyncSourceFile,
    Wing,
)
from app.services.canon_authoring import create_canon_page
from app.services.canon_sync import run_canon_sync

DATABASE_URL = os.environ.get("CANON_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="CANON_TEST_DATABASE_URL is not set; canon sync needs a real database",
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


class Corpus:
    """A throwaway git repository holding canon files."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        _git(root, "init", "-q", "-b", "master")
        _git(root, "config", "user.email", "test@example.com")
        _git(root, "config", "user.name", "Test")

    def write(self, name: str, body: str, *, section: str = "rules", title: str | None = None,
              room: str | None = None) -> None:
        path = self.root / "canon" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        front = [f"title: {title or name}", f"summary: About {name}.", f"section: {section}"]
        if room:
            front.append(f"room: {room}")
        path.write_text("---\n" + "\n".join(front) + "\n---\n" + body)

    def remove(self, name: str) -> None:
        (self.root / "canon" / name).unlink()

    def commit(self, message: str) -> str:
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", message)
        return _git(self.root, "rev-parse", "HEAD")


@pytest_asyncio.fixture
async def env(tmp_path):
    engine = create_async_engine(DATABASE_URL)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"canon-sync-test-{uuid.uuid4().hex[:10]}"
    corpus = Corpus(tmp_path / "corpus")

    async with session_factory() as db:
        wing = Wing(tenant_id=tenant, slug="company", name="Company")
        db.add(wing)
        await db.flush()
        room = Room(
            tenant_id=tenant,
            wing_id=wing.id,
            slug="product-decisions",
            stable_key=f"room:product-decisions:{tenant}",
            name="Product Decisions",
            snapshot_generation=3,
        )
        db.add(room)
        await db.commit()

        source = SyncSource(
            tenant_id=tenant,
            name="corpus",
            root_path=str(corpus.root),
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

        yield {"db": db, "session": session_factory, "tenant": tenant, "room": room,
               "source": source, "corpus": corpus}

    await engine.dispose()


async def _run(db, source) -> SyncRun:
    run = SyncRun(sync_source_id=source.id, tenant_id=source.tenant_id, status="queued")
    db.add(run)
    await db.commit()
    await db.refresh(run)
    await run_canon_sync(db, run=run, source=source, embedder=None, llm=None)
    await db.refresh(run)
    return run


async def _page(db, tenant, slug) -> CanonPage | None:
    return await db.scalar(
        select(CanonPage).where(CanonPage.tenant_id == tenant).where(CanonPage.slug == slug)
    )


async def _revisions(db, tenant, page) -> int:
    return int(
        await db.scalar(
            select(func.count()).select_from(CanonPageRevision)
            .where(CanonPageRevision.tenant_id == tenant)
            .where(CanonPageRevision.page_id == page.id)
        )
    )


@pytest.mark.asyncio
async def test_a_changed_file_creates_a_draft_and_publishes_nothing(env) -> None:
    db, tenant, source, corpus = env["db"], env["tenant"], env["source"], env["corpus"]
    corpus.write("company.md", "# Company\n\nWho we are.\n", section="identity")
    corpus.commit("seed")

    run = await _run(db, source)

    page = await _page(db, tenant, "company")
    assert run.pages_created == 1
    assert page.status == "draft"
    item = await db.scalar(select(Item).where(Item.id == page.item_id))
    assert item.status != "ready"


@pytest.mark.asyncio
async def test_a_sync_run_does_not_move_a_room_generation(env) -> None:
    db, tenant, source, corpus, room = env["db"], env["tenant"], env["source"], env["corpus"], env["room"]
    corpus.write("charter.md", "# Charter\n\nWhy this room exists.\n", section="charter", room="product-decisions")
    corpus.commit("seed")

    await _run(db, source)

    assert await db.scalar(select(Room.snapshot_generation).where(Room.id == room.id)) == 3
    charter = await _page(db, tenant, "charter")
    assert charter.room_id == room.id, "front matter room must bind the page to that room"


@pytest.mark.asyncio
async def test_a_deleted_file_archives_the_page(env) -> None:
    db, tenant, source, corpus = env["db"], env["tenant"], env["source"], env["corpus"]
    corpus.write("gone.md", "# Gone\n\nSoon removed.\n")
    corpus.commit("seed")
    await _run(db, source)
    before = await _page(db, tenant, "gone")

    corpus.remove("gone.md")
    corpus.commit("drop it")
    run = await _run(db, source)

    after = await _page(db, tenant, "gone")
    assert run.pages_archived == 1
    assert after is not None, "a canon page must never be deleted by a sync"
    assert after.id == before.id
    assert after.status == "archived"


@pytest.mark.asyncio
async def test_a_renamed_file_archives_the_old_slug_and_creates_the_new_one(env) -> None:
    db, tenant, source, corpus = env["db"], env["tenant"], env["source"], env["corpus"]
    corpus.write("old-name.md", "# Old\n\nSame content.\n")
    corpus.commit("seed")
    await _run(db, source)

    corpus.remove("old-name.md")
    corpus.write("new-name.md", "# New\n\nSame content.\n")
    corpus.commit("rename")
    await _run(db, source)

    assert (await _page(db, tenant, "old-name")).status == "archived"
    assert (await _page(db, tenant, "new-name")).status == "draft"


@pytest.mark.asyncio
async def test_a_hand_written_page_with_the_same_slug_conflicts(env) -> None:
    db, tenant, source, corpus = env["db"], env["tenant"], env["source"], env["corpus"]
    await create_canon_page(
        db, tenant_id=tenant, author_subject="andrew", slug="human-page",
        title="Human page", summary="By a person.", section="reference",
        body="# Human page\n\nWritten by a person.\n",
    )
    corpus.write("human-page.md", "# Human page\n\nWritten by a robot.\n", section="reference")
    corpus.commit("seed")

    run = await _run(db, source)

    page = await _page(db, tenant, "human-page")
    item = await db.scalar(select(Item).where(Item.id == page.item_id))
    assert run.pages_conflicted == 1
    assert "Written by a person" in item.raw_content
    surface = await db.scalar(
        select(CandidateCurationArtifact.target_surface)
        .where(CandidateCurationArtifact.tenant_id == tenant)
    )
    assert surface == "canon:human-page"


@pytest.mark.asyncio
async def test_an_unreachable_repo_fails_the_run_and_leaves_pages_untouched(env, tmp_path) -> None:
    db, tenant, source = env["db"], env["tenant"], env["source"]
    source.root_path = str(tmp_path / "not-a-repo")
    await db.commit()

    run = await _run(db, source)

    assert run.status == "failed"
    assert run.error_message
    assert await db.scalar(
        select(func.count()).select_from(CanonPage).where(CanonPage.tenant_id == tenant)
    ) == 0


@pytest.mark.asyncio
async def test_re_running_an_unchanged_repo_performs_zero_writes(env) -> None:
    db, tenant, source, corpus = env["db"], env["tenant"], env["source"], env["corpus"]
    corpus.write("stable.md", "# Stable\n\nNever changes.\n")
    corpus.commit("seed")
    await _run(db, source)
    page = await _page(db, tenant, "stable")
    revisions = await _revisions(db, tenant, page)

    run = await _run(db, source)

    assert (run.pages_created, run.pages_updated, run.pages_archived) == (0, 0, 0)
    assert await _revisions(db, tenant, page) == revisions


@pytest.mark.asyncio
async def test_a_run_over_the_caps_refuses_the_whole_batch(env, monkeypatch) -> None:
    db, tenant, source, corpus = env["db"], env["tenant"], env["source"], env["corpus"]
    corpus.write("one.md", "# One\n\nFirst.\n")
    corpus.write("two.md", "# Two\n\nSecond.\n")
    corpus.commit("seed")
    # The real cap is 500 files; the rule under test is that a refused batch
    # writes nothing at all, so the check itself is what gets tightened.
    def _refuse(**_kwargs):
        raise ValueError("batch exceeds the import caps")

    monkeypatch.setattr("app.services.canon_sync.check_import_batch_shape", _refuse)

    run = await _run(db, source)

    assert run.status == "failed"
    assert await db.scalar(
        select(func.count()).select_from(CanonPage).where(CanonPage.tenant_id == tenant)
    ) == 0, "a refused batch must not import a slice"


@pytest.mark.asyncio
async def test_a_repo_change_leaves_a_drifting_page_drifting(env) -> None:
    from app.services.canon_drift import list_canon_drift

    db, tenant, source, corpus, room = env["db"], env["tenant"], env["source"], env["corpus"], env["room"]
    corpus.write("charter.md", "# Charter\n\nWhy this room exists.\n", section="charter", room="product-decisions")
    corpus.commit("seed")
    await _run(db, source)

    room.snapshot_generation = 8
    await db.commit()
    corpus.write("charter.md", "# Charter\n\nWhy this room exists, restated.\n", section="charter", room="product-decisions")
    corpus.commit("rewrite")
    await _run(db, source)

    states = {row.slug: row.state for row in await list_canon_drift(db, tenant_id=tenant)}
    assert states["charter"] == "drift-suspected"


@pytest.mark.asyncio
async def test_a_canon_target_run_never_enqueues_a_palace_rebuild(env, monkeypatch) -> None:
    db, tenant, source, corpus = env["db"], env["tenant"], env["source"], env["corpus"]
    corpus.write("page.md", "# Page\n\nText.\n")
    corpus.commit("seed")

    async def _forbidden(*_args, **_kwargs):
        raise AssertionError("a canon run must never enqueue a palace rebuild")

    monkeypatch.setattr("app.services.palace.create_or_get_palace_run", _forbidden, raising=False)

    run = await _run(db, source)
    assert run.status == "completed"

@pytest.mark.asyncio
async def test_a_returning_file_revives_its_archived_page(env) -> None:
    """A file that comes back must bring its page back, history intact."""
    db, tenant, source, corpus = env["db"], env["tenant"], env["source"], env["corpus"]
    corpus.write("comes-back.md", "# Comes back\n\nHere for now.\n")
    corpus.commit("seed")
    await _run(db, source)
    original = await _page(db, tenant, "comes-back")

    corpus.remove("comes-back.md")
    corpus.commit("remove it")
    await _run(db, source)
    assert (await _page(db, tenant, "comes-back")).status == "archived"

    corpus.write("comes-back.md", "# Comes back\n\nHere again.\n")
    corpus.commit("restore it")
    await _run(db, source)

    revived = await _page(db, tenant, "comes-back")
    assert revived.id == original.id, "the page must be revived, not replaced"
    assert revived.status == "draft"
    assert await _revisions(db, tenant, revived) >= 1
