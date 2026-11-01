"""Canon authoring API.

Read is open to any authorized principal, because agents must read canon on
connect. Write is closed to everything except a human browser session: the
`require_human_author` dependency runs before the capability check, so an agent
holding a write grant still receives `403 canon_human_author_only` rather than
an insufficient-scope error. Agents contribute through
`candidate_canon_edit` artifacts instead.
"""

import json
import logging
import uuid

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_auth_context, require_api_capability, require_human_author
from app.database import get_db
from app.schemas.canon import (
    CanonDigestOut,
    CanonImportCommitOut,
    CanonImportFileOut,
    CanonImportPlanOut,
    CanonImportResultOut,
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
    canon_revision_counts,
    create_canon_page,
    get_canon_page_detail,
    get_canon_page_revision,
    list_canon_page_revisions,
    list_canon_pages,
    publish_canon_page,
    ratify_canon_page,
    update_canon_page,
)
from app.services.canon_import import (
    ImportFile,
    check_import_batch_shape,
    check_import_upload,
    commit_import,
    parse_import_file,
    plan_import,
    validate_import_batch,
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
    revision_counts = await canon_revision_counts(
        db, tenant_id=request.state.tenant_id, page_ids=[page.id for page, _item in rows]
    )
    return CanonPageListResponse(
        pages=[
            _page_out(page, item=item, revision_count=revision_counts.get(page.id, 0))
            for page, item in rows
        ],
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


# --- Folder import ---------------------------------------------------------
#
# Preview and commit are one operator decision in two phases. Both sit behind
# `_WRITE`: an import is authoring, so the human-author wall applies unchanged.
# The parse layer is pure, so a preview costs no writes at all.

_IMPORT_APPLIED = ("created", "updated", "renamed")


async def _read_import_uploads(files: list[UploadFile]) -> list[ImportFile]:
    """Read, cap-check, and parse the uploads; a bad file is refused by name.

    The type and size checks run before the body is decoded, so one oversize or
    unsupported file does not get parsed and does not cost the rest of the folder
    its place in the response — the operator gets an error naming that file.
    """
    parsed: list[ImportFile] = []
    total = 0
    for upload in files:
        data = await upload.read()
        path = upload.filename or "upload"
        check_import_upload(path, len(data))
        total += len(data)
        check_import_batch_shape(file_count=len(parsed) + 1, total_bytes=total)
        parsed.append(parse_import_file(path, data))
    validate_import_batch(parsed, extensions_checked=False)
    return parsed


def _parse_decisions(raw: str) -> dict[str, str]:
    """Parse the operator's decisions: a JSON object of slug -> apply|skip|rename.

    Malformed JSON is a client error, so it is raised as a canon validation error
    and mapped to 422 rather than escaping as a 500 from ``json.loads``.
    """
    try:
        loaded = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise CanonValidationError(f"decisions must be a JSON object: {exc}") from exc
    if not isinstance(loaded, dict):
        raise CanonValidationError("decisions must be a JSON object of slug -> decision")
    decisions: dict[str, str] = {}
    for slug, decision in loaded.items():
        if not isinstance(decision, str):
            raise CanonValidationError(f"decision for {slug!r} must be a string")
        decisions[str(slug)] = decision
    return decisions


@router.post("/import/folder/preview", response_model=CanonImportPlanOut, dependencies=_WRITE)
async def post_canon_import_preview(
    request: Request,
    files: list[UploadFile] = File(...),
    db: AsyncSession = Depends(get_db),
) -> CanonImportPlanOut:
    """Plan a folder import without writing anything.

    Returns what each file would do and a content-addressed ``plan_id`` the
    commit must quote, so a commit cannot apply a folder nobody previewed.
    """
    try:
        parsed = await _read_import_uploads(files)
        plan = await plan_import(db, tenant_id=request.state.tenant_id, files=parsed)
    except (CanonError, CanonAuthoringError) as exc:
        raise _raise_for(exc) from exc
    return CanonImportPlanOut(
        plan_id=plan.plan_id,
        files=[
            CanonImportFileOut(
                path=entry.path,
                slug=entry.slug,
                title=entry.title,
                section=entry.section,
                action=entry.action,
                content_hash=entry.content_hash,
                existing_status=entry.existing_status,
                reason=entry.reason,
            )
            for entry in plan.files
        ],
        counts=plan.counts,
    )


@router.post("/import/folder/commit", response_model=CanonImportCommitOut, dependencies=_WRITE)
async def post_canon_import_commit(
    request: Request,
    plan_id: str = Form(...),
    decisions: str = Form("{}"),
    files: list[UploadFile] = File(...),
    db: AsyncSession = Depends(get_db),
) -> CanonImportCommitOut:
    """Apply a previewed folder import as one transaction.

    ``decisions`` is a JSON object of slug -> ``apply`` | ``skip`` | ``rename``,
    the operator's answer to each conflict. The whole batch lands or none of it
    does; a stale ``plan_id`` is refused rather than applied blind.
    """
    try:
        chosen = _parse_decisions(decisions)
        parsed = await _read_import_uploads(files)
        results = await commit_import(
            db,
            tenant_id=request.state.tenant_id,
            author_subject=_author_subject(request),
            files=parsed,
            plan_id=plan_id,
            decisions=chosen,
        )
    except (CanonError, CanonAuthoringError) as exc:
        raise _raise_for(exc) from exc
    return CanonImportCommitOut(
        results=[
            CanonImportResultOut(slug=result.slug, action=result.action, new_slug=result.new_slug)
            for result in results
        ],
        applied=sum(1 for result in results if result.action in _IMPORT_APPLIED),
        skipped=sum(1 for result in results if result.action == "skipped"),
    )
