"""Canon authoring API.

Read is open to any authorized principal, because agents must read canon on
connect. Write is closed to everything except a human browser session: the
`require_human_author` dependency runs before the capability check, so an agent
holding a write grant still receives `403 canon_human_author_only` rather than
an insufficient-scope error. Agents contribute through
`candidate_canon_edit` artifacts instead.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_auth_context, require_api_capability, require_human_author
from app.database import get_db
from app.schemas.canon import (
    CanonDigestOut,
    CanonPageCreate,
    CanonPageListResponse,
    CanonPageOut,
    CanonPageSliceOut,
    CanonPageUpdate,
    CanonRevisionListResponse,
    CanonRevisionOut,
    CanonSearchHitOut,
    CanonSearchResponse,
    CanonWriteResponse,
)
from app.services.canon import (
    CanonCapExceeded,
    CanonError,
    CanonValidationError,
    clamp_page_max_chars,
    get_canon_digest,
    # Aliased: this module already has a route handler named `get_canon_page`,
    # and a route handler shadowing a service function is a silent trap.
    get_canon_page as read_canon_page_slice,
    load_canon_pages,
    search_canon_pages,
)
from app.services.canon_authoring import (
    UNSET,
    CanonAuthoringError,
    CanonConflictError,
    CanonNotFoundError,
    archive_canon_page,
    create_canon_page,
    get_canon_page_detail,
    get_canon_page_revision,
    list_canon_page_revisions,
    list_canon_pages,
    publish_canon_page,
    ratify_canon_page,
    update_canon_page,
)

router = APIRouter(prefix="/canon", tags=["canon"])
logger = logging.getLogger(__name__)

# Read: any principal with the read capability.
_READ = [Depends(require_api_capability("read"))]
# Write: human session first, then the write capability. Order is the policy.
_WRITE = [Depends(require_human_author()), Depends(require_api_capability("write"))]


def _author_subject(request: Request) -> str:
    """A stable identifier for the human who authored this page.

    A browser session id changes on every login, so the owning API key is the
    durable owner. Fall back to the session subject, then the auth mode.
    """
    context = get_auth_context(request)
    metadata = context.audit_metadata or {}
    api_key_id = metadata.get("api_key_id")
    if api_key_id:
        return f"api_key:{api_key_id}"
    return context.subject_id or str(context.client_id or context.auth_mode)


def _raise_for(exc: CanonError | CanonAuthoringError) -> HTTPException:
    if isinstance(exc, CanonNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, CanonConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, CanonCapExceeded):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, (CanonValidationError, CanonAuthoringError)):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def _page_out(page, *, item=None, revision_count: int = 0, include_body: bool = False) -> CanonPageOut:
    """Project a page row, plus its item where the caller loaded one.

    The title and body live on the item, so a page without its item is
    projected with the slug as the title rather than failing validation.
    """
    return CanonPageOut(
        slug=page.slug,
        title=(getattr(item, "title", None) or page.slug),
        summary=page.summary,
        section=page.section,
        status=page.status,
        room_id=page.room_id,
        parent_slug=page.parent_slug,
        priority=page.priority or 0,
        pinned_in_digest=bool(page.pinned_in_digest),
        char_count=page.char_count or 0,
        body=(getattr(item, "raw_content", None) if include_body else None),
        revision_count=revision_count,
        created_by_subject=page.created_by_subject,
        updated_by_subject=page.updated_by_subject,
        last_confirmed_generation=page.last_confirmed_generation or 0,
        last_confirmed_at=page.last_confirmed_at,
        created_at=page.created_at,
        updated_at=page.updated_at,
    )


@router.get("/pages", response_model=CanonPageListResponse, dependencies=_READ)
async def get_canon_pages(
    request: Request,
    room_id: uuid.UUID | None = Query(None),
    section: str | None = Query(None),
    page_status: str | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> CanonPageListResponse:
    try:
        rows, total = await list_canon_pages(
            db,
            tenant_id=request.state.tenant_id,
            room_id=room_id,
            section=section,
            status=page_status,
            limit=limit,
            offset=offset,
        )
    except (CanonError, CanonAuthoringError) as exc:
        raise _raise_for(exc) from exc
    return CanonPageListResponse(
        pages=[_page_out(page, item=item) for page, item in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/pages",
    response_model=CanonWriteResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=_WRITE,
)
async def post_canon_page(
    request: Request,
    body: CanonPageCreate,
    db: AsyncSession = Depends(get_db),
) -> CanonWriteResponse:
    try:
        result = await create_canon_page(
            db,
            tenant_id=request.state.tenant_id,
            author_subject=_author_subject(request),
            slug=body.slug,
            title=body.title,
            summary=body.summary,
            section=body.section,
            body=body.body,
            room_id=body.room_id,
            priority=body.priority,
            pinned_in_digest=body.pinned_in_digest,
            parent_slug=body.parent_slug,
            tags=body.tags,
        )
    except (CanonError, CanonAuthoringError) as exc:
        raise _raise_for(exc) from exc
    return CanonWriteResponse(
        page=_page_out(result.page, item=result.item, include_body=True),
        revision=result.revision,
        embedded=result.embedded,
    )


@router.get("/pages/{slug}", response_model=CanonPageOut, dependencies=_READ)
async def get_canon_page(
    request: Request,
    slug: str,
    db: AsyncSession = Depends(get_db),
) -> CanonPageOut:
    try:
        page, _item, revision_count = await get_canon_page_detail(
            db, tenant_id=request.state.tenant_id, slug=slug
        )
    except (CanonError, CanonAuthoringError) as exc:
        raise _raise_for(exc) from exc
    return _page_out(page, item=_item, revision_count=revision_count, include_body=True)


@router.patch("/pages/{slug}", response_model=CanonWriteResponse, dependencies=_WRITE)
async def patch_canon_page(
    request: Request,
    slug: str,
    body: CanonPageUpdate,
    db: AsyncSession = Depends(get_db),
) -> CanonWriteResponse:
    cleared = body.cleared_fields()
    try:
        result = await update_canon_page(
            db,
            tenant_id=request.state.tenant_id,
            author_subject=_author_subject(request),
            slug=slug,
            title=body.title,
            summary=body.summary,
            section=body.section,
            body=body.body,
            # The service uses UNSET to mean "leave alone" and None to mean
            # "clear it", so a client can unbind a page from its room.
            room_id=(
                None if "room_id" in cleared else (body.room_id if "room_id" in body.model_fields_set else UNSET)
            ),
            parent_slug=(
                None
                if "parent_slug" in cleared
                else (body.parent_slug if "parent_slug" in body.model_fields_set else UNSET)
            ),
            priority=body.priority,
            pinned_in_digest=body.pinned_in_digest,
            change_note=body.change_note,
            embedder=request.app.state.embedder,
            llm=request.app.state.llm,
        )
    except (CanonError, CanonAuthoringError) as exc:
        raise _raise_for(exc) from exc
    return CanonWriteResponse(
        page=_page_out(result.page, item=result.item, include_body=True),
        revision=result.revision,
        embedded=result.embedded,
    )


@router.post("/pages/{slug}/publish", response_model=CanonWriteResponse, dependencies=_WRITE)
async def post_canon_publish(
    request: Request,
    slug: str,
    db: AsyncSession = Depends(get_db),
) -> CanonWriteResponse:
    try:
        result = await publish_canon_page(
            db,
            tenant_id=request.state.tenant_id,
            author_subject=_author_subject(request),
            slug=slug,
            embedder=request.app.state.embedder,
            llm=request.app.state.llm,
        )
    except (CanonError, CanonAuthoringError) as exc:
        raise _raise_for(exc) from exc
    return CanonWriteResponse(
        page=_page_out(result.page, item=result.item, include_body=True),
        revision=result.revision,
        embedded=result.embedded,
    )


@router.post("/pages/{slug}/ratify", response_model=CanonWriteResponse, dependencies=_WRITE)
async def post_canon_ratify(
    request: Request,
    slug: str,
    db: AsyncSession = Depends(get_db),
) -> CanonWriteResponse:
    try:
        result = await ratify_canon_page(
            db,
            tenant_id=request.state.tenant_id,
            author_subject=_author_subject(request),
            slug=slug,
        )
    except (CanonError, CanonAuthoringError) as exc:
        raise _raise_for(exc) from exc
    return CanonWriteResponse(
        page=_page_out(result.page, item=result.item, include_body=True),
        revision=result.revision,
        embedded=result.embedded,
    )


@router.post("/pages/{slug}/archive", response_model=CanonWriteResponse, dependencies=_WRITE)
async def post_canon_archive(
    request: Request,
    slug: str,
    db: AsyncSession = Depends(get_db),
) -> CanonWriteResponse:
    try:
        result = await archive_canon_page(
            db,
            tenant_id=request.state.tenant_id,
            author_subject=_author_subject(request),
            slug=slug,
        )
    except (CanonError, CanonAuthoringError) as exc:
        raise _raise_for(exc) from exc
    return CanonWriteResponse(
        page=_page_out(result.page, item=result.item, include_body=True),
        revision=result.revision,
        embedded=result.embedded,
    )


@router.get(
    "/pages/{slug}/revisions",
    response_model=CanonRevisionListResponse,
    dependencies=_READ,
)
async def get_canon_revisions(
    request: Request,
    slug: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> CanonRevisionListResponse:
    try:
        rows = await list_canon_page_revisions(
            db, tenant_id=request.state.tenant_id, slug=slug, limit=limit, offset=offset
        )
    except (CanonError, CanonAuthoringError) as exc:
        raise _raise_for(exc) from exc
    return CanonRevisionListResponse(
        slug=slug,
        revisions=[CanonRevisionOut.model_validate(row) for row in rows],
        total=len(rows),
    )


@router.get(
    "/pages/{slug}/revisions/{revision}",
    response_model=CanonRevisionOut,
    dependencies=_READ,
)
async def get_canon_revision(
    request: Request,
    slug: str,
    revision: int,
    db: AsyncSession = Depends(get_db),
) -> CanonRevisionOut:
    try:
        row = await get_canon_page_revision(
            db, tenant_id=request.state.tenant_id, slug=slug, revision=revision
        )
    except (CanonError, CanonAuthoringError) as exc:
        raise _raise_for(exc) from exc
    return CanonRevisionOut.model_validate(row)


# --- Tier 0 and tier 2 read surfaces ---------------------------------------
#
# The MCP read tool is a thin client over these three routes, so every cap stays
# in `services/canon.py`. A route that re-implemented a limit would be a second
# owner of the same number, which is the drift the design forbids.


@router.get("/digest", response_model=CanonDigestOut, dependencies=_READ)
async def get_digest(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> CanonDigestOut:
    """Return the tenant digest: identity, rules, and a capped page index.

    This is the always-served tier. It is bounded by the canon service, and a
    truncated digest carries the cursor to resume from.
    """
    try:
        digest = await get_canon_digest(db, tenant_id=request.state.tenant_id)
    except CanonError as exc:
        raise _raise_for(exc) from exc
    return CanonDigestOut(
        text=digest.text,
        char_count=digest.char_count,
        page_count=digest.page_count,
        truncated=digest.truncated,
        next=digest.next,
    )


@router.get("/search", response_model=CanonSearchResponse, dependencies=_READ)
async def search_canon(
    request: Request,
    q: str = Query(..., min_length=1, max_length=500),
    limit: int | None = Query(None, ge=1, le=8),
    db: AsyncSession = Depends(get_db),
) -> CanonSearchResponse:
    """Search published canon pages and return capped snippets, never bodies."""
    pages = await load_canon_pages(db, tenant_id=request.state.tenant_id, status="published")
    hits = search_canon_pages(q, pages, limit=limit)
    return CanonSearchResponse(
        query=q,
        hits=[
            CanonSearchHitOut(
                slug=hit.slug,
                section=hit.section,
                summary=hit.summary,
                snippet=hit.snippet,
                score=hit.score,
                truncated=hit.truncated,
            )
            for hit in hits
        ],
        total=len(hits),
    )


@router.get("/pages/{slug}/slice", response_model=CanonPageSliceOut, dependencies=_READ)
async def get_canon_page_slice(
    request: Request,
    slug: str,
    max_chars: int | None = Query(None, ge=1),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> CanonPageSliceOut:
    """Return one capped window of a page body.

    This exists so an agent never receives a whole page by default: the caller
    asks for a window, the service clamps it, and a cut window reports the
    cursor for the next one.
    """
    try:
        sliced = await read_canon_page_slice(
            db,
            tenant_id=request.state.tenant_id,
            slug=slug,
            max_chars=clamp_page_max_chars(max_chars),
            offset=offset,
        )
    except (CanonError, CanonValidationError) as exc:
        raise _raise_for(exc) from exc
    return CanonPageSliceOut(
        slug=sliced.slug,
        section=sliced.section,
        summary=sliced.summary,
        body=sliced.body,
        total_char_count=sliced.total_char_count,
        truncated=sliced.truncated,
        next=sliced.next,
    )
