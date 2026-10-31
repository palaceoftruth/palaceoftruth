"""Canon read paths: digest assembly, page slicing, search, and every cap.

Design section 11 puts all canon budgets in this module and nowhere else, so a
cap can be changed in one place. Every capped output reports ``truncated`` plus
a ``next`` hint; silent truncation is a bug. The pure functions here are the
acceptance surface: page value objects in, value objects out, no database.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canon import CANON_SECTIONS, CANON_STATUSES, CanonDigestCache, CanonPage
from app.models.item import Item


# --- Caps (design section 11). This module is the only place they live. ---

CANON_DIGEST_TARGET_CHARS = 2000
CANON_DIGEST_HARD_CHARS = 2400
CANON_DIGEST_IDENTITY_CHARS = 400
CANON_DIGEST_RULES_CHARS = 700
CANON_DIGEST_INDEX_LINE_CHARS = 60
CANON_DIGEST_MAX_INDEX_PAGES = 12
CANON_PAGE_GET_DEFAULT_CHARS = 6000
CANON_PAGE_GET_MAX_CHARS = 12000
CANON_SEARCH_HITS = 8
CANON_SEARCH_SNIPPET_CHARS = 300
CANON_PER_CONNECT_TOKEN_BUDGET = 2500
CANON_PAGE_BODY_MAX_CHARS = 20000
CANON_PAGE_EDITOR_WARN_CHARS = 8000
CANON_SUMMARY_MAX_CHARS = 200
CANON_PAGES_PER_TENANT_SOFT = 500

# Rough characters-per-token for the per-connect estimate. Deliberately
# conservative: it over-counts tokens rather than under-counting them.
_CHARS_PER_TOKEN = 4

_DIGEST_IDENTITY_HEADING = "## Identity\n"
_DIGEST_RULES_HEADING = "## Rules\n"
_DIGEST_INDEX_HEADING = "## Index\n"

_TERM_SPLIT = re.compile(r"[^0-9a-z]+")


class CanonError(Exception):
    """Base error for canon reads and writes."""


class CanonValidationError(CanonError):
    """A canon value violates a validation rule from design section 11."""


class CanonCapExceeded(CanonValidationError):
    """A canon value exceeds a hard cap (not a soft warning threshold)."""


# --- Value objects ---


@dataclass(frozen=True)
class CanonDigestPage:
    """One page as the digest and search paths see it."""

    slug: str
    summary: str
    section: str
    priority: int
    pinned_in_digest: bool
    status: str
    body: str = ""
    char_count: int = 0
    room_id: uuid.UUID | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class CanonDigest:
    """The rendered digest plus its truncation cursor."""

    text: str
    char_count: int
    page_count: int
    truncated: bool
    next: str | None
    fingerprint: str


@dataclass(frozen=True)
class CanonPageSlice:
    """A window of one page body, capped by ``CANON_PAGE_GET_MAX_CHARS``."""

    slug: str
    section: str
    summary: str
    body: str
    total_char_count: int
    truncated: bool
    next: str | None


@dataclass(frozen=True)
class CanonSearchHit:
    """One search result, capped at ``CANON_SEARCH_SNIPPET_CHARS``."""

    slug: str
    section: str
    summary: str
    snippet: str
    score: float
    truncated: bool


# --- Validation helpers ---


def validate_section(section: str) -> str:
    """Return ``section`` when it is one of the seven canon sections."""
    if section not in CANON_SECTIONS:
        raise CanonValidationError(f"unknown canon section {section!r}; expected {CANON_SECTIONS}")
    return section


def validate_status(status: str) -> str:
    """Return ``status`` when it is draft, published, or archived."""
    if status not in CANON_STATUSES:
        raise CanonValidationError(f"unknown canon status {status!r}; expected {CANON_STATUSES}")
    return status


def enforce_summary(summary: str | None) -> str:
    """Strip a summary and reject one above ``CANON_SUMMARY_MAX_CHARS``.

    The summary is the digest index line and it is human-authored, so an
    over-long value is an authoring error to report, never text to silently
    mangle. Display truncation happens later, in the 60-character index line,
    and it is always flagged.
    """
    if summary is None:
        raise CanonValidationError("canon summary is required")
    cleaned = summary.strip()
    if not cleaned:
        raise CanonValidationError("canon summary is required")
    if len(cleaned) > CANON_SUMMARY_MAX_CHARS:
        raise CanonValidationError(
            f"canon summary is {len(cleaned)} chars; cap is {CANON_SUMMARY_MAX_CHARS}"
        )
    return cleaned


def enforce_page_body(body: str) -> str:
    """Reject a page body above ``CANON_PAGE_BODY_MAX_CHARS``."""
    if len(body) > CANON_PAGE_BODY_MAX_CHARS:
        raise CanonCapExceeded(
            f"canon page body is {len(body)} chars; cap is {CANON_PAGE_BODY_MAX_CHARS}"
        )
    return body


def page_char_count(body: str) -> int:
    """Character count stored in ``canon_pages.char_count``."""
    return len(body)


def clamp_page_max_chars(requested: int | None) -> int:
    """The single place the 6000 default / 12000 hard-max rule lives.

    The floor is 1, never 0: a zero-width window would return the same offset
    in ``next`` forever and turn a caller's paging loop into a hang.
    """
    if requested is None:
        return CANON_PAGE_GET_DEFAULT_CHARS
    return max(1, min(int(requested), CANON_PAGE_GET_MAX_CHARS))


def per_connect_token_estimate(text: str) -> int:
    """Rough token estimate used for the per-connect budget."""
    return math.ceil(len(text) / _CHARS_PER_TOKEN)


def enforce_per_connect_budget(text: str) -> str:
    """Cut ``text`` so the per-connect estimate fits ``CANON_PER_CONNECT_TOKEN_BUDGET``.

    The budget is a hard stop on what one connect may inject, so the cut is by
    characters (tokens * chars-per-token) and never exceeds the estimate.
    """
    limit = CANON_PER_CONNECT_TOKEN_BUDGET * _CHARS_PER_TOKEN
    if len(text) <= limit:
        return text
    return text[:limit]


def pages_per_tenant_warning(page_count: int) -> bool:
    """True when a tenant is past the soft page-count ceiling."""
    return page_count > CANON_PAGES_PER_TENANT_SOFT


# --- Digest ---


def effective_digest_budget(budget_chars: int) -> int:
    """Clamp a requested digest budget into ``[0, CANON_DIGEST_HARD_CHARS]``.

    The per-block caps already keep a normal digest under the hard cap, so this
    is defence in depth: it holds if a block cap is ever raised, or if a caller
    asks for a budget the design never allows.
    """
    return max(0, min(int(budget_chars), CANON_DIGEST_HARD_CHARS))


def canon_digest_fingerprint(pages: Iterable[CanonDigestPage], *, budget_chars: int) -> str:
    """sha256 over the canonical page tuple list plus the effective budget.

    Input order does not matter: entries are sorted by slug, so the same page
    state always fingerprints the same and the cache cannot false-miss.
    """
    rows = sorted(
        (
            (
                page.slug,
                page.summary,
                page.priority,
                page.pinned_in_digest,
                page.status,
                page.updated_at.isoformat() if page.updated_at is not None else "",
                page.char_count,
            )
            for page in pages
        ),
        key=lambda row: (row[0], row[1]),
    )
    identity = {"budget": effective_digest_budget(budget_chars), "pages": rows}
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _index_hint(page_count: int, shown: int) -> str | None:
    """Cursor for the pages the index block did not show."""
    remaining = page_count - shown
    if remaining <= 0:
        return None
    return f"canon:digest @page {shown + 1} ({remaining} more pages)"


def _render_digest(identity_body: str, rules_body: str, index_lines: Sequence[str]) -> str:
    return "\n\n".join(
        (
            _DIGEST_IDENTITY_HEADING + identity_body,
            _DIGEST_RULES_HEADING + rules_body,
            _DIGEST_INDEX_HEADING + "\n".join(index_lines),
        )
    )


def build_canon_digest(
    pages: Iterable[CanonDigestPage],
    *,
    budget_chars: int = CANON_DIGEST_TARGET_CHARS,
) -> CanonDigest:
    """Assemble the canon digest within the section 11 budgets.

    Order of sacrifice when the budget is tight: index lines go first, then the
    rules tail, then the identity tail. Identity is the last thing to lose
    characters because it is the page an agent must never get wrong.
    """
    ordered = sorted(pages, key=lambda page: (page.slug, page.summary))
    budget = effective_digest_budget(budget_chars)
    fingerprint = canon_digest_fingerprint(ordered, budget_chars=budget)

    published = [page for page in ordered if page.status == "published"]
    page_count = len(published)

    identity_page = min(
        (page for page in published if page.section == "identity"),
        key=lambda page: (page.priority, page.slug),
        default=None,
    )
    rules_page = min(
        (page for page in published if page.section == "rules"),
        key=lambda page: (page.priority, page.slug),
        default=None,
    )

    identity_source = identity_page.body if identity_page is not None else ""
    rules_source = rules_page.body if rules_page is not None else ""
    identity_body = identity_source[:CANON_DIGEST_IDENTITY_CHARS]
    rules_body = rules_source[:CANON_DIGEST_RULES_CHARS]

    identity_next: str | None = None
    rules_next: str | None = None
    if identity_page is not None and len(identity_body) < len(identity_source):
        identity_next = f"canon:get {identity_page.slug} @{len(identity_body)}"
    if rules_page is not None and len(rules_body) < len(rules_source):
        rules_next = f"canon:get {rules_page.slug} @{len(rules_body)}"

    ranked = sorted(
        published,
        key=lambda page: (not page.pinned_in_digest, page.priority, page.slug),
    )
    # Each entry keeps its own cut flag: a summary longer than one index line is
    # a truncation even when every page still fits in the digest.
    index_entries: list[tuple[str, bool, str]] = []
    for page in ranked[:CANON_DIGEST_MAX_INDEX_PAGES]:
        full_line = f"- {page.slug}: {page.summary}"
        line = full_line[:CANON_DIGEST_INDEX_LINE_CHARS]
        index_entries.append((line, len(line) < len(full_line), page.slug))

    def render(entries: Sequence[tuple[str, bool, str]]) -> str:
        return _render_digest(identity_body, rules_body, [entry[0] for entry in entries])

    index_next = _index_hint(page_count, len(index_entries))
    text = render(index_entries)

    # Sacrifice order: index lines, then rules tail, then identity tail.
    while len(text) > budget and index_entries:
        index_entries = index_entries[:-1]
        index_next = _index_hint(page_count, len(index_entries))
        text = render(index_entries)

    while len(text) > budget and rules_body:
        overflow = len(text) - budget
        rules_body = rules_body[: max(0, len(rules_body) - overflow)]
        if rules_page is not None:
            rules_next = f"canon:get {rules_page.slug} @{len(rules_body)}"
        text = render(index_entries)

    while len(text) > budget and identity_body:
        overflow = len(text) - budget
        identity_body = identity_body[: max(0, len(identity_body) - overflow)]
        if identity_page is not None:
            identity_next = f"canon:get {identity_page.slug} @{len(identity_body)}"
        text = render(index_entries)

    hard_cut = False
    if len(text) > budget:
        # The fixed headings alone do not fit: return what fits and say so.
        text = text[:budget]
        hard_cut = True

    cut_line = next((slug for _line, cut, slug in index_entries if cut), None)
    line_next = f"canon:digest @line {cut_line}" if cut_line else None

    truncated = bool(identity_next or rules_next or index_next or line_next or hard_cut)
    next_hint = identity_next or rules_next or index_next or line_next
    if truncated and next_hint is None:
        # Every capped response must carry a cursor; never return a bare flag.
        next_hint = "canon:digest @page 1"

    return CanonDigest(
        text=text,
        char_count=len(text),
        page_count=page_count,
        truncated=truncated,
        next=next_hint,
        fingerprint=fingerprint,
    )


# --- Page slicing ---


def slice_canon_page(
    page: CanonDigestPage,
    *,
    max_chars: int | None = None,
    offset: int = 0,
) -> CanonPageSlice:
    """Return at most ``max_chars`` characters of body starting at ``offset``.

    The cap is ``CANON_PAGE_GET_DEFAULT_CHARS`` unless a caller asks for more,
    and never above ``CANON_PAGE_GET_MAX_CHARS``. A cut always reports the
    offset of the next window so a caller can page through the body.
    """
    limit = clamp_page_max_chars(max_chars)
    start = max(0, int(offset))
    body = page.body
    window = body[start : start + limit]
    end = start + len(window)
    truncated = end < len(body)
    return CanonPageSlice(
        slug=page.slug,
        section=page.section,
        summary=page.summary,
        body=window,
        total_char_count=len(body),
        truncated=truncated,
        next=f"canon:get {page.slug} @{end}" if truncated else None,
    )


# --- Search ---


def _terms(value: str) -> set[str]:
    return {part for part in _TERM_SPLIT.split(value.lower()) if part}


def _snippet(body: str, terms: set[str], fallback: str) -> tuple[str, bool]:
    """Return a snippet of at most ``CANON_SEARCH_SNIPPET_CHARS`` around the match."""
    source = body or fallback
    lowered = source.lower()
    position = -1
    for term in sorted(terms):
        found = lowered.find(term)
        if found >= 0 and (position < 0 or found < position):
            position = found
    if position < 0:
        position = 0

    half = CANON_SEARCH_SNIPPET_CHARS // 2
    start = max(0, position - half)
    end = start + CANON_SEARCH_SNIPPET_CHARS
    if end > len(source):
        end = len(source)
        start = max(0, end - CANON_SEARCH_SNIPPET_CHARS)
    snippet = source[start:end]
    return snippet, len(snippet) < len(source)


def search_canon_pages(
    query: str,
    pages: Iterable[CanonDigestPage],
    *,
    limit: int | None = None,
) -> list[CanonSearchHit]:
    """Rank published pages by case-insensitive term overlap.

    Hits are capped at ``CANON_SEARCH_HITS`` (8); a caller asking for more is
    clamped down, never up. Snippets are capped at
    ``CANON_SEARCH_SNIPPET_CHARS`` and flagged when cut.
    """
    terms = _terms(query)
    if not terms:
        return []
    max_hits = CANON_SEARCH_HITS if limit is None else max(0, min(int(limit), CANON_SEARCH_HITS))
    if max_hits == 0:
        return []

    hits: list[CanonSearchHit] = []
    for page in pages:
        if page.status != "published":
            continue
        slug_terms = _terms(page.slug)
        summary_terms = _terms(page.summary)
        body_terms = _terms(page.body)
        score = sum(
            3.0 * (term in slug_terms) + 2.0 * (term in summary_terms) + 1.0 * (term in body_terms)
            for term in terms
        )
        if score <= 0:
            continue
        snippet, cut = _snippet(page.body, terms, page.summary)
        hits.append(
            CanonSearchHit(
                slug=page.slug,
                section=page.section,
                summary=page.summary,
                snippet=snippet,
                score=score,
                truncated=cut,
            )
        )

    hits.sort(key=lambda hit: (-hit.score, hit.slug))
    return hits[:max_hits]


# --- Async helpers (no business logic of their own) ---


def _to_digest_page(page: CanonPage, raw_content: str | None) -> CanonDigestPage:
    """Map a page row plus its item body onto the value object."""
    body = raw_content or ""
    return CanonDigestPage(
        slug=page.slug,
        summary=page.summary,
        section=page.section,
        priority=page.priority,
        pinned_in_digest=page.pinned_in_digest,
        status=page.status,
        body=body,
        char_count=page.char_count or len(body),
        room_id=page.room_id,
        updated_at=page.updated_at,
    )


async def load_canon_pages(
    db: AsyncSession,
    *,
    tenant_id: str,
    status: str | None = "published",
) -> list[CanonDigestPage]:
    """Load canon pages with their bodies, ordered by priority then slug."""
    stmt = (
        select(CanonPage, Item.raw_content)
        .join(Item, Item.id == CanonPage.item_id)
        .where(CanonPage.tenant_id == tenant_id)
        .order_by(CanonPage.priority.asc(), CanonPage.slug.asc())
    )
    if status is not None:
        stmt = stmt.where(CanonPage.status == status)

    rows = (await db.execute(stmt)).all()
    return [_to_digest_page(page, raw_content) for page, raw_content in rows]


async def get_canon_digest(
    db: AsyncSession,
    *,
    tenant_id: str,
    budget_chars: int = CANON_DIGEST_TARGET_CHARS,
) -> CanonDigest:
    """Return the tenant digest, reusing the cache when the fingerprint matches."""
    pages = await load_canon_pages(db, tenant_id=tenant_id, status="published")
    digest = build_canon_digest(pages, budget_chars=budget_chars)

    cached = await db.get(CanonDigestCache, tenant_id)
    if cached is not None and cached.fingerprint == digest.fingerprint:
        return replace(
            digest,
            text=cached.digest_text,
            char_count=cached.char_count,
            page_count=cached.page_count,
        )

    stmt = (
        pg_insert(CanonDigestCache.__table__)
        .values(
            tenant_id=tenant_id,
            fingerprint=digest.fingerprint,
            digest_text=digest.text,
            char_count=digest.char_count,
            page_count=digest.page_count,
            built_at=func.now(),
        )
        .on_conflict_do_update(
            index_elements=["tenant_id"],
            set_={
                "fingerprint": digest.fingerprint,
                "digest_text": digest.text,
                "char_count": digest.char_count,
                "page_count": digest.page_count,
                "built_at": func.now(),
            },
        )
    )
    await db.execute(stmt)
    return digest


async def get_canon_page(
    db: AsyncSession,
    *,
    tenant_id: str,
    slug: str,
    max_chars: int | None = None,
    offset: int = 0,
) -> CanonPageSlice:
    """Load one page and return the capped slice starting at ``offset``."""
    stmt = (
        select(CanonPage, Item.raw_content)
        .join(Item, Item.id == CanonPage.item_id)
        .where(CanonPage.tenant_id == tenant_id, CanonPage.slug == slug)
        .limit(1)
    )
    row = (await db.execute(stmt)).first()
    if row is None:
        raise CanonError(f"canon page not found: {slug}")

    page, raw_content = row
    return slice_canon_page(_to_digest_page(page, raw_content), max_chars=max_chars, offset=offset)
