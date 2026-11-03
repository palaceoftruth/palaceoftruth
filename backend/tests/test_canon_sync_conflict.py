"""Reading a conflict and accepting the repo's text.

The promise these tests keep: a conflict shows both sides truthfully, a path can
only address the binding's own checkout, and accepting the repo's text ends the
conflict for good without publishing anything.

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
from app.models.item import Item
from app.models.palace import SyncRun, SyncSource, SyncSourceFile, Wing
from app.services.canon import CanonValidationError
from app.services.canon_history import list_canon_bindings
from app.services.canon_sync import (
    accept_repo_version,
    read_conflict_pair,
    run_canon_sync,
)

DATABASE_URL = os.environ.get("CANON_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="CANON_TEST_DATABASE_URL is not set; conflicts need a real database",
)

PAGE_BODY = "# Naming\n\nOur names are written by a person.\n"


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
    return root


def _write(root: Path, name: str, body: str) -> None:
    (root / "canon" / name).write_text(
        f'---\ntitle: "{name}"\nsummary: "About {name}."\nsection: rules\n---\n{body}'
    )


def _commit(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


@pytest_asyncio.fixture
async def env(tmp_path):
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"canon-conflict-{uuid.uuid4().hex[:10]}"
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

        # A page a person wrote, with no ownership row: exactly what a run
        # refuses to overwrite.
        item = Item(
            tenant_id=tenant,
            source_type="canon",
            source_url=f"canon://naming/{uuid.uuid4().hex}",
            title="Naming",
            raw_content=PAGE_BODY,
            status="ready",
        )
        db.add(item)
        await db.flush()
        page = CanonPage(
            tenant_id=tenant,
            item_id=item.id,
            slug="naming",
            summary="How we name things.",
            section="rules",
            status="published",
            char_count=len(PAGE_BODY),
        )
        db.add(page)
        await db.commit()

        yield {"db": db, "tenant": tenant, "source": source, "root": root, "page": page}

    await engine.dispose()


@pytest.mark.asyncio
async def test_a_conflict_shows_both_sides(env) -> None:
    db, tenant, source, root = env["db"], env["tenant"], env["source"], env["root"]
    _write(root, "naming.md", "# Naming\n\nThe repo says this.\n")
    _commit(root, "seed")

    pair = await read_conflict_pair(
        db, tenant_id=tenant, source=source, relative_path="canon/naming.md"
    )

    assert pair["slug"] == "naming"
    assert "The repo says this." in pair["repo_text"]
    assert "written by a person" in pair["page_text"]
    assert pair["page_status"] == "published"
    assert pair["missing"] is False
    assert pair["file_hash"]


@pytest.mark.asyncio
async def test_a_file_gone_from_the_repo_is_reported_not_raised(env) -> None:
    db, tenant, source, root = env["db"], env["tenant"], env["source"], env["root"]
    # A path is known to a binding only after a run has seen it, so this page is
    # the binding's own: the run created it, and then the file disappeared.
    _write(root, "gone.md", "# Gone\n\nLeaving.\n")
    _commit(root, "seed")
    run = SyncRun(sync_source_id=source.id, tenant_id=tenant, status="queued")
    db.add(run)
    await db.commit()
    await db.refresh(run)
    await run_canon_sync(db, run=run, source=source, embedder=None, llm=None)
    await db.refresh(run)
    assert run.pages_created == 1

    (root / "canon" / "gone.md").unlink()
    _commit(root, "remove it")

    pair = await read_conflict_pair(
        db, tenant_id=tenant, source=source, relative_path="canon/gone.md"
    )

    assert pair["missing"] is True
    assert pair["repo_text"] == ""
    assert pair["page_text"], "the page still exists and still needs a decision"


@pytest.mark.asyncio
async def test_a_path_outside_the_checkout_is_refused(env, tmp_path) -> None:
    """A caller-supplied path may not reach a file outside the binding.

    The test would pass by accident if the only thing stopping it were a missing
    page, so the outside file is given a slug that a page already answers to: with
    the guard removed, its text comes back as the repo side of a real conflict.
    """
    db, tenant, source, root = env["db"], env["tenant"], env["source"], env["root"]
    _write(root, "naming.md", "# Naming\n\nText.\n")
    _commit(root, "seed")
    outside = tmp_path / "outside.md"
    outside.write_text(
        '---\ntitle: "Outside"\nsummary: "Outside."\nsection: rules\n---\nSECRET\n'
    )

    with pytest.raises(CanonValidationError) as excinfo:
        await read_conflict_pair(
            db, tenant_id=tenant, source=source, relative_path="../outside.md"
        )
    assert "no page is bound" in str(excinfo.value)

    # The same filename now has a page, and the refusal must still hold.
    item = Item(
        tenant_id=tenant,
        source_type="canon",
        source_url=f"canon://outside/{uuid.uuid4().hex}",
        title="Outside",
        raw_content="# Outside\n",
        status="ready",
    )
    db.add(item)
    await db.flush()
    db.add(
        CanonPage(
            tenant_id=tenant,
            item_id=item.id,
            slug="outside",
            summary="Outside.",
            section="rules",
            status="published",
            char_count=9,
        )
    )
    await db.commit()
    with pytest.raises(CanonValidationError):
        await read_conflict_pair(
            db, tenant_id=tenant, source=source, relative_path="../outside.md"
        )


@pytest.mark.asyncio
async def test_accepting_the_repo_text_updates_the_page_and_returns_it_to_draft(env) -> None:
    db, tenant, source, root = env["db"], env["tenant"], env["source"], env["root"]
    _write(root, "naming.md", "# Naming\n\nThe repo says this.\n")
    _commit(root, "seed")

    page = await accept_repo_version(
        db,
        tenant_id=tenant,
        source=source,
        relative_path="canon/naming.md",
        actor_subject="browser_session:andrew",
    )

    assert page.status == "draft", "accepting a file is not publishing a page"
    body = await db.scalar(select(Item.raw_content).where(Item.id == page.item_id))
    assert "The repo says this." in body


@pytest.mark.asyncio
async def test_accepting_records_ownership_so_the_next_run_is_quiet(env) -> None:
    db, tenant, source, root = env["db"], env["tenant"], env["source"], env["root"]
    _write(root, "naming.md", "# Naming\n\nThe repo says this.\n")
    _commit(root, "seed")
    await accept_repo_version(
        db,
        tenant_id=tenant,
        source=source,
        relative_path="canon/naming.md",
        actor_subject="browser_session:andrew",
    )

    row = await db.scalar(
        select(SyncSourceFile)
        .where(SyncSourceFile.tenant_id == tenant)
        .where(SyncSourceFile.relative_path == "canon/naming.md")
    )
    assert row is not None and row.page_slug == "naming"

    run = SyncRun(sync_source_id=source.id, tenant_id=tenant, status="queued")
    db.add(run)
    await db.commit()
    await db.refresh(run)
    await run_canon_sync(db, run=run, source=source, embedder=None, llm=None)
    await db.refresh(run)

    assert run.pages_conflicted == 0, "the conflict must not be filed twice"


@pytest.mark.asyncio
async def test_accepting_a_gone_file_is_refused(env) -> None:
    db, tenant, source, root = env["db"], env["tenant"], env["source"], env["root"]
    _write(root, "naming.md", "# Naming\n\nGone soon.\n")
    _commit(root, "seed")
    (root / "canon" / "naming.md").unlink()
    _commit(root, "remove it")

    with pytest.raises(CanonValidationError) as excinfo:
        await accept_repo_version(
            db,
            tenant_id=tenant,
            source=source,
            relative_path="canon/naming.md",
            actor_subject="browser_session:andrew",
        )
    assert "gone from the repo" in str(excinfo.value), (
        "a gone file must be refused by name, not by a generic validation failure"
    )


@pytest.mark.asyncio
async def test_another_tenant_cannot_read_the_conflict(env) -> None:
    db, source, root = env["db"], env["source"], env["root"]
    _write(root, "naming.md", "# Naming\n\nText.\n")
    _commit(root, "seed")

    with pytest.raises(CanonValidationError):
        await read_conflict_pair(
            db, tenant_id="someone-else", source=source, relative_path="canon/naming.md"
        )


@pytest.mark.asyncio
async def test_bindings_list_only_canon_sources(env) -> None:
    db, tenant, source = env["db"], env["tenant"], env["source"]
    room_source = SyncSource(
        tenant_id=tenant,
        name="rooms-only",
        root_path=str(env["root"] / "rooms-tree"),
        source_kind="folder",
        credential_type="none",
        status="active",
        scan_interval_seconds=900,
        target_kind="rooms",
    )
    db.add(room_source)
    await db.commit()

    rows = await list_canon_bindings(db, tenant_id=tenant)

    assert [row[0].id for row in rows] == [source.id]
