"""Admin endpoints for tenant and control-plane operations."""
import hashlib
import json
import os
import secrets
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Body, Depends, File, Form, Header, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.job import Job
from app.schemas.bundle import AdminImportResponse, AdminJobResponse
from app.auth import hash_secret
from app.schemas.memory import (
    McpOAuthClientRegisterRequest,
    McpOAuthClientRegisterResponse,
    McpOAuthClientRevokeResponse,
    McpOAuthClientSummary,
)
from app.services.bundle import (
    RESTORE_JOB_TYPE,
    BundleValidationError,
    create_restore_job,
    materialize_bundle_upload_artifacts,
    parse_bundle_archive,
    retry_restore_job,
    serialize_admin_job,
    tenant_has_state,
)

router = APIRouter(prefix="/admin", tags=["admin"])

_ADMIN_SECRET = os.environ.get("PALACEOFTRUTH_ADMIN_SECRET", "")


def _normalize_tenant_id(value: str) -> str:
    tenant_id = value.strip()
    if not tenant_id:
        raise ValueError("tenant_id must not be blank")
    return tenant_id


def _tenant_id_from_path(value: str) -> str:
    try:
        return _normalize_tenant_id(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _verify_admin(x_admin_secret: str | None = Header(None, alias="X-Admin-Secret")) -> None:
    if not _ADMIN_SECRET:
        raise HTTPException(status_code=503, detail="Admin secret not configured")
    if not x_admin_secret or not secrets.compare_digest(x_admin_secret, _ADMIN_SECRET):
        raise HTTPException(status_code=403, detail="Invalid admin secret")


class RegisterTenantRequest(BaseModel):
    tenant_id: str
    description: str | None = None

    @field_validator("tenant_id")
    @classmethod
    def tenant_id_not_blank(cls, value: str) -> str:
        return _normalize_tenant_id(value)

    @field_validator("description")
    @classmethod
    def description_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        description = value.strip()
        if not description:
            raise ValueError("description must not be blank")
        return description


class TenantApiKeySummary(BaseModel):
    id: uuid.UUID
    tenant_id: str
    description: str | None = None
    created_at: datetime
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None
    status: Literal["active", "revoked"]


class RegisterTenantResponse(BaseModel):
    tenant_id: str
    created: bool
    api_key: str | None = None  # raw key — returned once only, never stored
    active_key: TenantApiKeySummary
    active_key_count: int


class TenantApiKeyListResponse(BaseModel):
    tenant_id: str
    active_key_count: int
    keys: list[TenantApiKeySummary]


class RotateTenantApiKeyRequest(BaseModel):
    description: str | None = None
    revoke_existing: bool = True

    @field_validator("description")
    @classmethod
    def description_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        description = value.strip()
        if not description:
            raise ValueError("description must not be blank")
        return description


class RotateTenantApiKeyResponse(BaseModel):
    tenant_id: str
    api_key: str
    revoked_count: int
    active_key: TenantApiKeySummary


class RevokeTenantApiKeyResponse(BaseModel):
    tenant_id: str
    revoked: bool
    key: TenantApiKeySummary


class TenantApiKeyAuditEventSummary(BaseModel):
    id: uuid.UUID
    tenant_id: str
    api_key_id: uuid.UUID | None = None
    event_type: str
    actor_type: str
    decision: str
    details: dict
    created_at: datetime


class TenantApiKeyAuditListResponse(BaseModel):
    tenant_id: str
    events: list[TenantApiKeyAuditEventSummary]


def _serialize_mcp_oauth_client(row) -> McpOAuthClientSummary:
    metadata = row["metadata"] or {}
    allowed_scopes = row["allowed_scopes"] or []
    if not isinstance(metadata, dict):
        metadata = {}
    if not isinstance(allowed_scopes, list):
        allowed_scopes = []
    return McpOAuthClientSummary(
        id=row["id"],
        tenant_id=row["tenant_id"],
        client_key=row["client_key"],
        display_name=row["display_name"],
        allowed_scopes=allowed_scopes,
        metadata=metadata,
        token_ttl_seconds=row["oauth_token_ttl_seconds"],
        created_at=row.get("created_at"),
        last_seen_at=row.get("last_seen_at"),
        revoked_at=row["oauth_revoked_at"],
    )


def _serialize_api_key(row) -> TenantApiKeySummary:
    revoked_at = row["revoked_at"]
    return TenantApiKeySummary(
        id=row["id"],
        tenant_id=row["tenant_id"],
        description=row["description"],
        created_at=row["created_at"],
        revoked_at=revoked_at,
        last_used_at=row["last_used_at"],
        status="revoked" if revoked_at else "active",
    )


def _serialize_audit_event(row) -> TenantApiKeyAuditEventSummary:
    return TenantApiKeyAuditEventSummary(
        id=row["id"],
        tenant_id=row["tenant_id"],
        api_key_id=row["api_key_id"],
        event_type=row["event_type"],
        actor_type=row["actor_type"],
        decision=row["decision"],
        details=row["details"] or {},
        created_at=row["created_at"],
    )


async def _list_api_key_rows(db: AsyncSession, *, tenant_id: str) -> list[dict]:
    result = await db.execute(
        text(
            "SELECT id, tenant_id, description, created_at, revoked_at "
            ", last_used_at "
            "FROM api_keys "
            "WHERE tenant_id = :tenant_id "
            "ORDER BY created_at DESC"
        ),
        {"tenant_id": tenant_id},
    )
    return result.mappings().all()


async def _active_api_key_row(db: AsyncSession, *, tenant_id: str) -> dict | None:
    result = await db.execute(
        text(
            "SELECT id, tenant_id, description, created_at, revoked_at "
            ", last_used_at "
            "FROM api_keys "
            "WHERE tenant_id = :tenant_id AND revoked_at IS NULL "
            "ORDER BY created_at DESC "
            "LIMIT 1"
        ),
        {"tenant_id": tenant_id},
    )
    return result.mappings().one_or_none()


async def _insert_api_key(
    db: AsyncSession,
    *,
    tenant_id: str,
    description: str | None,
) -> tuple[str, dict]:
    raw_key = secrets.token_hex(32)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    result = await db.execute(
        text(
            "INSERT INTO api_keys (tenant_id, key_hash, description) "
            "VALUES (:tenant_id, :key_hash, :description) "
            "RETURNING id, tenant_id, description, created_at, revoked_at, last_used_at"
        ),
        {
            "tenant_id": tenant_id,
            "key_hash": key_hash,
            "description": description,
        },
    )
    return raw_key, result.mappings().one()


async def _record_api_key_audit_event(
    db: AsyncSession,
    *,
    tenant_id: str,
    api_key_id: uuid.UUID | None,
    event_type: str,
    decision: str,
    details: dict | None = None,
) -> dict:
    result = await db.execute(
        text(
            "INSERT INTO api_key_audit_events "
            "(tenant_id, api_key_id, event_type, actor_type, decision, details) "
            "VALUES (:tenant_id, :api_key_id, :event_type, 'admin', :decision, CAST(:details AS jsonb)) "
            "RETURNING id, tenant_id, api_key_id, event_type, actor_type, decision, details, created_at"
        ),
        {
            "tenant_id": tenant_id,
            "api_key_id": api_key_id,
            "event_type": event_type,
            "decision": decision,
            "details": json.dumps(details or {}),
        },
    )
    return result.mappings().one()


@router.post(
    "/tenants/register",
    response_model=RegisterTenantResponse,
    status_code=200,
    dependencies=[Depends(_verify_admin)],
)
async def register_tenant(
    body: RegisterTenantRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> RegisterTenantResponse:
    """Create an API key for a tenant if one does not already exist.

    Returns the raw key once. The caller must store it securely (e.g. in Vault).
    If the tenant already has an active key, do not mint another one.
    """
    existing = await _active_api_key_row(db, tenant_id=body.tenant_id)
    if existing is not None:
        rows = await _list_api_key_rows(db, tenant_id=body.tenant_id)
        await _record_api_key_audit_event(
            db,
            tenant_id=body.tenant_id,
            api_key_id=existing["id"],
            event_type="register_replay",
            decision="reused_existing_active_key",
            details={"active_key_count": sum(1 for row in rows if row["revoked_at"] is None)},
        )
        await db.commit()
        return RegisterTenantResponse(
            tenant_id=body.tenant_id,
            created=False,
            api_key=None,
            active_key=_serialize_api_key(existing),
            active_key_count=sum(1 for row in rows if row["revoked_at"] is None),
        )

    raw_key, row = await _insert_api_key(
        db,
        tenant_id=body.tenant_id,
        description=body.description,
    )
    await _record_api_key_audit_event(
        db,
        tenant_id=body.tenant_id,
        api_key_id=row["id"],
        event_type="register_created",
        decision="created_new_active_key",
        details={"description_present": body.description is not None},
    )
    await db.commit()
    response.status_code = 201
    return RegisterTenantResponse(
        tenant_id=body.tenant_id,
        created=True,
        api_key=raw_key,
        active_key=_serialize_api_key(row),
        active_key_count=1,
    )


@router.get(
    "/tenants/{tenant_id}/api-keys",
    response_model=TenantApiKeyListResponse,
    dependencies=[Depends(_verify_admin)],
)
async def list_tenant_api_keys(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
) -> TenantApiKeyListResponse:
    tenant_id = _tenant_id_from_path(tenant_id)
    rows = await _list_api_key_rows(db, tenant_id=tenant_id)
    return TenantApiKeyListResponse(
        tenant_id=tenant_id,
        active_key_count=sum(1 for row in rows if row["revoked_at"] is None),
        keys=[_serialize_api_key(row) for row in rows],
    )


@router.get(
    "/tenants/{tenant_id}/api-keys/audit",
    response_model=TenantApiKeyAuditListResponse,
    dependencies=[Depends(_verify_admin)],
)
async def list_tenant_api_key_audit_events(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
) -> TenantApiKeyAuditListResponse:
    tenant_id = _tenant_id_from_path(tenant_id)
    result = await db.execute(
        text(
            "SELECT id, tenant_id, api_key_id, event_type, actor_type, decision, details, created_at "
            "FROM api_key_audit_events "
            "WHERE tenant_id = :tenant_id "
            "ORDER BY created_at DESC"
        ),
        {"tenant_id": tenant_id},
    )
    rows = result.mappings().all()
    return TenantApiKeyAuditListResponse(
        tenant_id=tenant_id,
        events=[_serialize_audit_event(row) for row in rows],
    )


@router.post(
    "/tenants/{tenant_id}/mcp-clients/register",
    response_model=McpOAuthClientRegisterResponse,
    status_code=201,
    dependencies=[Depends(_verify_admin)],
)
async def register_mcp_oauth_client(
    tenant_id: str,
    body: McpOAuthClientRegisterRequest,
    db: AsyncSession = Depends(get_db),
) -> McpOAuthClientRegisterResponse:
    tenant_id = _tenant_id_from_path(tenant_id)
    raw_secret = secrets.token_urlsafe(48)
    result = await db.execute(
        text(
            """
            INSERT INTO mcp_clients
                (tenant_id, client_key, display_name, allowed_scopes, metadata,
                 oauth_client_secret_hash, oauth_revoked_at, oauth_token_ttl_seconds)
            VALUES
                (:tenant_id, :client_key, :display_name, CAST(:allowed_scopes AS jsonb),
                 CAST(:metadata AS jsonb), :secret_hash, NULL, :token_ttl_seconds)
            ON CONFLICT (tenant_id, client_key) DO UPDATE
            SET display_name = EXCLUDED.display_name,
                allowed_scopes = EXCLUDED.allowed_scopes,
                metadata = EXCLUDED.metadata,
                oauth_client_secret_hash = EXCLUDED.oauth_client_secret_hash,
                oauth_revoked_at = NULL,
                oauth_token_ttl_seconds = EXCLUDED.oauth_token_ttl_seconds
            RETURNING id, tenant_id, client_key, display_name, allowed_scopes, metadata,
                      oauth_revoked_at, oauth_token_ttl_seconds
            """
        ),
        {
            "tenant_id": tenant_id,
            "client_key": body.client_key,
            "display_name": body.display_name,
            "allowed_scopes": json.dumps(body.allowed_scopes),
            "metadata": json.dumps(body.metadata),
            "secret_hash": hash_secret(raw_secret),
            "token_ttl_seconds": body.token_ttl_seconds,
        },
    )
    await db.commit()
    return McpOAuthClientRegisterResponse(
        tenant_id=tenant_id,
        client=_serialize_mcp_oauth_client(result.mappings().one()),
        client_secret=raw_secret,
    )


@router.post(
    "/tenants/{tenant_id}/mcp-clients/{client_id}/revoke",
    response_model=McpOAuthClientRevokeResponse,
    dependencies=[Depends(_verify_admin)],
)
async def revoke_mcp_oauth_client(
    tenant_id: str,
    client_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> McpOAuthClientRevokeResponse:
    tenant_id = _tenant_id_from_path(tenant_id)
    result = await db.execute(
        text(
            """
            UPDATE mcp_clients
            SET oauth_revoked_at = COALESCE(oauth_revoked_at, CURRENT_TIMESTAMP)
            WHERE tenant_id = :tenant_id AND id = :client_id
            RETURNING id, tenant_id, client_key, display_name, allowed_scopes, metadata,
                      oauth_revoked_at, oauth_token_ttl_seconds
            """
        ),
        {"tenant_id": tenant_id, "client_id": client_id},
    )
    row = result.mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="MCP OAuth client not found")
    await db.execute(
        text(
            "UPDATE mcp_oauth_access_tokens "
            "SET revoked_at = COALESCE(revoked_at, CURRENT_TIMESTAMP) "
            "WHERE tenant_id = :tenant_id AND client_id = :client_id"
        ),
        {"tenant_id": tenant_id, "client_id": client_id},
    )
    await db.commit()
    return McpOAuthClientRevokeResponse(tenant_id=tenant_id, client=_serialize_mcp_oauth_client(row))


@router.post(
    "/tenants/{tenant_id}/api-keys/rotate",
    response_model=RotateTenantApiKeyResponse,
    dependencies=[Depends(_verify_admin)],
)
async def rotate_tenant_api_key(
    tenant_id: str,
    body: RotateTenantApiKeyRequest = Body(default_factory=RotateTenantApiKeyRequest),
    db: AsyncSession = Depends(get_db),
) -> RotateTenantApiKeyResponse:
    tenant_id = _tenant_id_from_path(tenant_id)
    revoked_count = 0
    if body.revoke_existing:
        revoke_result = await db.execute(
            text(
                "UPDATE api_keys "
                "SET revoked_at = COALESCE(revoked_at, CURRENT_TIMESTAMP) "
                "WHERE tenant_id = :tenant_id AND revoked_at IS NULL "
                "RETURNING id"
            ),
            {"tenant_id": tenant_id},
        )
        revoked_count = len(revoke_result.fetchall())

    raw_key, row = await _insert_api_key(
        db,
        tenant_id=tenant_id,
        description=body.description,
    )
    await _record_api_key_audit_event(
        db,
        tenant_id=tenant_id,
        api_key_id=row["id"],
        event_type="rotate",
        decision="created_replacement_key",
        details={
            "revoked_count": revoked_count,
            "revoke_existing": body.revoke_existing,
            "description_present": body.description is not None,
        },
    )
    await db.commit()
    return RotateTenantApiKeyResponse(
        tenant_id=tenant_id,
        api_key=raw_key,
        revoked_count=revoked_count,
        active_key=_serialize_api_key(row),
    )


@router.post(
    "/tenants/{tenant_id}/api-keys/{key_id}/revoke",
    response_model=RevokeTenantApiKeyResponse,
    dependencies=[Depends(_verify_admin)],
)
async def revoke_tenant_api_key(
    tenant_id: str,
    key_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> RevokeTenantApiKeyResponse:
    tenant_id = _tenant_id_from_path(tenant_id)
    result = await db.execute(
        text(
            "UPDATE api_keys "
            "SET revoked_at = COALESCE(revoked_at, CURRENT_TIMESTAMP) "
            "WHERE tenant_id = :tenant_id AND id = :key_id "
            "RETURNING id, tenant_id, description, created_at, revoked_at, last_used_at"
        ),
        {
            "tenant_id": tenant_id,
            "key_id": key_id,
        },
    )
    row = result.mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Tenant API key not found")
    await _record_api_key_audit_event(
        db,
        tenant_id=tenant_id,
        api_key_id=row["id"],
        event_type="revoke",
        decision="key_marked_revoked_or_already_revoked",
        details={},
    )
    await db.commit()
    return RevokeTenantApiKeyResponse(
        tenant_id=tenant_id,
        revoked=True,
        key=_serialize_api_key(row),
    )


@router.post(
    "/bundles/import",
    response_model=AdminImportResponse,
    status_code=202,
    dependencies=[Depends(_verify_admin)],
)
async def import_bundle(
    request: Request,
    tenant_id: str = Form(...),
    bundle: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
) -> AdminImportResponse:
    if await tenant_has_state(db, tenant_id, include_api_keys=False):
        raise HTTPException(status_code=409, detail="Target tenant is not empty")

    try:
        bundle_bytes = await bundle.read()
        payload = parse_bundle_archive(bundle_bytes)
        payload = materialize_bundle_upload_artifacts(bundle_bytes, payload, tenant_id=tenant_id)
    except BundleValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    job = await create_restore_job(db, tenant_id=tenant_id, payload=payload)
    await request.app.state.arq_pool.enqueue_job("restore_bundle", job_id=str(job.id))
    return AdminImportResponse(job_id=job.id, tenant_id=tenant_id, status=job.status)


@router.get(
    "/jobs/{job_id}",
    response_model=AdminJobResponse,
    dependencies=[Depends(_verify_admin)],
)
async def get_admin_job(job_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> AdminJobResponse:
    job = await db.get(Job, job_id)
    if not job or job.job_type != RESTORE_JOB_TYPE:
        raise HTTPException(status_code=404, detail="Admin job not found")
    return serialize_admin_job(job)


@router.post(
    "/jobs/{job_id}/retry",
    response_model=AdminJobResponse,
    dependencies=[Depends(_verify_admin)],
)
async def retry_admin_job(
    job_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> AdminJobResponse:
    job = await db.get(Job, job_id)
    if not job or job.job_type != RESTORE_JOB_TYPE:
        raise HTTPException(status_code=404, detail="Admin job not found")
    if job.status not in ("failed", "cancelled"):
        raise HTTPException(status_code=409, detail=f"Job is {job.status}; only failed or cancelled jobs can be retried")
    job = await retry_restore_job(db, job)
    await request.app.state.arq_pool.enqueue_job("restore_bundle", job_id=str(job.id))
    return serialize_admin_job(job)


@router.post(
    "/jobs/{job_id}/cancel",
    response_model=AdminJobResponse,
    dependencies=[Depends(_verify_admin)],
)
async def cancel_admin_job(job_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> AdminJobResponse:
    job = await db.get(Job, job_id)
    if not job or job.job_type != RESTORE_JOB_TYPE:
        raise HTTPException(status_code=404, detail="Admin job not found")
    if job.status not in ("validated", "queued"):
        raise HTTPException(status_code=409, detail=f"Job is {job.status}; only validated or queued jobs can be cancelled")
    job.status = "cancelled"
    await db.commit()
    await db.refresh(job)
    return serialize_admin_job(job)
