"""The per-page revision count the canon list shows.

The list used to report ``revision_count = 0`` for every page, because the count
lives in the revisions table and only the detail view ever read it. A page with
three revisions displayed "rev 0" — the API was not lying about the schema, it
was lying about the page.

These tests pin the helper's contract without a database: one grouped query, a
dict keyed by page id, and absent keys rather than zeros for pages that have no
revisions.
"""

import uuid

import pytest

from app.services.canon_authoring import canon_revision_counts


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


class _FakeSession:
    """Records the statements it is asked to run and replays fixed rows."""

    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _FakeResult(self.rows)


@pytest.mark.asyncio
async def test_counts_are_keyed_by_page_id() -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    session = _FakeSession([(first, 3), (second, 2)])

    counts = await canon_revision_counts(
        session, tenant_id="tenant-a", page_ids=[first, second]
    )

    assert counts == {first: 3, second: 2}


@pytest.mark.asyncio
async def test_a_page_with_no_revisions_is_absent_rather_than_zero() -> None:
    known, unrevised = uuid.uuid4(), uuid.uuid4()
    session = _FakeSession([(known, 4)])

    counts = await canon_revision_counts(
        session, tenant_id="tenant-a", page_ids=[known, unrevised]
    )

    assert counts[known] == 4
    assert unrevised not in counts, "a missing key must not look like a real zero"
    assert counts.get(unrevised, 0) == 0


@pytest.mark.asyncio
async def test_counts_are_ints_not_database_scalars() -> None:
    page = uuid.uuid4()

    class _Scalarish:
        def __int__(self):
            return 7

    session = _FakeSession([(page, _Scalarish())])

    counts = await canon_revision_counts(session, tenant_id="tenant-a", page_ids=[page])

    assert counts[page] == 7
    assert isinstance(counts[page], int)


@pytest.mark.asyncio
async def test_no_pages_means_no_query() -> None:
    session = _FakeSession([])

    counts = await canon_revision_counts(session, tenant_id="tenant-a", page_ids=[])

    assert counts == {}
    assert session.statements == [], "an empty list must not reach the database"


@pytest.mark.asyncio
async def test_one_query_for_the_whole_page() -> None:
    pages = [uuid.uuid4() for _ in range(20)]
    session = _FakeSession([(page, 1) for page in pages])

    await canon_revision_counts(session, tenant_id="tenant-a", page_ids=pages)

    assert len(session.statements) == 1, (
        "the count must be one grouped query, not one per page; a per-page count "
        "turns a 200 row list into 200 round trips"
    )
