"""Canon authoring: the write path for tenant canon pages.

``app.services.canon`` owns every canon cap, the section and status
vocabularies, and the read path. This module owns writes only and imports its
validators rather than restating a cap, so the two halves of canon cannot
disagree about what is legal.

A canon page is an ``items`` row plus a ``canon_pages`` row, and the item is
what retrieval, governance, and the room rebuild actually read: a correct page
row with a stale item is a page agents will quote wrongly. Hence
``_apply_item_fields`` on every write, and hence the invariants below.

* ``metadata_["sync_active"] = False``: canon pages are authored, not routed.
  Without it ``_apply_room_routing`` treats the page as routable and bumps
  ``membership_generation``, which the drift detector then reports against the
  page's own edit.
* A content change to a published page demotes it to ``draft``: agents cache
  and quote canon, so a published page must not change under them silently.
* Revisions are appended on create, on a content change, and on every publish,
  but never on ratify or archive: those confirm or retire, they do not edit.
* Embedding is hash-guarded. ``process_prebuilt_item`` commits on its own, so
  an unchanged body must make zero embedding calls.

The router owns HTTP: ``CanonNotFoundError`` is 404, ``CanonConflictError`` 409,
``CanonAuthoringError`` 422.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canon import (
    CANON_SECTIONS,
    CANON_SOURCE_TYPE,
    CANON_STATUSES,
    CanonPage,
    CanonPageRevision,
)
from app.models.item import Item
from app.models.palace import Room, RoomMembership
from app.services.canon import (
    CANON_PAGE_EDITOR_WARN_CHARS,
    CanonCapExceeded,
    CanonError,
    CanonValidationError,
    enforce_page_body,
    enforce_summary,
    page_char_count,
    validate_section,
    validate_status,
)
from app.services.item_processing import canon_content_hash, process_prebuilt_item

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps the import graph light
    from app.services.embedder import EmbeddingService
    from app.services.llm import LLMService

# ``None`` means "leave unchanged" in a partial update, so clearing a field
# needs a distinct sentinel rather than a second endpoint.
UNSET = object()

_SLUG_MIN_CHARS, _SLUG_MAX_CHARS = 1, 80
_SLUG_ALLOWED = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")
_CANON_TAG, _SECTION_TAG_PREFIX = "canon", "canon-section-"
_AUTHORED_SOURCE = "authored"


class CanonAuthoringError(Exception):
    """Base error for canon writes; the router maps it to 422."""


class CanonNotFoundError(CanonAuthoringError):
    """No canon page (or revision) matches the tenant and slug; maps to 404."""


class CanonConflictError(CanonAuthoringError):
    """The write collides with existing canon state; maps to 409."""


@dataclass(frozen=True)
class CanonPageWrite:
    """The outcome of one write, as the router and the tests see it."""

    page: CanonPage
    item: Item
    revision: int
    embedded: bool


def _slug_is_valid(slug: str) -> bool:
    """True for a lowercase hyphen slug of 1..80 chars with no edge hyphen.

    Slugs are the handle agents quote, so they stay ASCII-lowercase: anything
    else would need URL encoding or sit ambiguously beside a near-identical slug.
    """
    if not isinstance(slug, str) or not (_SLUG_MIN_CHARS <= len(slug) <= _SLUG_MAX_CHARS):
        return False
    if slug[0] == "-" or slug[-1] == "-":
        return False
    return all(char in _SLUG_ALLOWED for char in slug)


def _validate_slug(slug: str) -> str:
    if not _slug_is_valid(slug):
        raise CanonValidationError(
            f"invalid canon slug {slug!r}: lowercase [a-z0-9-], "
            f"{_SLUG_MIN_CHARS}-{_SLUG_MAX_CHARS} chars, no leading or trailing '-'"
        )
    return slug


def _validate_title(title: str) -> str:
    """Strip and require a title: ``items.title`` is non-null and never blank."""
    cleaned = (title or "").strip()
    if not cleaned:
        raise CanonValidationError("canon page title is required")
    return cleaned


def _extra_tags_from_item(item: Item) -> list[str]:
    """Caller tags on the item, minus the markers this module owns.

    ``update_canon_page`` takes no ``tags`` argument, so it must not drop tags a
    caller added at create time.
    """
    extras: list[str] = []
    for tag in item.tags or []:
        if tag != _CANON_TAG and not tag.startswith(_SECTION_TAG_PREFIX) and tag not in extras:
            extras.append(tag)
    return extras


def _validate_body(body: str) -> str:
    """A canon page with a blank body is not a document.

    ``enforce_page_body`` only enforces the upper cap, so a whitespace body
    would otherwise publish and then be quoted as canon.
    """
    if not (body or "").strip():
        raise CanonValidationError("canon page body is required")
    return enforce_page_body(body)


def _item_status_for(page: CanonPage) -> str:
    """The item status a page's visibility requires.

    Every retrieval path filters ``items.status = 'ready'``. A draft or archived
    page therefore must NOT be ready, or unpublished canon would be quoted by
    agents before a human publishes it. ``processing`` is the existing
    not-yet-live value, and nothing scans it as a work queue.
    """
    return "ready" if page.status == "published" else "processing"


def _apply_item_fields(
    item: Item, *, page: CanonPage, title: str, body: str, summary: str,
    tags_extra: list[str], author_subject: str, now: datetime,
) -> None:
    """Mirror the page row onto its item, which is what retrieval reads.

    ``metadata_`` is merged, never replaced: other subsystems keep their own keys
    there and a canon write must not erase them.
    """
    item.source_type = CANON_SOURCE_TYPE
    item.title, item.summary, item.raw_content = title, summary, body

    tags: list[str] = []
    for tag in [_CANON_TAG, f"{_SECTION_TAG_PREFIX}{page.section}", *tags_extra]:
        if tag not in tags:
            tags.append(tag)
    item.tags = tags

    metadata = dict(item.metadata_ or {})
    metadata["canon"] = {
        "slug": page.slug, "section": page.section, "parent_slug": page.parent_slug,
        "room_id": str(page.room_id) if page.room_id is not None else None,
        "priority": page.priority,
    }
    # Required: without it routing and the room rebuild treat a canon page as
    # routable and bump the room generation.
    metadata["sync_active"] = False
    item.metadata_ = metadata

    item.governance_owner_subject = author_subject
    item.governance_verification_state = "verified"
    item.governance_verified_at = now
    item.governance_verified_by_subject = author_subject

    page.char_count = page_char_count(body)  # page row only; items has no such column
    item.status = _item_status_for(page)
    item.deleted_at = None


async def _load_page(db: AsyncSession, *, tenant_id: str, slug: str) -> tuple[CanonPage, Item]:
    """Load one page and its item together, or raise the not-found error."""
    row = (await db.execute(
        select(CanonPage, Item)
        .join(Item, Item.id == CanonPage.item_id)
        .where(CanonPage.tenant_id == tenant_id, CanonPage.slug == slug)
        .limit(1)
    )).first()
    if row is None:
        raise CanonNotFoundError(f"canon page not found: {slug}")
    return row[0], row[1]


async def _reload_for_response(
    db: AsyncSession, *, tenant_id: str, slug: str
) -> tuple[CanonPage, Item]:
    """Re-read the page and item after a write, before anyone projects them.

    A rollback anywhere in the write path expires every ORM instance in the
    session, and an expired attribute on an async session raises
    ``MissingGreenlet`` the moment it is touched — outside a greenlet there is no
    way to run the SELECT it wants. ``process_prebuilt_item`` rolls back on its
    own error paths, so a write that fully succeeded can still hand back a page
    whose attributes are unreadable.

    Projecting the objects the write path happened to be holding therefore cannot
    work, and the failure is invisible in a test session that never expires
    anything. One SELECT here makes the returned objects safe to read, at the cost
    of a single round trip on a write.
    """
    return await _load_page(db, tenant_id=tenant_id, slug=slug)


async def _room_generation(db: AsyncSession, *, room_id: uuid.UUID | None) -> int:
    """The room's ``membership_generation``; 0 when unbound or gone.

    The recorded generation is what a later drift check compares against, and a
    page with no room has nothing to be stale relative to.
    """
    if room_id is None:
        return 0
    room = await db.get(Room, room_id)
    return int(room.membership_generation or 0) if room is not None else 0


async def _delete_authored_membership(
    db: AsyncSession, *, tenant_id: str, item_id: uuid.UUID
) -> None:
    """Drop the authored charter membership for an item.

    A demoted or archived page must not stay a room's charter: the charter is
    what binds an agent there. Scoped by ``source`` so routed rows are untouched.
    """
    await db.execute(delete(RoomMembership).where(
        RoomMembership.tenant_id == tenant_id,
        RoomMembership.item_id == item_id,
        RoomMembership.source == _AUTHORED_SOURCE,
    ))


async def _latest_revision(db: AsyncSession, *, page_id: uuid.UUID) -> int:
    """Highest revision number for a page, or 0 when none was written yet."""
    newest = await db.scalar(
        select(func.max(CanonPageRevision.revision)).where(CanonPageRevision.page_id == page_id)
    )
    return int(newest or 0)


async def _append_revision(
    db: AsyncSession, *, page: CanonPage, item: Item, author_subject: str,
    change_note: str | None, now: datetime,
) -> int:
    """Append the next immutable revision row and return its number.

    The number comes from a SELECT rather than a counter column so a concurrent
    edit cannot renumber history; the unique key on ``(page_id, revision)`` turns
    a race into a visible error instead of a silent overwrite.
    """
    revision = await _latest_revision(db, page_id=page.id) + 1
    db.add(CanonPageRevision(
        tenant_id=page.tenant_id, page_id=page.id, revision=revision,
        body_md=item.raw_content or "", summary=page.summary, change_note=change_note,
        created_by_subject=author_subject, created_at=now,
    ))
    await db.flush()
    return revision


async def _embed_if_changed(
    db: AsyncSession, *, item: Item, body: str, embedder: EmbeddingService | None,
    llm: LLMService | None, tenant_id: str, page_slug: str,
) -> bool:
    """Re-embed only when the body hash moved; return whether embedding ran.

    ``process_prebuilt_item`` is expensive and commits on its own, so an unchanged
    body must make zero embedding calls and a retry must see its own hash.

    It also reports ``duplicate`` instead of raising when the tenant already holds
    an item with the same dedup key, and in that case it marks the item failed and
    embeds nothing. A canon publish that silently stored no vectors would look
    successful while the page is invisible to semantic retrieval, so a duplicate
    result is surfaced as a conflict here rather than swallowed.
    """
    if embedder is None or llm is None:
        return False
    # The guard must use the SAME key process_prebuilt_item writes, or an
    # unchanged re-publish would look changed and re-embed every time.
    new_hash = canon_content_hash(item.id, body)
    if item.content_hash == new_hash:
        return False
    item.content_hash = new_hash
    await db.flush()
    result = await process_prebuilt_item(
        db, item=item, embedder=embedder, llm=llm, tenant_id=tenant_id, enable_ai_enrichment=False
    )
    if result.status == "duplicate":
        raise CanonConflictError(
            f"canon page {page_slug} was not embedded: its body collides with "
            f"item {result.duplicate_of}. Change the body or archive the other item."
        )
    return True


async def create_canon_page(
    db: AsyncSession, *, tenant_id: str, author_subject: str, slug: str, title: str,
    summary: str, section: str, body: str, room_id: uuid.UUID | None = None,
    priority: int = 100, pinned_in_digest: bool = False, parent_slug: str | None = None,
    tags: list[str] | None = None, commit: bool = True,
) -> CanonPageWrite:
    """Create a draft page, its item, and revision 1; never embed here.

    Always a draft: nothing reaches the digest or an agent's context until a
    human publishes, so a half-written page is harmless. Publish owns the
    embedding cost.

    ``commit=False`` leaves the transaction open because the folder importer
    commits once for the whole batch instead of once per file.
    """
    now = datetime.now(timezone.utc)
    validate_section(section)
    clean_summary = enforce_summary(summary)
    _validate_body(body)
    clean_slug, clean_title = _validate_slug(slug), _validate_title(title)
    clean_parent = _validate_slug(parent_slug) if parent_slug is not None else None
    if clean_parent == clean_slug:
        raise CanonValidationError("a canon page cannot be its own parent")

    duplicate = await db.scalar(
        select(CanonPage.id).where(CanonPage.tenant_id == tenant_id, CanonPage.slug == clean_slug)
    )
    if duplicate is not None:
        raise CanonConflictError(f"canon page already exists: {clean_slug}")

    # Item first: the page row carries a foreign key to it.
    item = Item(
        tenant_id=tenant_id, source_type=CANON_SOURCE_TYPE, title=clean_title,
        summary=clean_summary, raw_content=body, metadata_={}, tags=[], categories=[],
        status="processing", created_at=now, updated_at=now,
    )
    db.add(item)
    await db.flush()

    page = CanonPage(
        tenant_id=tenant_id, item_id=item.id, room_id=room_id, slug=clean_slug,
        section=section, parent_slug=clean_parent, summary=clean_summary, priority=priority,
        pinned_in_digest=pinned_in_digest, status="draft", char_count=page_char_count(body),
        created_by_subject=author_subject, updated_by_subject=author_subject,
        created_at=now, updated_at=now,
    )
    db.add(page)
    await db.flush()

    _apply_item_fields(
        item, page=page, title=clean_title, body=body, summary=clean_summary,
        tags_extra=list(tags or []), author_subject=author_subject, now=now,
    )
    revision = await _append_revision(
        db, page=page, item=item, author_subject=author_subject, change_note="create", now=now
    )
    if not commit:
        # The folder importer commits once for the whole batch, so this write
        # joins its transaction instead of ending it. Nothing was rolled back,
        # so the instances below are still readable without a reload.
        await db.flush()
        return CanonPageWrite(page=page, item=item, revision=revision, embedded=False)

    await db.commit()
    # Re-read after the commit: a rollback inside the write path expires the
    # instances, and projecting an expired attribute would raise MissingGreenlet
    # on a request that actually succeeded.
    page, item = await _reload_for_response(db, tenant_id=tenant_id, slug=slug)
    return CanonPageWrite(page=page, item=item, revision=revision, embedded=False)


async def update_canon_page(
    db: AsyncSession, *, tenant_id: str, author_subject: str, slug: str,
    title: str | None = None, summary: str | None = None, section: str | None = None,
    body: str | None = None, room_id: Any = UNSET, priority: int | None = None,
    pinned_in_digest: bool | None = None, parent_slug: Any = UNSET,
    change_note: str | None = None, embedder: EmbeddingService | None = None,
    llm: LLMService | None = None, commit: bool = True,
) -> CanonPageWrite:
    """Apply a partial edit; ``None`` leaves a field alone, ``UNSET`` clears one.

    A body or summary change to a published page demotes it to ``draft`` and
    drops its authored charter membership, so the published text an agent read
    cannot change without a fresh publish.

    ``commit=False`` leaves the transaction open because the folder importer
    commits once for the whole batch instead of once per file.
    """
    now = datetime.now(timezone.utc)
    page, item = await _load_page(db, tenant_id=tenant_id, slug=slug)

    new_summary = page.summary if summary is None else enforce_summary(summary)
    new_body = (item.raw_content or "") if body is None else _validate_body(body)
    new_section = page.section if section is None else validate_section(section)
    new_title = item.title if title is None else _validate_title(title)

    new_parent: str | None = page.parent_slug
    if parent_slug is not UNSET:
        new_parent = None if parent_slug is None else _validate_slug(parent_slug)
        if new_parent == page.slug:
            raise CanonValidationError("a canon page cannot be its own parent")

    summary_changed = new_summary != page.summary
    body_changed = new_body != (item.raw_content or "")

    if (summary_changed or body_changed) and page.status == "published":
        page.status = "draft"
        await _delete_authored_membership(db, tenant_id=tenant_id, item_id=item.id)

    page.summary, page.section, page.parent_slug = new_summary, new_section, new_parent
    page.updated_by_subject = author_subject
    if priority is not None:
        page.priority = priority
    if pinned_in_digest is not None:
        page.pinned_in_digest = pinned_in_digest
    if room_id is not UNSET:
        page.room_id = room_id

    _apply_item_fields(
        item, page=page, title=new_title, body=new_body, summary=new_summary,
        tags_extra=_extra_tags_from_item(item), author_subject=author_subject, now=now,
    )

    revision = await _latest_revision(db, page_id=page.id)
    if summary_changed or body_changed:
        revision = await _append_revision(
            db, page=page, item=item, author_subject=author_subject,
            change_note=change_note or "update", now=now,
        )

    embedded = False
    if body_changed:
        embedded = await _embed_if_changed(
            db, item=item, body=new_body, embedder=embedder, llm=llm,
            tenant_id=tenant_id, page_slug=page.slug,
        )
        # process_prebuilt_item marks a successfully embedded item ready. A page
        # that is not published must not become retrievable as a side effect.
        item.status = _item_status_for(page)
    if not commit:
        # The folder importer commits once for the whole batch, so this write
        # joins its transaction instead of ending it. Nothing was rolled back,
        # so the instances below are still readable without a reload.
        await db.flush()
        return CanonPageWrite(page=page, item=item, revision=revision, embedded=embedded)

    await db.commit()
    # Re-read after the commit: a rollback inside the write path expires the
    # instances, and projecting an expired attribute would raise MissingGreenlet
    # on a request that actually succeeded.
    page, item = await _reload_for_response(db, tenant_id=tenant_id, slug=slug)
    return CanonPageWrite(page=page, item=item, revision=revision, embedded=embedded)


async def publish_canon_page(
    db: AsyncSession, *, tenant_id: str, author_subject: str, slug: str,
    embedder: EmbeddingService, llm: LLMService,
) -> CanonPageWrite:
    """Publish: confirm, snapshot, embed, and bind a charter if one is asked for.

    Order matters. The status change is flushed before the revision and before
    embedding so the partial unique index on one published charter per room
    raises here, in this transaction, rather than from inside
    ``process_prebuilt_item`` after it has already committed.
    """
    now = datetime.now(timezone.utc)
    page, item = await _load_page(db, tenant_id=tenant_id, slug=slug)

    enforce_summary(page.summary)
    body = item.raw_content or ""
    _validate_body(body)

    page.status = validate_status("published")
    page.updated_by_subject = author_subject
    page.last_confirmed_at = now
    page.last_confirmed_generation = await _room_generation(db, room_id=page.room_id)
    page.char_count = page_char_count(body)

    conflict = f"room already has a published charter: {slug}"
    try:
        await db.flush()
    except IntegrityError as exc:
        # Deliberately not pre-checked with a SELECT: a pre-check races, the
        # index does not.
        await db.rollback()
        raise CanonConflictError(conflict) from exc

    revision = await _append_revision(
        db, page=page, item=item, author_subject=author_subject, change_note="publish", now=now
    )
    embedded = await _embed_if_changed(
        db, item=item, body=body, embedder=embedder, llm=llm,
        tenant_id=tenant_id, page_slug=page.slug,
    )
    item.status = _item_status_for(page)

    if page.section == "charter" and page.room_id is not None:
        try:
            existing = await db.scalar(select(RoomMembership).where(
                RoomMembership.tenant_id == tenant_id,
                RoomMembership.room_id == page.room_id,
                RoomMembership.item_id == item.id,
                RoomMembership.source == _AUTHORED_SOURCE,
            ))
            if existing is None:
                db.add(RoomMembership(
                    tenant_id=tenant_id, room_id=page.room_id, item_id=item.id,
                    membership_kind="primary", source=_AUTHORED_SOURCE, confidence=1.0,
                ))
            else:
                existing.membership_kind, existing.confidence = "primary", 1.0
            await db.flush()
        except IntegrityError as exc:
            await db.rollback()
            raise CanonConflictError(conflict) from exc

    await db.commit()
    # Re-read after the commit: a rollback inside the write path expires the
    # instances, and projecting an expired attribute would raise MissingGreenlet
    # on a request that actually succeeded.
    page, item = await _reload_for_response(db, tenant_id=tenant_id, slug=slug)
    return CanonPageWrite(page=page, item=item, revision=revision, embedded=embedded)


async def ratify_canon_page(
    db: AsyncSession, *, tenant_id: str, author_subject: str, slug: str
) -> CanonPageWrite:
    """Re-confirm a published page without editing it, and append no revision.

    Ratifying is how a page stays trusted: it refreshes the confirmation stamp,
    the room generation it was confirmed against, and the item governance record.
    """
    now = datetime.now(timezone.utc)
    page, item = await _load_page(db, tenant_id=tenant_id, slug=slug)
    if page.status != "published":
        raise CanonConflictError(
            f"canon page {slug} is {page.status}; only a published page can be ratified"
        )

    page.last_confirmed_at = now
    page.last_confirmed_generation = await _room_generation(db, room_id=page.room_id)
    page.updated_by_subject = author_subject
    item.governance_verification_state = "verified"
    item.governance_verified_at = now
    item.governance_verified_by_subject = author_subject

    revision = await _latest_revision(db, page_id=page.id)
    await db.commit()
    # Re-read after the commit: a rollback inside the write path expires the
    # instances, and projecting an expired attribute would raise MissingGreenlet
    # on a request that actually succeeded.
    page, item = await _reload_for_response(db, tenant_id=tenant_id, slug=slug)
    return CanonPageWrite(page=page, item=item, revision=revision, embedded=False)


async def archive_canon_page(
    db: AsyncSession, *, tenant_id: str, author_subject: str, slug: str
) -> CanonPageWrite:
    """Archive a page and release any room charter it held; append no revision.

    A retired page still bound as a charter would keep speaking in that room,
    and archiving changes no history, so there is nothing to snapshot.
    """
    page, item = await _load_page(db, tenant_id=tenant_id, slug=slug)
    page.status = validate_status("archived")
    page.updated_by_subject = author_subject
    await _delete_authored_membership(db, tenant_id=tenant_id, item_id=item.id)

    revision = await _latest_revision(db, page_id=page.id)
    await db.commit()
    # Re-read after the commit: a rollback inside the write path expires the
    # instances, and projecting an expired attribute would raise MissingGreenlet
    # on a request that actually succeeded.
    page, item = await _reload_for_response(db, tenant_id=tenant_id, slug=slug)
    return CanonPageWrite(page=page, item=item, revision=revision, embedded=False)


async def get_canon_page_detail(
    db: AsyncSession, *, tenant_id: str, slug: str
) -> tuple[CanonPage, Item, int]:
    """Return the page, its item, and how many revisions exist."""
    page, item = await _load_page(db, tenant_id=tenant_id, slug=slug)
    total = await db.scalar(
        select(func.count())
        .select_from(CanonPageRevision)
        .where(CanonPageRevision.page_id == page.id)
    )
    return page, item, int(total or 0)


async def list_canon_page_revisions(
    db: AsyncSession, *, tenant_id: str, slug: str, limit: int = 50, offset: int = 0
) -> list[CanonPageRevision]:
    """List a page's revisions newest first, for the editor's history view."""
    page, _item = await _load_page(db, tenant_id=tenant_id, slug=slug)
    rows = await db.scalars(
        select(CanonPageRevision)
        .where(CanonPageRevision.page_id == page.id)
        .order_by(CanonPageRevision.revision.desc())
        .limit(max(1, min(int(limit), 200)))
        .offset(max(0, int(offset)))
    )
    return list(rows)


async def get_canon_page_revision(
    db: AsyncSession, *, tenant_id: str, slug: str, revision: int
) -> CanonPageRevision:
    """Return one revision; a missing page or revision is the same 404."""
    page, _item = await _load_page(db, tenant_id=tenant_id, slug=slug)
    row = await db.scalar(
        select(CanonPageRevision).where(
            CanonPageRevision.page_id == page.id, CanonPageRevision.revision == revision
        )
    )
    if row is None:
        raise CanonNotFoundError(f"canon page {slug} has no revision {revision}")
    return row


async def canon_revision_counts(
    db: AsyncSession, *, tenant_id: str, page_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Count revisions per page for a set of pages, in one query.

    The list view shows "rev N" per row, and a page row does not carry its own
    revision count: the revisions live in their own table and only the detail
    view ever asked for them. Returning a dict rather than `len(await ...)` per
    page keeps the list at one query regardless of page count.

    Pages with no revisions are absent from the mapping rather than present as
    zero, so a caller must use `.get(page.id, 0)` and cannot mistake a missing
    key for a real count.
    """
    if not page_ids:
        return {}
    rows = await db.execute(
        select(CanonPageRevision.page_id, func.count())
        .where(
            CanonPageRevision.tenant_id == tenant_id,
            CanonPageRevision.page_id.in_(page_ids),
        )
        .group_by(CanonPageRevision.page_id)
    )
    return {page_id: int(count) for page_id, count in rows}


async def list_canon_pages(
    db: AsyncSession, *, tenant_id: str, room_id: uuid.UUID | None = None,
    section: str | None = None, status: str | None = None, limit: int = 50, offset: int = 0,
) -> tuple[list[tuple[CanonPage, Item]], int]:
    """List pages with filters, plus the total of the filtered set.

    Returns ``(page, item)`` pairs because the title an editor displays lives on
    the item, not the page row. ``total`` counts the filter, not the window: a
    caller paging the editor needs to know how many rows exist.
    """
    conditions = [CanonPage.tenant_id == tenant_id]
    if room_id is not None:
        conditions.append(CanonPage.room_id == room_id)
    if section is not None:
        conditions.append(CanonPage.section == validate_section(section))
    if status is not None:
        conditions.append(CanonPage.status == validate_status(status))

    total = await db.scalar(select(func.count()).select_from(CanonPage).where(*conditions))
    rows = (await db.execute(
        select(CanonPage, Item)
        .join(Item, Item.id == CanonPage.item_id)
        .where(*conditions)
        .order_by(CanonPage.priority.asc(), CanonPage.slug.asc())
        .limit(max(1, min(int(limit), 200)))
        .offset(max(0, int(offset)))
    )).all()
    return [(page, item) for page, item in rows], int(total or 0)
