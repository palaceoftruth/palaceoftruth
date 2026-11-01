"""Canon drift detection: the machine notices, the human ratifies.

Design section 8 is one sentence of policy with a lot behind it: *canon never
regenerates, it can fall behind, palace detects that and asks.* This module is
the "detects that and asks" half.

A canon page bound to a room is drift-suspected when the room's
``snapshot_generation`` has moved past the generation the page was confirmed
against, or when the room gained more than
``CANON_DRIFT_MEMBERSHIP_THRESHOLD`` memberships since that confirmation. A
published page no human ever confirmed is ``stale`` instead: weaker than drift,
because nothing is known to have changed, but it was never ratified either.

Detection is read-only apart from one artifact insert. It never writes
``canon_pages``, ``canon_page_revisions``, ``items`` or ``rooms``: a machine that
edited canon would be exactly the failure mode the design forbids. The artifact
is a question for a human, and ratifying or publishing the page answers it —
``resolve_canon_drift_artifacts`` is what the two writers call to close it.
"""

import logging
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canon import CanonPage
from app.models.item import Item
from app.models.palace import CandidateCurationArtifact, Room, RoomMembership
from app.services.canon import (
    CANON_DRIFT_MEMBERSHIP_THRESHOLD,
    CanonError,
    CanonValidationError,
)
from app.services.curation_artifacts import _artifact_snapshot, _record_artifact_event

logger = logging.getLogger(__name__)

STATE_NONE = "none"
STATE_DRIFT_SUSPECTED = "drift-suspected"
STATE_STALE = "stale"
DRIFT_STATES = (STATE_NONE, STATE_DRIFT_SUSPECTED, STATE_STALE)

# Statuses that still mean "nobody has answered this question". A ratification
# or a publish is the answer, so every one of them becomes ``promoted``.
OPEN_DRIFT_STATUSES = ("draft", "needs_source", "reviewable")

_DRIFT_ARTIFACT_KIND = "candidate_source_drift"
_TARGET_RUNTIME = "palace"
# "New tag clusters" is a reading aid, not a budget: the top few tags are enough
# for a human to recognise what kind of material the room has taken on.
_TAG_CLUSTER_LIMIT = 5
_CREATED_BY = "system:canon-drift"


class CanonDriftError(CanonError):
    """Detection could not leave a reviewable artifact behind."""


@dataclass(frozen=True)
class PageDrift:
    """One page's drift verdict, as the read route and the tests see it."""

    slug: str
    title: str
    room_id: uuid.UUID | None
    room_slug: str
    state: str  # none | drift-suspected | stale
    confirmed_generation: int
    current_generation: int
    new_memberships: int
    artifact_id: uuid.UUID | None
    reason: str  # one sentence a human reads on screen


@dataclass(frozen=True)
class DriftDetectionSummary:
    """What one detection run did, so a caller can tell a no-op from a write."""

    evaluated: int
    created: int
    deduplicated: int
    counts: dict[str, int]


@dataclass(frozen=True)
class _RoomMeasure:
    """The room facts one page is judged against, read once per page."""

    room_slug: str
    current_generation: int
    item_count: int
    new_memberships: int
    top_tags: tuple[tuple[str, int], ...]


def drift_dedupe_key(page_id: uuid.UUID, current_generation: int) -> str:
    """One artifact per page per observed generation; the history is kept."""
    return f"canon-drift:{page_id}:{current_generation}"


def drift_target_surface(slug: str) -> str:
    """The surface a drift artifact points at: the page, not the room."""
    return f"canon:{slug}"


def membership_watermark(page: CanonPage) -> datetime:
    """The instant new memberships are counted from.

    ``room_memberships`` carries no generation column, so there is no honest way
    to ask "which memberships arrived after generation N". Time is the only
    watermark available: the confirmation stamp when the page has one, and the
    page's own creation when it has never been confirmed. Counting from the
    beginning of time instead would report a page that has been stable for a year
    as drifted by every membership the room ever had.
    """
    return page.last_confirmed_at or page.created_at


def _confirmed_generation(page: CanonPage) -> int:
    return int(page.last_confirmed_generation or 0)


def _is_newer(created_at: datetime | None, watermark: datetime) -> bool:
    return created_at is not None and created_at > watermark


async def _measure_room(
    db: AsyncSession, *, tenant_id: str, page: CanonPage
) -> _RoomMeasure:
    """Read the room's generation, size, new memberships, and tag profile."""
    room = await db.get(Room, page.room_id)
    watermark = membership_watermark(page)

    memberships = (
        await db.execute(
            select(RoomMembership.item_id, RoomMembership.created_at)
            .where(
                RoomMembership.tenant_id == tenant_id,
                RoomMembership.room_id == page.room_id,
            )
        )
    ).all()
    member_item_ids = {item_id for item_id, _created_at in memberships}
    # Memberships, not items: the design's threshold is about how much the room
    # took on, and one item can arrive through more than one source.
    new_memberships = sum(1 for _item_id, created_at in memberships if _is_newer(created_at, watermark))

    tag_rows = (
        await db.execute(
            select(Item.id, Item.tags)
            .join(RoomMembership, RoomMembership.item_id == Item.id)
            .where(
                Item.tenant_id == tenant_id,
                RoomMembership.tenant_id == tenant_id,
                RoomMembership.room_id == page.room_id,
            )
        )
    ).all()
    tag_counts: Counter[str] = Counter()
    seen_items: set[uuid.UUID] = set()
    for item_id, tags in tag_rows:
        if item_id in seen_items:
            continue
        seen_items.add(item_id)
        tag_counts.update(tags or [])

    return _RoomMeasure(
        room_slug=(room.slug if room is not None else ""),
        current_generation=int(room.snapshot_generation or 0) if room is not None else 0,
        item_count=len(member_item_ids),
        new_memberships=new_memberships,
        top_tags=tuple(
            sorted(tag_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:_TAG_CLUSTER_LIMIT]
        ),
    )


def _state_for(page: CanonPage, *, measure: _RoomMeasure) -> str:
    """Map one page onto none | drift-suspected | stale.

    ``stale`` is checked first and wins: a published page that no human ever
    confirmed has nothing to compare against, so calling it drift-suspected would
    overstate what is known. It reads as a weaker, different problem.
    """
    if page.status == "published" and _confirmed_generation(page) == 0:
        return STATE_STALE
    if measure.current_generation > _confirmed_generation(page):
        return STATE_DRIFT_SUSPECTED
    if measure.new_memberships > CANON_DRIFT_MEMBERSHIP_THRESHOLD:
        return STATE_DRIFT_SUSPECTED
    return STATE_NONE


def _reason_for(page: CanonPage, *, measure: _RoomMeasure, state: str) -> str:
    """One sentence naming what changed, in numbers a human can act on."""
    confirmed = _confirmed_generation(page)
    gap = measure.current_generation - confirmed
    if state == STATE_STALE:
        return (
            f"Published but never ratified: no human has confirmed this page against "
            f"room {measure.room_slug!r} (confirmed generation 0)."
        )
    if state == STATE_DRIFT_SUSPECTED:
        changes: list[str] = []
        if gap > 0:
            changes.append(
                f"room {measure.room_slug!r} moved from generation {confirmed} to "
                f"{measure.current_generation} (gap {gap})"
            )
        if measure.new_memberships > CANON_DRIFT_MEMBERSHIP_THRESHOLD:
            changes.append(
                f"{measure.new_memberships} memberships were added since confirmation "
                f"(threshold {CANON_DRIFT_MEMBERSHIP_THRESHOLD})"
            )
        return "Drift suspected: " + " and ".join(changes) + "."
    return (
        f"Confirmed at generation {confirmed}; the room has gained "
        f"{measure.new_memberships} memberships since, at or under the threshold of "
        f"{CANON_DRIFT_MEMBERSHIP_THRESHOLD}."
    )


def _candidate_body(*, page: CanonPage, item: Item, measure: _RoomMeasure, state: str) -> str:
    """Plain sentences with every measured number, zeros included.

    A zero is stated rather than dropped: "new memberships: 0" tells a reviewer
    the drift is a generation bump alone, while a missing line reads as an
    unknown.
    """
    confirmed = _confirmed_generation(page)
    gap = measure.current_generation - confirmed
    if measure.top_tags:
        tags_text = ", ".join(f"{tag} ({count})" for tag, count in measure.top_tags)
    else:
        tags_text = "none (0 member items carry tags)"
    return "\n".join(
        (
            f"Canon page {item.title or page.slug!r} ({drift_target_surface(page.slug)}) "
            f"may have drifted from room {measure.room_slug!r}.",
            f"State: {state}.",
            f"Room item count: {measure.item_count}.",
            f"New memberships since confirmation: {measure.new_memberships}.",
            f"Generation gap: {gap} (confirmed generation {confirmed}, "
            f"current generation {measure.current_generation}).",
            f"Most common tags among room member items: {tags_text}.",
        )
    )


async def _evaluate(
    db: AsyncSession, *, page: CanonPage, item: Item
) -> tuple[PageDrift, _RoomMeasure | None]:
    """Measure one page. No artifact is written here."""
    title = item.title or page.slug
    if page.room_id is None:
        # Nothing to be stale relative to, so nothing to report and nothing to file.
        return (
            PageDrift(
                slug=page.slug,
                title=title,
                room_id=None,
                room_slug="",
                state=STATE_NONE,
                confirmed_generation=_confirmed_generation(page),
                current_generation=0,
                new_memberships=0,
                artifact_id=None,
                reason="No room binding: this page does not describe a room, so there is nothing to drift from.",
            ),
            None,
        )

    measure = await _measure_room(db, tenant_id=page.tenant_id, page=page)
    state = _state_for(page, measure=measure)
    artifact_id = None
    if state != STATE_NONE:
        existing = await _find_artifact(
            db,
            tenant_id=page.tenant_id,
            dedupe_key=drift_dedupe_key(page.id, measure.current_generation),
        )
        artifact_id = existing.id if existing is not None else None

    return (
        PageDrift(
            slug=page.slug,
            title=title,
            room_id=page.room_id,
            room_slug=measure.room_slug,
            state=state,
            confirmed_generation=_confirmed_generation(page),
            current_generation=measure.current_generation,
            new_memberships=measure.new_memberships,
            artifact_id=artifact_id,
            reason=_reason_for(page, measure=measure, state=state),
        ),
        measure,
    )


async def drift_for_page(db: AsyncSession, *, page: CanonPage, item: Item) -> PageDrift:
    """Evaluate one page. Reads only; an unbound page is always ``none``."""
    drift, _measure = await _evaluate(db, page=page, item=item)
    return drift


async def _bound_pages(db: AsyncSession, *, tenant_id: str) -> list[tuple[CanonPage, Item]]:
    """Every page bound to a room, oldest confirmation first (never = first).

    Pages without a room are excluded in SQL and again in Python: the SQL filter
    is the real one, and the guard keeps a caller that reuses the rows from
    judging a page that has nothing to drift from.
    """
    rows = (
        await db.execute(
            select(CanonPage, Item)
            .join(Item, Item.id == CanonPage.item_id)
            .where(CanonPage.tenant_id == tenant_id, CanonPage.room_id.is_not(None))
            .order_by(CanonPage.last_confirmed_at.asc().nulls_first(), CanonPage.slug.asc())
        )
    ).all()
    return [(page, item) for page, item in rows if page.room_id is not None]


async def list_canon_drift(
    db: AsyncSession, *, tenant_id: str, state: str | None = None
) -> list[PageDrift]:
    """Every room-bound page with its verdict, oldest confirmation first.

    ``state`` filters to one verdict and rejects anything outside the three;
    ``none`` is only included when no filter is given, because a healthy page is
    not something the drift screen is asking about.
    """
    if state is not None and state not in DRIFT_STATES:
        raise CanonValidationError(
            f"unknown canon drift state {state!r}; expected one of {list(DRIFT_STATES)}"
        )
    drifts = [
        (await _evaluate(db, page=page, item=item))[0]
        for page, item in await _bound_pages(db, tenant_id=tenant_id)
    ]
    if state is None:
        return drifts
    return [drift for drift in drifts if drift.state == state]


async def _find_artifact(
    db: AsyncSession, *, tenant_id: str, dedupe_key: str
) -> CandidateCurationArtifact | None:
    """The artifact already filed for this page and generation, if any."""
    return await db.scalar(
        select(CandidateCurationArtifact).where(
            CandidateCurationArtifact.tenant_id == tenant_id,
            CandidateCurationArtifact.dedupe_key == dedupe_key,
        )
    )


def _owner_routes(item: Item) -> list[str]:
    routes = sorted(
        {
            route
            for route in (item.governance_reviewer_subject, item.governance_owner_subject)
            if route
        }
    )
    return routes or ["tenant-operator"]


def _artifact_for(
    *, page: CanonPage, item: Item, drift: PageDrift, measure: _RoomMeasure
) -> CandidateCurationArtifact:
    """Build the reviewable proposal. It asks a question; it changes nothing."""
    item_id = str(page.item_id)
    return CandidateCurationArtifact(
        tenant_id=page.tenant_id,
        artifact_kind=_DRIFT_ARTIFACT_KIND,
        target_runtime=_TARGET_RUNTIME,
        target_surface=drift_target_surface(page.slug),
        status="reviewable",
        source_item_ids=[item_id],
        # Only when the item actually has a hash: an invented digest would be
        # worse than an absent one, because it would look like evidence.
        source_digests={item_id: item.content_hash} if item.content_hash else {},
        affected_item_ids=[item_id],
        affected_claim_ids=[],
        evidence_diff={
            "format": "canon_drift_v1",
            "confirmed_generation": drift.confirmed_generation,
            "current_generation": drift.current_generation,
            "membership_gap": drift.new_memberships,
            "room_item_count": measure.item_count,
        },
        dedupe_key=drift_dedupe_key(page.id, drift.current_generation),
        candidate_body=_candidate_body(page=page, item=item, measure=measure, state=drift.state),
        privacy_review={
            "safe_for_review": True,
            "raw_sensitive_content_excluded": True,
            "contains_sensitive_content": False,
            "method": "deterministic_canon_drift_v1",
        },
        eval_summary={
            "detector": "deterministic_canon_drift_v1",
            "llm_used": False,
            "state": drift.state,
            "generation_gap": drift.current_generation - drift.confirmed_generation,
            "membership_gap": drift.new_memberships,
        },
        approval={},
        created_by_principal=_CREATED_BY,
        metadata_={
            "proposal_type": "canon_drift",
            "state": drift.state,
            "room_id": str(page.room_id) if page.room_id is not None else None,
            "owner_routes": _owner_routes(item),
            "review_inbox": {"resolved": False},
        },
    )


async def _create_or_find_artifact(
    db: AsyncSession, *, page: CanonPage, item: Item, drift: PageDrift, measure: _RoomMeasure
) -> str:
    """File one artifact per page per generation; a retry is a quiet no-op."""
    if drift.artifact_id is not None:
        # `_evaluate` already found the artifact for this generation.
        return "deduplicated"
    dedupe_key = drift_dedupe_key(page.id, drift.current_generation)
    existing = await _find_artifact(db, tenant_id=page.tenant_id, dedupe_key=dedupe_key)
    if existing is not None:
        return "deduplicated"

    artifact = _artifact_for(page=page, item=item, drift=drift, measure=measure)
    try:
        # A savepoint, not a commit: the caller owns the transaction, and a lost
        # race must not take the caller's other work down with it.
        async with db.begin_nested():
            db.add(artifact)
            await db.flush()
            _record_artifact_event(
                db,
                artifact=artifact,
                event_type="canon_drift_created",
                previous_snapshot=None,
            )
            await db.flush()
    except IntegrityError as exc:
        winner = await _find_artifact(db, tenant_id=page.tenant_id, dedupe_key=dedupe_key)
        if winner is None:
            raise CanonDriftError(
                f"canon drift dedupe conflict for {dedupe_key} has no visible winner"
            ) from exc
        return "deduplicated"

    logger.info(
        "canon_drift_created tenant_id=%s page=%s generation=%s artifact_id=%s",
        page.tenant_id,
        page.slug,
        drift.current_generation,
        artifact.id,
    )
    return "created"


async def detect_canon_drift(db: AsyncSession, *, tenant_id: str) -> DriftDetectionSummary:
    """Evaluate every room-bound page and file an artifact where it is warranted.

    The caller owns the transaction: nothing here commits or rolls back, so the
    detection run lands with whatever else the caller is doing, or not at all.
    """
    created = 0
    deduplicated = 0
    counts = {name: 0 for name in DRIFT_STATES}
    pages = await _bound_pages(db, tenant_id=tenant_id)
    for page, item in pages:
        drift, measure = await _evaluate(db, page=page, item=item)
        counts[drift.state] = counts.get(drift.state, 0) + 1
        if drift.state == STATE_NONE or measure is None:
            continue
        outcome = await _create_or_find_artifact(
            db, page=page, item=item, drift=drift, measure=measure
        )
        if outcome == "created":
            created += 1
        else:
            deduplicated += 1
    return DriftDetectionSummary(
        evaluated=len(pages),
        created=created,
        deduplicated=deduplicated,
        counts=counts,
    )


async def resolve_canon_drift_artifacts(
    db: AsyncSession, *, tenant_id: str, slug: str, resolved_by: str
) -> int:
    """Close every open drift artifact for one page after a human said it is true.

    Called by ``ratify_canon_page`` and ``publish_canon_page`` inside their own
    transaction, before the commit. The status becomes ``promoted`` because the
    question the artifact asked has been answered by a person, not because
    anything was regenerated. Returns how many artifacts were closed.
    """
    artifacts = list(
        (
            await db.scalars(
                select(CandidateCurationArtifact).where(
                    CandidateCurationArtifact.tenant_id == tenant_id,
                    CandidateCurationArtifact.artifact_kind == _DRIFT_ARTIFACT_KIND,
                    CandidateCurationArtifact.target_surface == drift_target_surface(slug),
                    CandidateCurationArtifact.status.in_(OPEN_DRIFT_STATUSES),
                )
            )
        ).all()
    )
    if not artifacts:
        return 0

    now = datetime.now(timezone.utc)
    for artifact in artifacts:
        previous_snapshot = _artifact_snapshot(artifact)
        artifact.status = "promoted"
        artifact.approval = {
            **dict(artifact.approval or {}),
            "approved_by": resolved_by,
            "approved_at": now.isoformat(),
            "decision": "approved",
            "promotion_target": artifact.target_surface,
            "note": "cleared by canon ratification",
        }
        artifact.approved_by_principal = resolved_by
        artifact.approval_decided_at = now
        artifact.approved_at = artifact.approved_at or now
        metadata = dict(artifact.metadata_ or {})
        review = dict(metadata.get("review_inbox") or {})
        review["resolved"] = True
        review["last_action"] = "ratify"
        review["last_actor"] = resolved_by
        review["last_action_at"] = now.isoformat()
        metadata["review_inbox"] = review
        artifact.metadata_ = metadata
        _record_artifact_event(
            db,
            artifact=artifact,
            event_type="canon_drift_resolved",
            previous_snapshot=previous_snapshot,
        )
    await db.flush()
    logger.info(
        "canon_drift_resolved tenant_id=%s page=%s artifacts=%d",
        tenant_id,
        slug,
        len(artifacts),
    )
    return len(artifacts)
