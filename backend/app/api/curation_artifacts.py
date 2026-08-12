import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_auth_context, require_api_capability
from app.database import get_db
from app.schemas.curation_artifact import (
    CandidateCurationArtifactCreate,
    CandidateCurationArtifactListResponse,
    CandidateCurationArtifactOut,
    CandidatePromotionHandoffOut,
    CandidateCurationArtifactUpdate,
    ReviewInboxActionRequest,
    ReviewInboxActionResponse,
    ReviewInboxResponse,
)
from app.services.candidate_curation_promotion import render_candidate_promotion_handoff
from app.services.curation_artifacts import (
    CandidateCurationArtifactError,
    apply_review_inbox_action,
    create_candidate_curation_artifact,
    get_candidate_curation_artifact,
    list_review_inbox,
    list_candidate_curation_artifacts,
    update_candidate_curation_artifact,
)

router = APIRouter(
    prefix="/curation-artifacts",
    tags=["curation-artifacts"],
)


def _validation_error(exc: CandidateCurationArtifactError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


def _principal_id(request: Request) -> str:
    context = get_auth_context(request)
    return context.subject_id or str(context.client_id or context.client_key or context.auth_mode)


@router.get("", response_model=CandidateCurationArtifactListResponse, dependencies=[Depends(require_api_capability("read"))])
async def list_curation_artifacts(
    request: Request,
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> CandidateCurationArtifactListResponse:
    try:
        artifacts = await list_candidate_curation_artifacts(
            db,
            tenant_id=request.state.tenant_id,
            status=status_filter,
            limit=limit,
        )
    except CandidateCurationArtifactError as exc:
        raise _validation_error(exc) from exc
    return CandidateCurationArtifactListResponse(
        artifacts=[CandidateCurationArtifactOut.model_validate(row) for row in artifacts],
        total=len(artifacts),
    )


@router.post("", response_model=CandidateCurationArtifactOut, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_api_capability("write"))])
async def post_curation_artifact(
    body: CandidateCurationArtifactCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> CandidateCurationArtifactOut:
    try:
        artifact = await create_candidate_curation_artifact(
            db,
            tenant_id=request.state.tenant_id,
            body=body,
            principal_id=_principal_id(request),
        )
        await db.commit()
    except CandidateCurationArtifactError as exc:
        await db.rollback()
        raise _validation_error(exc) from exc
    return CandidateCurationArtifactOut.model_validate(artifact)


@router.get("/review-inbox", response_model=ReviewInboxResponse, dependencies=[Depends(require_api_capability("read"))])
async def get_review_inbox(
    request: Request,
    include_deferred: bool = False,
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> ReviewInboxResponse:
    return await list_review_inbox(
        db,
        tenant_id=request.state.tenant_id,
        include_deferred=include_deferred,
        limit=limit,
    )


@router.post("/review-inbox/actions", response_model=ReviewInboxActionResponse, dependencies=[Depends(require_api_capability("write"))])
async def post_review_inbox_action(
    body: ReviewInboxActionRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ReviewInboxActionResponse:
    context = get_auth_context(request)
    if body.action in {"accept", "reject"} and not context.has_capability("curation:approve"):
        raise HTTPException(status_code=403, detail="curation approval scope is required")
    try:
        artifacts = await apply_review_inbox_action(
            db,
            tenant_id=request.state.tenant_id,
            body=body,
            principal_id=_principal_id(request),
            can_approve=context.has_capability("curation:approve"),
        )
        await db.commit()
    except CandidateCurationArtifactError as exc:
        await db.rollback()
        raise _validation_error(exc) from exc
    return ReviewInboxActionResponse(
        action=body.action,
        artifacts=[CandidateCurationArtifactOut.model_validate(artifact) for artifact in artifacts],
        updated=len(artifacts),
    )


@router.get("/{artifact_id}", response_model=CandidateCurationArtifactOut, dependencies=[Depends(require_api_capability("read"))])
async def get_curation_artifact(
    artifact_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> CandidateCurationArtifactOut:
    artifact = await get_candidate_curation_artifact(
        db,
        tenant_id=request.state.tenant_id,
        artifact_id=artifact_id,
    )
    if artifact is None:
        raise HTTPException(status_code=404, detail="Candidate curation artifact not found")
    return CandidateCurationArtifactOut.model_validate(artifact)


@router.get("/{artifact_id}/promotion-handoff", response_model=CandidatePromotionHandoffOut, dependencies=[Depends(require_api_capability("read"))])
async def get_curation_artifact_promotion_handoff(
    artifact_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> CandidatePromotionHandoffOut:
    artifact = await get_candidate_curation_artifact(
        db,
        tenant_id=request.state.tenant_id,
        artifact_id=artifact_id,
    )
    if artifact is None:
        raise HTTPException(status_code=404, detail="Candidate curation artifact not found")
    try:
        return CandidatePromotionHandoffOut.model_validate(render_candidate_promotion_handoff(artifact))
    except CandidateCurationArtifactError as exc:
        raise _validation_error(exc) from exc


@router.patch("/{artifact_id}", response_model=CandidateCurationArtifactOut, dependencies=[Depends(require_api_capability("write"))])
async def patch_curation_artifact(
    artifact_id: uuid.UUID,
    body: CandidateCurationArtifactUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> CandidateCurationArtifactOut:
    artifact = await get_candidate_curation_artifact(
        db,
        tenant_id=request.state.tenant_id,
        artifact_id=artifact_id,
    )
    if artifact is None:
        raise HTTPException(status_code=404, detail="Candidate curation artifact not found")
    context = get_auth_context(request)
    if body.status in {"promoted", "approved", "rejected"} and not context.has_capability(
        "curation:approve"
    ):
        raise HTTPException(status_code=403, detail="curation approval scope is required")
    try:
        updated = await update_candidate_curation_artifact(
            db,
            artifact=artifact,
            body=body,
            principal_id=_principal_id(request),
            can_approve=context.has_capability("curation:approve"),
        )
        await db.commit()
    except CandidateCurationArtifactError as exc:
        await db.rollback()
        raise _validation_error(exc) from exc
    return CandidateCurationArtifactOut.model_validate(updated)
