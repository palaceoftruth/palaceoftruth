"""The digest cache is keyed by page state plus budget, so it cannot serve stale text.

SAR-1531 acceptance: "Digest cache keyed by fingerprint of page state plus
budget." A cache that answers on a stale fingerprint is worse than no cache at
all, because the agent would read yesterday's canon with today's confidence.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.models.canon import CanonDigestCache, CanonPage
from app.services.canon import CanonDigestPage, build_canon_digest, get_canon_digest

_TENANT = "tenant-a"
_NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


class _Rows:
    def __init__(self, rows) -> None:
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """Serves page rows and records every statement the service sends."""

    def __init__(self, rows, *, cached: CanonDigestCache | None = None) -> None:
        self.rows = rows
        self.cached = cached
        self.statements: list[object] = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Rows(self.rows)

    async def get(self, model, key):
        if model is CanonDigestCache:
            return self.cached
        return None

    @property
    def writes(self) -> list[object]:
        return [
            statement
            for statement in self.statements
            if "canon_digest_cache" in str(statement).lower()
            and "insert" in str(statement).lower()
        ]


def _row(slug: str, *, summary: str, body: str, section: str = "reference") -> tuple:
    """One (CanonPage, raw_content) row exactly as ``load_canon_pages`` returns it."""
    page = CanonPage(
        id=uuid.uuid4(),
        tenant_id=_TENANT,
        item_id=uuid.uuid4(),
        slug=slug,
        section=section,
        summary=summary,
        priority=100,
        pinned_in_digest=False,
        status="published",
        char_count=len(body),
        updated_at=_NOW,
    )
    return (page, body)


def _digest_page(row: tuple) -> CanonDigestPage:
    page, body = row
    return CanonDigestPage(
        slug=page.slug,
        summary=page.summary,
        section=page.section,
        priority=page.priority,
        pinned_in_digest=page.pinned_in_digest,
        status=page.status,
        body=body,
        char_count=page.char_count,
        updated_at=page.updated_at,
    )


def _cached(fingerprint: str, *, text: str = "CACHED TEXT"):
    return CanonDigestCache(
        tenant_id=_TENANT,
        fingerprint=fingerprint,
        digest_text=text,
        char_count=len(text),
        page_count=1,
        built_at=_NOW,
    )


@pytest.mark.asyncio
async def test_cache_hit_returns_cached_text_and_writes_nothing() -> None:
    rows = [_row("company", summary="What we do.", body="We build software.")]
    digest = build_canon_digest([_digest_page(row) for row in rows])

    db = _FakeSession(rows, cached=_cached(digest.fingerprint))
    result = await get_canon_digest(db, tenant_id=_TENANT)

    assert result.text == "CACHED TEXT"
    assert result.fingerprint == digest.fingerprint
    assert db.writes == []


@pytest.mark.asyncio
async def test_stale_fingerprint_rebuilds_and_upserts() -> None:
    rows = [_row("company", summary="What we do.", body="We build software.")]

    db = _FakeSession(rows, cached=_cached("stale-fingerprint"))
    result = await get_canon_digest(db, tenant_id=_TENANT)

    assert result.text != "CACHED TEXT"
    # A reference page contributes its index line, not its body, to the digest.
    assert "company: What we do." in result.text
    assert len(db.writes) == 1


@pytest.mark.asyncio
async def test_no_cache_row_builds_and_writes() -> None:
    rows = [_row("company", summary="What we do.", body="We build software.")]

    db = _FakeSession(rows, cached=None)
    result = await get_canon_digest(db, tenant_id=_TENANT)

    assert result.page_count == 1
    assert len(db.writes) == 1


@pytest.mark.asyncio
async def test_a_changed_budget_is_not_served_from_cache() -> None:
    """Budget is part of the fingerprint, so a different cap is a different digest."""
    rows = [_row("company", summary="What we do.", body="We build software.")]
    default_digest = build_canon_digest([_digest_page(row) for row in rows])

    db = _FakeSession(rows, cached=_cached(default_digest.fingerprint))
    result = await get_canon_digest(db, tenant_id=_TENANT, budget_chars=1200)

    assert result.text != "CACHED TEXT"
    assert result.fingerprint != default_digest.fingerprint
    assert len(db.writes) == 1
