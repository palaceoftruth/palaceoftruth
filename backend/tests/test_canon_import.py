"""Canon folder import: parse, plan, and commit a folder of markdown files.

Three behaviours are easy to get wrong and each has a test here that fails if
the behaviour is removed, not if a function stops being called:

1. **The allowlist.** Unknown front-matter keys are preserved, never promoted to
   a column.
2. **The two phases.** A preview writes nothing; a commit refuses a plan the
   caller was not shown; a conflict is never applied unless the operator says so.
3. **Zero writes on a re-import.** An unchanged folder must leave every page's
   revisions and ``updated_at`` exactly where they were.

The session below is an in-memory stand-in for the two canon tables. It exists
because those behaviours are about what lands in storage: a session that only
recorded method calls could not tell a skipped file from an applied one.
"""

import copy
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.sql.elements import BindParameter, BooleanClauseList

from app import auth
from app.api import canon as canon_api
from app.database import get_db
from app.models.canon import CanonPage, CanonPageRevision
from app.models.item import Item
from app.services.canon import CanonCapExceeded, CanonValidationError
from app.services.canon_authoring import CanonConflictError
from app.services.canon_import import (
    check_import_upload,
    commit_import,
    parse_import_file,
    plan_id_for,
    plan_import,
    validate_import_batch,
)


# --- An in-memory stand-in for the canon tables -----------------------------


def _mapped_fields(obj) -> dict:
    """Every mapped column on an ORM instance, as plain values."""
    return {
        attribute.key: copy.deepcopy(getattr(obj, attribute.key))
        for attribute in sa_inspect(obj).mapper.column_attrs
    }


def _comparable(obj) -> dict:
    """Fields that decide whether a row was written, minus ``updated_at``."""
    fields = _mapped_fields(obj)
    fields.pop("updated_at", None)
    return fields


def _bound_values(statement) -> dict:
    """The values bound into a statement's WHERE clause, keyed by column name."""
    values: dict = {}

    def visit(clause) -> None:
        if clause is None:
            return
        if isinstance(clause, BooleanClauseList):
            for child in clause.clauses:
                visit(child)
            return
        left, right = getattr(clause, "left", None), getattr(clause, "right", None)
        key = getattr(left, "key", None)
        if key and isinstance(right, BindParameter):
            values[key] = right.value

    visit(getattr(statement, "whereclause", None))
    return values


class _Result:
    def __init__(self, rows) -> None:
        self._rows = list(rows)

    def all(self) -> list:
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return _Result([row[0] if isinstance(row, tuple) else row for row in self._rows])

    def __iter__(self):
        return iter(self._rows)


class MemorySession:
    """Enough of an async session to run canon_authoring and canon_import.

    It answers only the statements those modules issue, and raises on anything
    else, so a new query shape cannot pass silently through a permissive stub.
    """

    def __init__(self, *, tenant_id: str = "tenant-a") -> None:
        self.tenant_id = tenant_id
        self.pages: dict[str, CanonPage] = {}
        self.items: dict[uuid.UUID, Item] = {}
        self.revisions: list[CanonPageRevision] = []
        self.rooms: dict[uuid.UUID, object] = {}
        self.commits = 0
        self.rollbacks = 0
        self.flushes = 0
        self.plan_queries = 0
        self._pending: list = []
        self._clock = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        self._seen: dict[int, dict] = {}
        self._baseline = self._snapshot()

    # -- seeding ------------------------------------------------------------

    def add_page(
        self, *, slug: str, body: str, status: str = "published", section: str = "reference"
    ) -> CanonPage:
        now = self._tick()
        item = Item(
            id=uuid.uuid4(), tenant_id=self.tenant_id, source_type="canon",
            title=slug.replace("-", " ").title(), summary=f"Summary for {slug}",
            raw_content=body, metadata_={}, tags=["canon"], categories=[],
            status="ready" if status == "published" else "processing",
            created_at=now, updated_at=now,
        )
        page = CanonPage(
            id=uuid.uuid4(), tenant_id=self.tenant_id, item_id=item.id, slug=slug,
            section=section, summary=item.summary, priority=100, pinned_in_digest=False,
            status=status, char_count=len(body), created_at=now, updated_at=now,
        )
        self.items[item.id] = item
        self.pages[slug] = page
        self._seen[id(page)], self._seen[id(item)] = _comparable(page), _comparable(item)
        self._baseline = self._snapshot()
        return page

    def body_of(self, slug: str) -> str:
        return self.items[self.pages[slug].item_id].raw_content

    def revision_count_of(self, slug: str) -> int:
        page_id = self.pages[slug].id
        return sum(1 for revision in self.revisions if revision.page_id == page_id)

    # -- session surface ----------------------------------------------------

    def add(self, obj) -> None:
        self._pending.append(obj)

    async def flush(self) -> None:
        self.flushes += 1
        now = self._tick()
        for obj in self._pending:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()
            if isinstance(obj, Item):
                obj.created_at = obj.created_at or now
                obj.updated_at = obj.updated_at or now
                self.items[obj.id] = obj
            elif isinstance(obj, CanonPage):
                obj.created_at = obj.created_at or now
                obj.updated_at = obj.updated_at or now
                self.pages[obj.slug] = obj
            elif isinstance(obj, CanonPageRevision):
                obj.created_at = obj.created_at or now
                self.revisions.append(obj)
            else:
                raise AssertionError(f"the fake session was asked to store {type(obj).__name__}")
            self._seen[id(obj)] = _comparable(obj)
        self._pending.clear()
        self._stamp_changes(now)

    async def commit(self) -> None:
        self.commits += 1
        self._baseline = self._snapshot()

    async def rollback(self) -> None:
        self.rollbacks += 1
        self._restore(self._baseline)

    async def get(self, model, identity):
        if model.__name__ == "Room":
            return self.rooms.get(identity)
        raise AssertionError(f"the fake session was asked for a {model.__name__}")

    async def scalar(self, statement):
        sql = str(statement)
        values = _bound_values(statement)
        if "max(" in sql and "canon_page_revisions" in sql:
            numbers = [
                revision.revision
                for revision in self.revisions
                if revision.page_id == values.get("page_id")
            ]
            return max(numbers) if numbers else None
        if "canon_pages.id" in sql and "JOIN items" not in sql:
            page = self.pages.get(values.get("slug"))
            return page.id if page is not None else None
        raise AssertionError(f"the fake session got an unexpected scalar query:\n{sql}")

    async def scalars(self, statement):
        return (await self.execute(statement)).scalars()

    async def execute(self, statement):
        sql = str(statement)
        values = _bound_values(statement)
        wanted = values.get("slug")
        slugs = wanted if isinstance(wanted, list) else ([wanted] if wanted else [])

        if "JOIN items" in sql:
            if "items.id AS id_1" in sql:
                page = self.pages.get(values.get("slug"))
                if page is None or page.tenant_id != self.tenant_id:
                    return _Result([])
                return _Result([(page, self.items[page.item_id])])
            self.plan_queries += 1
            rows = [
                (self.pages[slug].slug, self.pages[slug].status, self.body_of(slug))
                for slug in slugs
                if slug in self.pages and self.pages[slug].tenant_id == self.tenant_id
            ]
            return _Result(rows)

        if "FROM canon_pages" in sql:
            if sql.startswith("SELECT canon_pages.id"):
                page = self.pages.get(values.get("slug"))
                return _Result([(page.id,)] if page is not None else [])
            if sql.startswith("SELECT canon_pages.slug"):
                return _Result([(slug,) for slug in slugs if slug in self.pages])
            raise AssertionError(f"the fake session got an unexpected canon_pages query:\n{sql}")

        if "FROM rooms" in sql:
            return _Result(
                [(slug, room_id) for slug, room_id in self._room_rows().items() if slug in slugs]
            )

        if sql.startswith("DELETE FROM room_memberships"):
            # The importer holds no charter memberships; the delete is a no-op here.
            return _Result([])

        raise AssertionError(f"the fake session got an unexpected query:\n{sql}")

    # -- storage bookkeeping ------------------------------------------------

    def _room_rows(self) -> dict[str, uuid.UUID]:
        return {slug: room.id for slug, room in self.rooms.items()}

    def _tick(self) -> datetime:
        self._clock += timedelta(seconds=1)
        return self._clock

    def _snapshot(self):
        return (
            {slug: _mapped_fields(page) for slug, page in self.pages.items()},
            {item_id: _mapped_fields(item) for item_id, item in self.items.items()},
            len(self.revisions),
        )

    def _restore(self, snapshot) -> None:
        page_fields, item_fields, revision_count = snapshot
        self.pages = {slug: page for slug, page in self.pages.items() if slug in page_fields}
        self.items = {item_id: item for item_id, item in self.items.items() if item_id in item_fields}
        del self.revisions[revision_count:]
        for slug, fields in page_fields.items():
            for name, value in fields.items():
                setattr(self.pages[slug], name, copy.deepcopy(value))
        for item_id, fields in item_fields.items():
            for name, value in fields.items():
                setattr(self.items[item_id], name, copy.deepcopy(value))
        self._pending.clear()
        for obj in [*self.pages.values(), *self.items.values()]:
            self._seen[id(obj)] = _comparable(obj)

    def _stamp_changes(self, now: datetime) -> None:
        for obj in [*self.pages.values(), *self.items.values()]:
            current = _comparable(obj)
            if self._seen.get(id(obj)) != current:
                obj.updated_at = now
                self._seen[id(obj)] = _comparable(obj)


# --- Fixtures and helpers --------------------------------------------------


def _file(path: str, body: str):
    return parse_import_file(path, body.encode())


def _front_matter_file(path: str, front: str, body: str):
    return parse_import_file(path, f"---\n{front}---\n\n{body}".encode())


def _folder():
    return [
        _file("deploy.md", "# Deploy\n\nDeploy is a checklist. It has steps.\n"),
        _file("rollback.mdx", "# Rollback\n\nRoll back before you panic.\n"),
        _file("notes.txt", "# Notes\n\nNotes are not canon yet.\n"),
    ]


# --- 1. The front-matter allowlist ----------------------------------------


def test_front_matter_allowlist_keeps_known_keys_and_stashes_the_rest() -> None:
    file = _front_matter_file(
        "release.md",
        "title: Release notes\nsection: runbook\ntags: [release, ops]\nrelated: [deploy]\nweird: 1\n",
        "Body text for the release.\n",
    )

    assert file.title == "Release notes"
    assert file.section == "runbook"
    assert file.tags == ["release", "ops"]
    assert file.related == ["deploy"]
    assert file.extra == {"weird": 1}


@pytest.mark.asyncio
async def test_an_unknown_front_matter_key_never_becomes_a_column() -> None:
    session = MemorySession()
    files = [
        _front_matter_file(
            "release.md",
            "title: Release notes\nsection: runbook\nweird: 1\n",
            "Body text for the release.\n",
        )
    ]

    plan = await plan_import(session, tenant_id="tenant-a", files=files)
    await commit_import(
        session, tenant_id="tenant-a", author_subject="human-1", files=files,
        plan_id=plan.plan_id, decisions={},
    )

    item = session.items[session.pages["release"].item_id]
    assert session.pages["release"].section == "runbook"
    assert item.metadata_["canon_extra"] == {"weird": 1}
    assert "weird" not in {attribute.key for attribute in sa_inspect(item).mapper.column_attrs}
    assert getattr(item, "weird", None) is None


@pytest.mark.asyncio
async def test_front_matter_tags_and_related_follow_the_file_on_reimport() -> None:
    session = MemorySession()
    first = [
        _front_matter_file(
            "release.md",
            "title: Release notes\ntags: [release, ops]\nrelated: [deploy, rollback]\n",
            "# Release notes\n\nFirst body.\n",
        )
    ]
    plan = await plan_import(session, tenant_id="tenant-a", files=first)
    await commit_import(
        session, tenant_id="tenant-a", author_subject="human-1", files=first,
        plan_id=plan.plan_id, decisions={},
    )

    item = session.items[session.pages["release"].item_id]
    assert item.tags == ["canon", "canon-section-reference", "release", "ops"]
    assert item.metadata_["canon_related"] == ["deploy", "rollback"]

    second = [
        _front_matter_file(
            "release.md", "title: Release notes\ntags: [release]\n", "# Release notes\n\nSecond body.\n"
        )
    ]
    reimport = await plan_import(session, tenant_id="tenant-a", files=second)
    results = await commit_import(
        session, tenant_id="tenant-a", author_subject="human-1", files=second,
        plan_id=reimport.plan_id, decisions={},
    )

    assert results[0].action == "updated"
    item = session.items[session.pages["release"].item_id]
    assert item.tags == ["canon", "canon-section-reference", "release"], (
        "a tag dropped from the file must not linger on the page"
    )


# --- 2. Derivation when the front matter is absent ------------------------


def test_missing_values_are_derived_from_the_file() -> None:
    file = _file(
        "deploy_checklist.md",
        "\n\n# Deploy checklist\n\nDeploy is a checklist. It has steps.\n",
    )

    assert file.title == "Deploy checklist"
    assert file.slug == "deploy-checklist"
    assert file.summary == "Deploy is a checklist."
    assert file.section == "reference"
    assert file.extra == {}


def test_a_file_with_no_heading_takes_its_title_from_the_filename() -> None:
    file = _file("run_book_v2.md", "Prose with no heading. Second sentence.\n")

    assert file.title == "Run Book V2"
    assert file.slug == "run-book-v2"
    assert file.summary == "Prose with no heading."


def test_the_body_excludes_the_front_matter_and_its_hash_is_byte_exact() -> None:
    with_front = _front_matter_file("guide.md", "title: Guide\n", "# Guide\n\nBody.\n")
    without_front = _file("guide.md", "# Guide\n\nBody.\n")
    edited = _file("guide.md", "# Guide\n\nBody!\n")

    assert with_front.body == "# Guide\n\nBody.\n"
    assert "title: Guide" not in with_front.body
    assert with_front.content_hash == without_front.content_hash
    assert edited.content_hash != without_front.content_hash


# --- 3. Accepted and refused file types -----------------------------------


@pytest.mark.parametrize("path", ["guide.md", "guide.mdx", "guide.txt"])
def test_md_mdx_and_txt_all_parse(path: str) -> None:
    file = parse_import_file(path, b"# Guide\n\nBody.\n")

    assert file.body == "# Guide\n\nBody.\n"
    assert file.slug == "guide"


def test_a_pdf_is_refused_by_name() -> None:
    with pytest.raises(CanonValidationError) as excinfo:
        check_import_upload("handbook.pdf", 10)
    assert "handbook.pdf" in str(excinfo.value)
    assert ".pdf" in str(excinfo.value)

    parsed_pdf = parse_import_file("handbook.pdf", b"%PDF-1.4 not really")
    with pytest.raises(CanonValidationError) as excinfo:
        validate_import_batch([parsed_pdf])
    assert "handbook.pdf" in str(excinfo.value)


# --- 4. Caps ---------------------------------------------------------------


def test_more_than_500_files_is_refused() -> None:
    files = [_file(f"note-{index}.md", "# Note\n\nBody.\n") for index in range(501)]

    with pytest.raises(CanonCapExceeded) as excinfo:
        validate_import_batch(files)

    assert "501" in str(excinfo.value)


def test_a_file_over_250_kilobytes_is_refused() -> None:
    big = b"# Big\n\n" + b"x" * 251_000
    files = [parse_import_file("big.md", big)]

    with pytest.raises(CanonCapExceeded) as excinfo:
        validate_import_batch(files)

    assert "big.md" in str(excinfo.value)
    assert str(len(big)) in str(excinfo.value)


def test_a_total_over_5_megabytes_is_refused() -> None:
    chunk = b"x" * 249_000
    files = [parse_import_file(f"part-{index}.md", chunk) for index in range(21)]
    assert all(file.size_bytes <= 250_000 for file in files), "the per-file cap must not fire first"

    with pytest.raises(CanonCapExceeded) as excinfo:
        validate_import_batch(files)

    assert str(sum(file.size_bytes for file in files)) in str(excinfo.value)


# --- 5. Plan actions -------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_actions_cover_create_skip_conflict_and_update() -> None:
    session = MemorySession()
    session.add_page(slug="already-there", body="# Already there\n\nSame body.\n")
    session.add_page(slug="live-page", body="# Live page\n\nPublished body.\n", status="published")
    session.add_page(slug="draft-page", body="# Draft page\n\nDraft body.\n", status="draft")

    files = [
        _file("brand-new.md", "# Brand new\n\nFresh page.\n"),
        _file("already-there.md", "# Already there\n\nSame body.\n"),
        _file("live-page.md", "# Live page\n\nEdited body.\n"),
        _file("draft-page.md", "# Draft page\n\nEdited draft body.\n"),
    ]

    plan = await plan_import(session, tenant_id="tenant-a", files=files)

    actions = {entry.slug: entry.action for entry in plan.files}
    assert actions == {
        "brand-new": "create",
        "already-there": "skip-unchanged",
        "live-page": "conflict",
        "draft-page": "update",
    }
    assert plan.counts == {"create": 1, "update": 1, "skip-unchanged": 1, "conflict": 1}
    assert {entry.slug: entry.existing_status for entry in plan.files} == {
        "brand-new": None,
        "already-there": "published",
        "live-page": "published",
        "draft-page": "draft",
    }
    assert all(entry.reason for entry in plan.files), "every action needs a human-readable reason"
    assert session.plan_queries == 1, "the plan must read the batch in one query, not one per file"
    assert session.pages["live-page"].status == "published", "planning must not write"


# --- 6. plan_id is content-addressed --------------------------------------


def test_plan_id_is_stable_for_a_folder_and_moves_when_one_byte_moves() -> None:
    folder = _folder()
    reordered = list(reversed(folder))

    assert plan_id_for(folder) == plan_id_for(list(folder))
    assert plan_id_for(folder) == plan_id_for(reordered), "upload order must not change the plan"

    edited = [
        folder[0],
        _file("notes.txt", "# Notes\n\nNotes are not canon yet!\n"),
        folder[2],
    ]
    assert plan_id_for(edited) != plan_id_for(folder)


@pytest.mark.asyncio
async def test_plan_id_from_the_service_matches_the_content_addressed_helper() -> None:
    session = MemorySession()
    folder = _folder()

    plan = await plan_import(session, tenant_id="tenant-a", files=folder)

    assert plan.plan_id == plan_id_for(folder)


# --- 7. A stale plan is refused -------------------------------------------


@pytest.mark.asyncio
async def test_a_stale_plan_id_is_refused_and_nothing_is_written() -> None:
    session = MemorySession()
    session.add_page(slug="draft-page", body="# Draft page\n\nOld body.\n", status="draft")
    previewed = [_file("draft-page.md", "# Draft page\n\nBody the operator saw.\n")]
    plan = await plan_import(session, tenant_id="tenant-a", files=previewed)

    changed = [_file("draft-page.md", "# Draft page\n\nBody the operator never saw.\n")]
    with pytest.raises(CanonConflictError) as excinfo:
        await commit_import(
            session, tenant_id="tenant-a", author_subject="human-1", files=changed,
            plan_id=plan.plan_id, decisions={},
        )

    assert "changed between preview and commit" in str(excinfo.value)
    assert session.body_of("draft-page") == "# Draft page\n\nOld body.\n"
    assert session.commits == 0
    assert session.rollbacks == 0, "a refused plan must not even open a write"


# --- 8. Decisions on a conflict -------------------------------------------


@pytest.mark.asyncio
async def test_apply_rename_and_skip_do_what_the_operator_asked() -> None:
    session = MemorySession()
    for slug in ("apply-me", "rename-me", "skip-me"):
        session.add_page(slug=slug, body=f"# {slug}\n\nOriginal body.\n", status="published")

    files = [
        _file("apply-me.md", "# Apply me\n\nOverwritten body.\n"),
        _file("rename-me.md", "# Rename me\n\nRenamed body.\n"),
        _file("skip-me.md", "# Skip me\n\nNever written.\n"),
    ]
    revisions_before = session.revision_count_of("apply-me")
    plan = await plan_import(session, tenant_id="tenant-a", files=files)
    assert {entry.action for entry in plan.files} == {"conflict"}

    results = await commit_import(
        session, tenant_id="tenant-a", author_subject="human-1", files=files,
        plan_id=plan.plan_id,
        decisions={"apply-me": "apply", "rename-me": "rename", "skip-me": "skip"},
    )

    by_slug = {result.slug: result for result in results}
    assert by_slug["apply-me"].action == "updated"
    assert by_slug["rename-me"].action == "renamed"
    assert by_slug["rename-me"].new_slug == "rename-me-2"
    assert by_slug["skip-me"].action == "skipped"

    assert session.body_of("apply-me") == "# Apply me\n\nOverwritten body.\n"
    # Overwriting published canon demotes it, exactly as any other body edit does.
    assert session.pages["apply-me"].status == "draft"
    assert session.revision_count_of("apply-me") == revisions_before + 1

    assert session.body_of("rename-me") == "# rename-me\n\nOriginal body.\n"
    assert session.body_of("rename-me-2") == "# Rename me\n\nRenamed body.\n"
    assert session.pages["rename-me-2"].status == "draft"

    assert session.body_of("skip-me") == "# skip-me\n\nOriginal body.\n"
    assert session.revision_count_of("skip-me") == 0
    assert session.commits == 1, "the whole batch is one transaction"


@pytest.mark.asyncio
async def test_rename_skips_slugs_that_are_already_taken() -> None:
    session = MemorySession()
    session.add_page(slug="collide", body="# Collide\n\nOriginal.\n")
    session.add_page(slug="collide-2", body="# Collide two\n\nSomeone got here first.\n")

    files = [_file("collide.md", "# Collide\n\nIncoming.\n")]
    plan = await plan_import(session, tenant_id="tenant-a", files=files)
    results = await commit_import(
        session, tenant_id="tenant-a", author_subject="human-1", files=files,
        plan_id=plan.plan_id, decisions={"collide": "rename"},
    )

    assert results[0].action == "renamed"
    assert results[0].new_slug == "collide-3"
    assert session.body_of("collide-2") == "# Collide two\n\nSomeone got here first.\n"
    assert session.body_of("collide-3") == "# Collide\n\nIncoming.\n"


# --- 9. Re-importing an unchanged folder writes nothing -------------------


@pytest.mark.asyncio
async def test_reimporting_an_unchanged_folder_writes_nothing() -> None:
    session = MemorySession()
    folder = _folder()

    first_plan = await plan_import(session, tenant_id="tenant-a", files=folder)
    await commit_import(
        session, tenant_id="tenant-a", author_subject="human-1", files=folder,
        plan_id=first_plan.plan_id, decisions={},
    )
    before_pages = {
        slug: (page.id, page.updated_at, session.revision_count_of(slug))
        for slug, page in session.pages.items()
    }
    before_items = {
        slug: (session.items[page.item_id].updated_at, session.body_of(slug))
        for slug, page in session.pages.items()
    }
    commits_before = session.commits

    second_plan = await plan_import(session, tenant_id="tenant-a", files=folder)
    assert {entry.action for entry in second_plan.files} == {"skip-unchanged"}
    results = await commit_import(
        session, tenant_id="tenant-a", author_subject="human-1", files=folder,
        plan_id=second_plan.plan_id, decisions={},
    )

    assert [result.action for result in results] == ["skipped", "skipped", "skipped"]
    assert session.commits == commits_before, "an unchanged folder must not even commit"
    for slug, (page_id, updated_at, revision_count) in before_pages.items():
        assert session.pages[slug].id == page_id
        assert session.pages[slug].updated_at == updated_at, f"{slug} was rewritten"
        assert session.revision_count_of(slug) == revision_count, f"{slug} gained a revision"
    for slug, (item_updated_at, body) in before_items.items():
        page = session.pages[slug]
        assert session.items[page.item_id].updated_at == item_updated_at
        assert session.body_of(slug) == body


# --- 10. A decision for a slug that is not in the plan --------------------


@pytest.mark.asyncio
async def test_a_decision_for_an_unknown_slug_is_refused() -> None:
    session = MemorySession()
    files = [_file("guide.md", "# Guide\n\nBody.\n")]
    plan = await plan_import(session, tenant_id="tenant-a", files=files)

    with pytest.raises(CanonConflictError) as excinfo:
        await commit_import(
            session, tenant_id="tenant-a", author_subject="human-1", files=files,
            plan_id=plan.plan_id, decisions={"not-in-this-folder": "apply"},
        )

    assert "not-in-this-folder" in str(excinfo.value)
    assert session.pages == {}, "nothing may be written when a decision is rejected"
    assert session.commits == 0


@pytest.mark.asyncio
async def test_an_unknown_decision_verb_is_a_validation_error() -> None:
    session = MemorySession()
    session.add_page(slug="guide", body="# Guide\n\nOld body.\n")
    files = [_file("guide.md", "# Guide\n\nNew body.\n")]
    plan = await plan_import(session, tenant_id="tenant-a", files=files)

    with pytest.raises(CanonValidationError) as excinfo:
        await commit_import(
            session, tenant_id="tenant-a", author_subject="human-1", files=files,
            plan_id=plan.plan_id, decisions={"guide": "maybe"},
        )

    assert "maybe" in str(excinfo.value)
    assert session.body_of("guide") == "# Guide\n\nOld body.\n"


# --- 11. A batch lands whole or not at all --------------------------------


@pytest.mark.asyncio
async def test_a_failure_part_way_through_rolls_the_whole_batch_back() -> None:
    """The importer is one transaction, so a bad file late in the folder leaves no pages."""
    session = MemorySession()
    files = [
        _file("first.md", "# First\n\nFine.\n"),
        _file("too-long.md", "# Too long\n\n" + "x" * 20_001),
        _file("last.md", "# Last\n\nAlso fine.\n"),
    ]
    plan = await plan_import(session, tenant_id="tenant-a", files=files)
    assert plan.counts["create"] == 3

    with pytest.raises(CanonCapExceeded):
        await commit_import(
            session, tenant_id="tenant-a", author_subject="human-1", files=files,
            plan_id=plan.plan_id, decisions={},
        )

    assert session.pages == {}, "a page from earlier in the batch must not survive the failure"
    assert session.items == {}
    assert session.revisions == []
    assert session.rollbacks == 1
    assert session.commits == 0


# --- The two routes -------------------------------------------------------
#
# The service is exercised for real here; only the session and the tenant
# principal are substituted, so these tests cover the upload reading, the JSON
# `decisions` field, and the error mapping as the browser sees them.


def _app(session: MemorySession, *, auth_mode: str = "browser_session") -> FastAPI:
    app = FastAPI()
    app.include_router(canon_api.router, prefix="/api/v1")
    app.state.embedder = object()
    app.state.llm = object()

    async def fake_verify_memory_auth(request: Request) -> str:
        auth._attach_auth_context(
            request,
            auth._context_from_scopes(
                tenant_id="tenant-a",
                auth_mode=auth_mode,
                subject_id="subject-1",
                token_hash_reference="hash-1",
                scopes=["read", "write"],
                audit_metadata={"api_key_id": "key-1"},
            ),
        )
        return ""

    async def fake_db():
        yield session

    app.dependency_overrides[auth.verify_memory_auth] = fake_verify_memory_auth
    app.dependency_overrides[get_db] = fake_db
    return app


def _client(session: MemorySession, *, auth_mode: str = "browser_session") -> TestClient:
    return TestClient(_app(session, auth_mode=auth_mode), base_url="https://testserver")


def _upload(path: str, body: bytes) -> tuple:
    return ("files", (path, body, "text/markdown"))


def test_preview_then_commit_through_the_routes() -> None:
    session = MemorySession()
    client = _client(session)
    upload = [_upload("guide.md", b"# Guide\n\nBody of the guide.\n")]

    preview = client.post("/api/v1/canon/import/folder/preview", files=upload)

    assert preview.status_code == 200, preview.text
    plan_body = preview.json()
    assert plan_body["files"][0]["action"] == "create"
    assert plan_body["files"][0]["slug"] == "guide"
    assert plan_body["counts"]["create"] == 1
    assert session.pages == {}, "a preview writes nothing"

    commit = client.post(
        "/api/v1/canon/import/folder/commit",
        files=upload,
        data={"plan_id": plan_body["plan_id"], "decisions": "{}"},
    )

    assert commit.status_code == 200, commit.text
    committed = commit.json()
    assert committed["applied"] == 1
    assert committed["skipped"] == 0
    assert committed["results"][0]["slug"] == "guide"
    assert committed["results"][0]["action"] == "created"
    assert session.pages["guide"].status == "draft"
    assert session.items[session.pages["guide"].item_id].governance_verification_state == "verified"
    assert session.items[session.pages["guide"].item_id].governance_owner_subject == "api_key:key-1"


def test_malformed_decisions_json_is_a_422_not_a_500() -> None:
    client = _client(MemorySession())

    response = client.post(
        "/api/v1/canon/import/folder/commit",
        files=[_upload("guide.md", b"# Guide\n\nBody.\n")],
        data={"plan_id": "unused", "decisions": "{not json"},
    )

    assert response.status_code == 422, response.text
    assert "decisions" in response.json()["detail"]


def test_a_pdf_upload_is_refused_with_its_name_over_http() -> None:
    client = _client(MemorySession())

    response = client.post(
        "/api/v1/canon/import/folder/preview",
        files=[_upload("handbook.pdf", b"%PDF-1.4 not really")],
    )

    assert response.status_code == 422, response.text
    assert "handbook.pdf" in response.json()["detail"]


def test_an_agent_cannot_import_a_folder() -> None:
    client = _client(MemorySession(), auth_mode="mcp_oauth")

    response = client.post(
        "/api/v1/canon/import/folder/preview",
        files=[_upload("guide.md", b"# Guide\n\nBody.\n")],
    )

    assert response.status_code == 403, response.text
    assert response.json()["detail"]["error"] == "canon_human_author_only"