"""Canon drift detection: the machine proposes, the human ratifies.

Four behaviours carry the design section 8 rule, and each has a test here that
fails if the behaviour is removed rather than if a function stops being called:

1. **The rule itself.** A generation bump or more than ten new memberships makes
   a page drift-suspected; ten does not; a published page nobody ratified is
   ``stale`` instead, because that is a weaker and different problem.
2. **One artifact per page per generation.** A retry is a quiet no-op, and a
   second bump at a new generation keeps the older artifact so the history of
   drift survives.
3. **Detection never edits canon.** No page, item, revision, or room row moves;
   the session below refuses every non-SELECT statement, so a stray write is a
   failure rather than a silent pass.
4. **Ratifying or publishing clears the question.** The open artifact becomes
   ``promoted`` and a later detection does not treat it as open again.

The session is an in-memory stand-in for the five tables these paths touch. It
answers only the query shapes the service issues and raises on anything else, so
a new query cannot slip through a permissive stub.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy.sql.elements import BindParameter, BooleanClauseList

from app import auth
from app.api import canon as canon_api
from app.database import get_db
from app.models.canon import CanonPage, CanonPageRevision
from app.models.item import Item
from app.models.palace import (
    CandidateCurationArtifact,
    CandidateCurationArtifactEvent,
    Room,
    RoomMembership,
)
from app.services.canon import CANON_DRIFT_MEMBERSHIP_THRESHOLD, CanonValidationError
from app.services.canon_authoring import publish_canon_page, ratify_canon_page
from app.services.canon_drift import (
    STATE_DRIFT_SUSPECTED,
    STATE_NONE,
    STATE_STALE,
    detect_canon_drift,
    drift_for_page,
    list_canon_drift,
)

_NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


# --- An in-memory stand-in for the tables these paths touch ----------------


def _where_values(statement) -> dict:
    """The values bound into a statement's WHERE clause, keyed by column name.

    A list value means an ``IN`` clause, which is the only other predicate shape
    the service uses.
    """
    values: dict = {}

    def visit(clause) -> None:
        if clause is None:
            return
        if isinstance(clause, BooleanClauseList):
            for child in clause.clauses:
                visit(child)
            return
        left, right = getattr(clause, "left", None), getattr(clause, "right", None)
        if isinstance(right, BindParameter) and getattr(left, "key", None):
            values[left.key] = right.value

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


class _Nested:
    """A savepoint stand-in; the outer transaction is the caller's business."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class MemorySession:
    """Enough of an async session to run canon_drift and the two writers."""

    def __init__(self, *, tenant_id: str = "tenant-a") -> None:
        self.tenant_id = tenant_id
        self.pages: dict[str, CanonPage] = {}
        self.items: dict[uuid.UUID, Item] = {}
        self.rooms: dict[uuid.UUID, Room] = {}
        self.memberships: list[RoomMembership] = []
        self.artifacts: list[CandidateCurationArtifact] = []
        self.events: list[CandidateCurationArtifactEvent] = []
        self.revisions: list[CanonPageRevision] = []
        self.commits = 0
        self.rollbacks = 0
        self._pending: list = []
        self._clock = _NOW

    # -- seeding ------------------------------------------------------------

    def add_room(
        self, *, slug: str = "engineering", generation: int = 0, membership_generation: int | None = None
    ) -> Room:
        room = Room(
            id=uuid.uuid4(),
            tenant_id=self.tenant_id,
            slug=slug,
            stable_key=slug,
            name=slug.title(),
            snapshot_generation=generation,
            membership_generation=generation if membership_generation is None else membership_generation,
        )
        self.rooms[room.id] = room
        return room

    def add_page(
        self,
        *,
        slug: str,
        room: Room | None = None,
        status: str = "published",
        confirmed_generation: int = 0,
        confirmed_at: datetime | None = None,
        created_at: datetime | None = None,
        section: str = "reference",
    ) -> CanonPage:
        created = created_at or _NOW
        item = Item(
            id=uuid.uuid4(), tenant_id=self.tenant_id, source_type="canon",
            title=slug.replace("-", " ").title(), summary=f"Summary for {slug}",
            raw_content=f"# {slug}\n\nBody of {slug}.\n", metadata_={},
            tags=["canon"], categories=[], status="ready", created_at=created, updated_at=created,
        )
        page = CanonPage(
            id=uuid.uuid4(), tenant_id=self.tenant_id, item_id=item.id,
            room_id=room.id if room is not None else None, slug=slug, section=section,
            summary=item.summary, priority=100, pinned_in_digest=False, status=status,
            last_confirmed_generation=confirmed_generation, last_confirmed_at=confirmed_at,
            char_count=len(item.raw_content), created_at=created, updated_at=created,
        )
        self.items[item.id] = item
        self.pages[slug] = page
        return page

    def add_revision(self, page: CanonPage) -> None:
        self.revisions.append(
            CanonPageRevision(
                id=uuid.uuid4(), tenant_id=self.tenant_id, page_id=page.id,
                revision=len(self.revisions) + 1, body_md="body", summary=page.summary,
                change_note="seed", created_at=_NOW,
            )
        )

    def add_member(self, room: Room, *, created_at: datetime, tags: list[str] | None = None) -> Item:
        """One membership, plus the item it points at so tag counts have a source."""
        item = Item(
            id=uuid.uuid4(), tenant_id=self.tenant_id, source_type="note",
            title="Member", summary="", raw_content="", metadata_={},
            tags=list(tags or []), categories=[], status="ready",
            created_at=created_at, updated_at=created_at,
        )
        self.items[item.id] = item
        self.memberships.append(
            RoomMembership(
                id=uuid.uuid4(), tenant_id=self.tenant_id, room_id=room.id,
                item_id=item.id, membership_kind="primary", source="auto",
                confidence=1.0, created_at=created_at, updated_at=created_at,
            )
        )
        return item

    def fill_room(self, room: Room, count: int, *, after: datetime | None = None) -> None:
        """Add ``count`` memberships, each a second later than the last."""
        start = (after or _NOW) + timedelta(seconds=1)
        for index in range(count):
            self.add_member(room, created_at=start + timedelta(seconds=index))

    # -- session surface ----------------------------------------------------

    def add(self, obj) -> None:
        self._pending.append(obj)

    def begin_nested(self):
        return _Nested()

    async def flush(self) -> None:
        for obj in self._pending:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()
            if isinstance(obj, CandidateCurationArtifact):
                self.artifacts.append(obj)
            elif isinstance(obj, CandidateCurationArtifactEvent):
                self.events.append(obj)
            elif isinstance(obj, CanonPageRevision):
                self.revisions.append(obj)
            else:
                raise AssertionError(f"the fake session was asked to store {type(obj).__name__}")
        self._pending.clear()

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def get(self, model, identity):
        if model is Room:
            return self.rooms.get(identity)
        raise AssertionError(f"the fake session was asked for a {model.__name__}")

    async def scalar(self, statement):
        row = (await self.execute(statement)).first()
        if isinstance(row, tuple):
            return row[0] if len(row) == 1 else row
        return row

    async def scalars(self, statement):
        return (await self.execute(statement)).scalars()

    async def execute(self, statement):
        sql = str(statement)
        assert sql.lstrip().upper().startswith("SELECT"), (
            f"detection must not write; it issued:\n{sql}"
        )
        values = _where_values(statement)
        entity = statement.column_descriptions[0].get("entity")

        if "max(" in sql and "canon_page_revisions" in sql:
            numbers = [
                revision.revision
                for revision in self.revisions
                if revision.page_id == values.get("page_id")
            ]
            return _Result([(max(numbers) if numbers else None,)])

        if entity is CanonPage:
            pages = [page for page in self.pages.values() if page.tenant_id == self.tenant_id]
            if "slug" in values:
                pages = [page for page in pages if page.slug == values["slug"]]
            if "room_id" in values:
                pages = [page for page in pages if page.room_id == values["room_id"]]
            if "room_id IS NOT NULL" in sql:
                pages = [page for page in pages if page.room_id is not None]
            return _Result([(page, self.items[page.item_id]) for page in pages])

        if entity is RoomMembership:
            rows = [
                membership
                for membership in self.memberships
                if membership.tenant_id == self.tenant_id
                and membership.room_id == values.get("room_id")
            ]
            return _Result([(row.item_id, row.created_at) for row in rows])

        if entity is Item:
            item_ids = {
                membership.item_id
                for membership in self.memberships
                if membership.room_id == values.get("room_id")
            }
            return _Result(
                [(item.id, item.tags) for item in self.items.values() if item.id in item_ids]
            )

        if entity is CandidateCurationArtifact:
            rows = [
                artifact for artifact in self.artifacts if artifact.tenant_id == self.tenant_id
            ]
            if "dedupe_key" in values:
                rows = [row for row in rows if row.dedupe_key == values["dedupe_key"]]
            if "artifact_kind" in values:
                rows = [row for row in rows if row.artifact_kind == values["artifact_kind"]]
            if "target_surface" in values:
                rows = [row for row in rows if row.target_surface == values["target_surface"]]
            if "status" in values:
                allowed = values["status"]
                allowed = allowed if isinstance(allowed, list) else [allowed]
                rows = [row for row in rows if row.status in allowed]
            return _Result(rows)

        raise AssertionError(f"the fake session got an unexpected query:\n{sql}")


# --- Helpers ---------------------------------------------------------------


def _drift(session: MemorySession, slug: str):
    """Evaluate one seeded page the way the service does."""
    page = session.pages[slug]
    return drift_for_page(session, page=page, item=session.items[page.item_id])


@pytest.mark.asyncio
async def test_a_settled_page_is_none() -> None:
    session = MemorySession()
    room = session.add_room(generation=4)
    session.add_page(slug="charter", room=room, confirmed_generation=4, confirmed_at=_NOW)
    session.fill_room(room, 2)

    drift = await _drift(session, "charter")

    assert drift.state == STATE_NONE
    assert drift.confirmed_generation == 4
    assert drift.current_generation == 4
    assert drift.new_memberships == 2
    assert drift.artifact_id is None


@pytest.mark.asyncio
async def test_a_generation_bump_is_drift_suspected_and_the_reason_names_the_gap() -> None:
    session = MemorySession()
    room = session.add_room(generation=2)
    session.add_page(slug="charter", room=room, confirmed_generation=2, confirmed_at=_NOW)
    session.fill_room(room, 1)
    room.snapshot_generation = 5

    drift = await _drift(session, "charter")

    assert drift.state == STATE_DRIFT_SUSPECTED
    assert drift.current_generation == 5
    assert "gap 3" in drift.reason
    assert "from generation 2 to 5" in drift.reason


@pytest.mark.asyncio
async def test_eleven_new_memberships_drift_but_ten_do_not() -> None:
    session = MemorySession()
    room = session.add_room(generation=1)
    session.add_page(slug="charter", room=room, confirmed_generation=1, confirmed_at=_NOW)
    session.fill_room(room, CANON_DRIFT_MEMBERSHIP_THRESHOLD)

    ten = await _drift(session, "charter")
    assert ten.state == STATE_NONE, "exactly the threshold is not over it"
    assert ten.new_memberships == CANON_DRIFT_MEMBERSHIP_THRESHOLD

    session.add_member(room, created_at=_NOW + timedelta(minutes=5))
    eleven = await _drift(session, "charter")

    assert eleven.state == STATE_DRIFT_SUSPECTED
    assert eleven.new_memberships == CANON_DRIFT_MEMBERSHIP_THRESHOLD + 1
    assert eleven.current_generation == 1, "the drift came from memberships, not a generation"
    assert str(CANON_DRIFT_MEMBERSHIP_THRESHOLD + 1) in eleven.reason


@pytest.mark.asyncio
async def test_a_published_never_ratified_page_reads_stale_not_drift_suspected() -> None:
    session = MemorySession()
    room = session.add_room(generation=7)
    session.add_page(slug="charter", room=room, status="published", confirmed_generation=0)
    session.fill_room(room, 3)

    drift = await _drift(session, "charter")

    assert drift.state == STATE_STALE, "an unratified page is weaker than a suspected drift"
    assert drift.confirmed_generation == 0
    assert "never ratified" in drift.reason


@pytest.mark.asyncio
async def test_an_unbound_page_is_none_and_never_files_an_artifact() -> None:
    session = MemorySession()
    session.add_page(slug="reference", room=None, confirmed_generation=0)
    session.add_page(slug="charter", room=session.add_room(generation=3), confirmed_generation=0)

    unbound = await _drift(session, "reference")
    summary = await detect_canon_drift(session, tenant_id="tenant-a")
    listed = await list_canon_drift(session, tenant_id="tenant-a")

    assert unbound.state == STATE_NONE
    assert unbound.artifact_id is None
    assert [page.slug for page in listed] == ["charter"], "an unbound page is not on the drift screen"
    assert summary.evaluated == 1
    assert all(artifact.target_surface != "canon:reference" for artifact in session.artifacts)


@pytest.mark.asyncio
async def test_detection_files_exactly_one_artifact_per_page_per_generation() -> None:
    session = MemorySession()
    room = session.add_room(generation=2)
    session.add_page(slug="charter", room=room, confirmed_generation=2, confirmed_at=_NOW)
    session.fill_room(room, 1)
    room.snapshot_generation = 3

    first = await detect_canon_drift(session, tenant_id="tenant-a")
    second = await detect_canon_drift(session, tenant_id="tenant-a")

    assert first.created == 1
    assert first.deduplicated == 0
    assert second.created == 0
    assert second.deduplicated == 1
    assert len(session.artifacts) == 1, "a retry must not file a second artifact"
    assert session.artifacts[0].dedupe_key == f"canon-drift:{session.pages['charter'].id}:3"


@pytest.mark.asyncio
async def test_a_second_bump_keeps_a_second_artifact_as_drift_history() -> None:
    session = MemorySession()
    room = session.add_room(generation=2)
    session.add_page(slug="charter", room=room, confirmed_generation=2, confirmed_at=_NOW)
    session.fill_room(room, 1)

    room.snapshot_generation = 3
    await detect_canon_drift(session, tenant_id="tenant-a")
    room.snapshot_generation = 4
    second_run = await detect_canon_drift(session, tenant_id="tenant-a")

    assert second_run.created == 1
    assert len(session.artifacts) == 2
    generations = {artifact.dedupe_key.rsplit(":", 1)[1] for artifact in session.artifacts}
    assert generations == {"3", "4"}


@pytest.mark.asyncio
async def test_detection_does_not_move_the_page_it_judges() -> None:
    session = MemorySession()
    room = session.add_room(generation=2)
    page = session.add_page(slug="charter", room=room, confirmed_generation=2, confirmed_at=_NOW)
    session.add_revision(page)
    session.add_revision(page)
    session.fill_room(room, 4)
    room.snapshot_generation = 9

    before = (
        page.updated_at,
        page.last_confirmed_generation,
        page.status,
        len(session.revisions),
        session.items[page.item_id].raw_content,
        room.snapshot_generation,
    )
    await detect_canon_drift(session, tenant_id="tenant-a")
    after = (
        page.updated_at,
        page.last_confirmed_generation,
        page.status,
        len(session.revisions),
        session.items[page.item_id].raw_content,
        room.snapshot_generation,
    )

    assert len(session.artifacts) == 1, "the artifact is the only thing detection writes"
    assert after == before, "detection edited canon"
    assert session.commits == 0, "the caller owns the transaction"


@pytest.mark.asyncio
async def test_the_artifact_body_states_every_measured_number_including_zero() -> None:
    session = MemorySession()
    room = session.add_room(generation=5)
    # Confirmed against the current generation, so the gap and the membership
    # count are both zero: the body must say so rather than drop the lines.
    session.add_page(slug="charter", room=room, confirmed_generation=5, confirmed_at=_NOW)
    session.fill_room(room, 3, after=_NOW - timedelta(days=1))
    session.add_member(room, created_at=_NOW - timedelta(days=1), tags=["ops"])
    room.snapshot_generation = 6

    await detect_canon_drift(session, tenant_id="tenant-a")
    artifact = session.artifacts[0]
    body = artifact.candidate_body

    assert "Room item count: 4." in body
    assert "New memberships since confirmation: 0." in body
    assert "Generation gap: 1 (confirmed generation 5, current generation 6)." in body
    assert "ops (1)" in body


@pytest.mark.asyncio
async def test_ratify_promotes_the_open_artifact_and_detection_leaves_it_closed() -> None:
    session = MemorySession()
    room = session.add_room(generation=3, membership_generation=3)
    page = session.add_page(
        slug="charter", room=room, status="published", confirmed_generation=1, confirmed_at=_NOW
    )

    await detect_canon_drift(session, tenant_id="tenant-a")
    artifact = session.artifacts[0]
    assert artifact.status == "reviewable"

    await ratify_canon_page(session, tenant_id="tenant-a", author_subject="human-1", slug="charter")

    assert artifact.status == "promoted"
    assert artifact.approved_by_principal == "human-1"
    assert page.last_confirmed_generation == 3

    again = await detect_canon_drift(session, tenant_id="tenant-a")
    assert again.created == 0
    assert artifact.status == "promoted", "a resolved artifact is not open again"

    # A later bump is a new question, with its own artifact.
    room.snapshot_generation = 4
    bumped = await detect_canon_drift(session, tenant_id="tenant-a")
    assert bumped.created == 1
    assert [row.status for row in session.artifacts] == ["promoted", "reviewable"]


@pytest.mark.asyncio
async def test_publish_also_promotes_the_open_artifact() -> None:
    session = MemorySession()
    room = session.add_room(generation=2, membership_generation=2)
    session.add_page(slug="charter", room=room, status="published", confirmed_generation=1, confirmed_at=_NOW)
    room.snapshot_generation = 5

    await detect_canon_drift(session, tenant_id="tenant-a")
    artifact = session.artifacts[0]
    assert artifact.status == "reviewable"

    await publish_canon_page(
        session,
        tenant_id="tenant-a",
        author_subject="human-1",
        slug="charter",
        embedder=None,
        llm=None,
    )

    assert artifact.status == "promoted"
    # The room's snapshot moved to 5 after the page last confirmed 1, which is what
    # made it drift. Publishing a human edit confirms the room as it stands now, so
    # the recorded watermark is 5. It used to record membership_generation (2), a
    # counter the drift check never reads, so publishing appeared to succeed and
    # left the page permanently drifting.
    assert session.pages["charter"].last_confirmed_generation == 5


@pytest.mark.asyncio
async def test_the_artifact_targets_the_page_surface_exactly() -> None:
    session = MemorySession()
    room = session.add_room(generation=1)
    session.add_page(slug="company-charter", room=room, confirmed_generation=1, confirmed_at=_NOW)
    room.snapshot_generation = 2

    await detect_canon_drift(session, tenant_id="tenant-a")

    artifact = session.artifacts[0]
    assert artifact.target_surface == "canon:company-charter"
    assert artifact.artifact_kind == "candidate_source_drift"
    assert artifact.target_runtime == "palace"
    assert artifact.status == "reviewable"
    assert artifact.evidence_diff == {
        "format": "canon_drift_v1",
        "confirmed_generation": 1,
        "current_generation": 2,
        "membership_gap": 0,
        "room_item_count": 0,
    }
    assert artifact.eval_summary["detector"] == "deterministic_canon_drift_v1"
    assert artifact.eval_summary["llm_used"] is False


@pytest.mark.asyncio
async def test_the_state_filter_is_closed_and_only_includes_none_on_request() -> None:
    session = MemorySession()
    room = session.add_room(generation=2)
    session.add_page(slug="settled", room=room, confirmed_generation=2, confirmed_at=_NOW)
    session.add_page(slug="drifted", room=room, confirmed_generation=1, confirmed_at=_NOW)

    everything = await list_canon_drift(session, tenant_id="tenant-a")
    drifted_only = await list_canon_drift(session, tenant_id="tenant-a", state=STATE_DRIFT_SUSPECTED)

    assert {page.slug: page.state for page in everything} == {
        "settled": STATE_NONE,
        "drifted": STATE_DRIFT_SUSPECTED,
    }
    assert [page.slug for page in drifted_only] == ["drifted"]

    with pytest.raises(CanonValidationError) as excinfo:
        await list_canon_drift(session, tenant_id="tenant-a", state="drifted")
    assert "none" in str(excinfo.value)
    assert "drift-suspected" in str(excinfo.value)
    assert "stale" in str(excinfo.value)


# --- The two routes -------------------------------------------------------
#
# The service is exercised for real here; only the session and the tenant
# principal are substituted, so these cover the query filter and the error
# mapping as a browser sees them.


def _client(session: MemorySession, *, auth_mode: str = "browser_session") -> TestClient:
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
    return TestClient(app, base_url="https://testserver")


def test_the_drift_read_lists_the_page_over_http() -> None:
    session = MemorySession()
    room = session.add_room(generation=1)
    session.add_page(slug="charter", room=room, confirmed_generation=1, confirmed_at=_NOW)
    room.snapshot_generation = 3

    response = _client(session).get("/api/v1/canon/drift")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pages"][0]["slug"] == "charter"
    assert body["pages"][0]["state"] == STATE_DRIFT_SUSPECTED
    assert body["counts"] == {STATE_NONE: 0, STATE_DRIFT_SUSPECTED: 1, STATE_STALE: 0}


def test_an_unknown_state_filter_is_a_422_naming_the_allowed_values() -> None:
    response = _client(MemorySession()).get("/api/v1/canon/drift?state=drifted")

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    for allowed in ("none", "drift-suspected", "stale"):
        assert allowed in detail


def test_detection_over_http_files_the_artifact() -> None:
    session = MemorySession()
    room = session.add_room(generation=1)
    session.add_page(slug="charter", room=room, confirmed_generation=1, confirmed_at=_NOW)
    room.snapshot_generation = 2

    response = _client(session).post("/api/v1/canon/drift/detect")

    assert response.status_code == 200, response.text
    assert response.json()["created"] == 1
    assert len(session.artifacts) == 1
    assert session.commits == 1, "the route owns the transaction"


def test_an_agent_cannot_run_detection() -> None:
    response = _client(MemorySession(), auth_mode="mcp_oauth").post("/api/v1/canon/drift/detect")

    assert response.status_code == 403, response.text
    assert response.json()["detail"]["error"] == "canon_human_author_only"


# --- the watermark ratify records -------------------------------------------
#
# Ratify stores a room counter into `canon_pages.last_confirmed_generation`, and
# the drift check compares that value with the room's own counter. If the two
# read different columns the round trip is silently broken: ratify reports
# success, the page keeps its old number, and the pill never clears. It happened
# against a live database with snapshot_generation=5 and membership_generation=0,
# which is why this test exists at all.

@pytest.mark.asyncio
async def test_ratify_records_the_snapshot_generation_not_the_membership_one() -> None:
    from app.services.canon_authoring import _room_generation

    class _Room:
        snapshot_generation = 5
        membership_generation = 0
        closet_generation = 0
        tunnel_generation = 0

    class _Session:
        async def get(self, _model, _pk):
            return _Room()

    assert await _room_generation(_Session(), room_id=uuid.uuid4()) == 5, (
        "ratify must record the generation the drift check compares against"
    )


@pytest.mark.asyncio
async def test_an_unbound_page_has_no_generation_to_confirm() -> None:
    from app.services.canon_authoring import _room_generation

    class _Session:
        async def get(self, _model, _pk):  # pragma: no cover - must not run
            raise AssertionError("an unbound page must not query a room")

    assert await _room_generation(_Session(), room_id=None) == 0
