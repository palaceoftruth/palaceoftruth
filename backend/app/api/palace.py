from __future__ import annotations

import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import hash_secret, require_api_capability
from app.database import async_session, get_db
from app.mcp_scopes import serialize_mcp_scope_catalog
from app.models.palace import PalaceRun, SyncRun, SyncSource
from app.schemas.memory import (
    BrowserExtensionTokenIssueRequest,
    BrowserExtensionTokenIssueResponse,
    McpClientConfigSnippets,
    McpOAuthClientListResponse,
    McpOAuthClientRegisterRequest,
    McpOAuthClientRegisterResponse,
    McpOAuthClientRevokeResponse,
    McpOAuthClientSummary,
)
from app.schemas.palace import (
    PalaceAnswerAuditReport,
    PalaceClaimReviewRequest,
    PalaceClaimSupportSummary,
    PalaceClaimSupportReport,
    PalaceControlTower,
    PalaceItemSourceSummary,
    PalaceTemporalFactSummary,
    PalaceOverview,
    PalacePinRequest,
    PalaceRetrieveRequest,
    PalaceRetrieveResponse,
    PalaceRoomDetail,
    PalaceRoomUpdate,
    PalaceRunSummary,
    SyncSourceDeleteResponse,
    SyncRunSummary,
    SyncSourceCreate,
    SyncSourceSummary,
    SyncSourceUpdate,
)
from app.services.fact_registry import list_temporal_facts
from app.services.palace import (
    build_control_tower,
    build_overview,
    create_or_get_palace_run,
    create_or_get_sync_run,
    create_sync_source,
    delete_sync_source,
    get_room_detail,
    list_palace_runs,
    list_sync_runs,
    list_sync_sources,
    pin_room_membership,
    restore_sync_source,
    run_palace_run,
    run_sync_run,
    retrieve_palace,
    unpin_room_membership,
    update_room,
    update_sync_source,
)
from app.services.source_compiler import (
    ClaimReviewError,
    get_answer_audit_report,
    get_claim_support_report,
    get_item_source_summary,
    review_decision_claim,
)
from app.workers.queues import enqueue_palace_job

router = APIRouter(prefix="/palace", tags=["palace"])
logger = logging.getLogger(__name__)


def _serialize_claim_support_summary(claim) -> dict:
    return {
        "id": claim.id,
        "claim_key": claim.claim_key,
        "claim_text": claim.claim_text,
        "claim_type": claim.claim_type,
        "confidence": claim.confidence,
        "status": claim.status,
        "support_state": claim.support_state,
        "warning": claim.warning,
        "metadata": claim.metadata,
        "sources": [
            {
                "id": source.id,
                "source_record_id": source.source_record_id,
                "source_chunk_id": source.source_chunk_id,
                "source_item_id": source.source_item_id,
                "source_record_status": source.source_record_status,
                "support_role": source.support_role,
                "status": source.status,
                "source_digest": source.source_digest,
                "source_span": source.source_span,
            }
            for source in claim.sources
        ],
    }


def _serialize_answer_audit_item(item) -> dict:
    return {
        "object_type": item.object_type,
        "object_id": item.object_id,
        "object_key": item.object_key,
        "object_text": item.object_text,
        "claim_type": item.claim_type,
        "claim_status": item.claim_status,
        "support_state": item.support_state,
        "audit_state": item.audit_state,
        "warning": item.warning,
        "promotion_status": item.promotion_status,
        "source_count": item.source_count,
        "metadata": item.metadata,
        "sources": [
            {
                "source_record_id": source.source_record_id,
                "source_chunk_id": source.source_chunk_id,
                "source_item_id": source.source_item_id,
                "source_record_status": source.source_record_status,
                "support_role": source.support_role,
                "support_status": source.support_status,
                "source_digest": source.source_digest,
                "source_span": source.source_span,
            }
            for source in item.sources
        ],
    }


def _serialize_mcp_client(row) -> McpOAuthClientSummary:
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
        request_count=int(row.get("request_count") or 0),
        success_count=int(row.get("success_count") or 0),
        denied_count=int(row.get("denied_count") or 0),
        error_count=int(row.get("error_count") or 0),
        last_request_at=row.get("last_request_at"),
        revoked_at=row["oauth_revoked_at"],
    )


def _config_snippets(request: Request, *, client_key: str = "<client_key>", scopes: list[str] | None = None) -> McpClientConfigSnippets:
    base_url = str(request.base_url).rstrip("/")
    api_base = f"{base_url}/api/v1"
    mcp_url = f"{base_url}/mcp"
    token_url = f"{api_base}/memory/mcp/oauth/token"
    scope_arg = " ".join(scopes or ["read"])
    return McpClientConfigSnippets(
        codex_stdio_toml=(
            "[mcp_servers.palaceoftruth-memory]\n"
            'command = "uv"\n'
            'args = ["--directory", "backend", "run", "python", "scripts/palaceoftruth_mcp.py"]\n\n'
            "[mcp_servers.palaceoftruth-memory.env]\n"
            f'PALACEOFTRUTH_API_BASE_URL = "{api_base}"\n'
            'PALACEOFTRUTH_API_KEY = "set-from-your-secret-manager"\n'
            f'PALACEOFTRUTH_MCP_CLIENT_KEY = "{client_key}"\n'
        ),
        http_oauth_toml=(
            "[mcp_servers.palaceoftruth-memory]\n"
            f'url = "{mcp_url}"\n'
            'bearer_token_env_var = "PALACEOFTRUTH_MCP_BEARER_TOKEN"\n'
        ),
        oauth_token_command=(
            "read -rsp 'Palace MCP client secret: ' PALACEOFTRUTH_MCP_CLIENT_SECRET; echo\n"
            "export PALACEOFTRUTH_MCP_BEARER_TOKEN=$(curl -fsS -X POST "
            f"{token_url} "
            "-d grant_type=client_credentials "
            f"--data-urlencode client_id={client_key!r} "
            '"--data-urlencode client_secret=${PALACEOFTRUTH_MCP_CLIENT_SECRET}" '
            f"--data-urlencode scope={scope_arg!r} "
            f"--data-urlencode resource={mcp_url!r} "
            "| python3 -c 'import json,sys; print(json.load(sys.stdin)[\"access_token\"])')"
        ),
        legacy_api_key_toml=(
            "[mcp_servers.palaceoftruth-memory]\n"
            f'url = "{mcp_url}"\n\n'
            "[mcp_servers.palaceoftruth-memory.headers]\n"
            'X-API-Key = "set-from-your-secret-manager"\n'
        ),
        secret_handling_note=(
            "The client_secret is returned once. Store it in a secret manager or paste it into a hidden prompt; "
            "do not commit it, put it in shell history, or add it directly to Codex config."
        ),
    )


async def _run_sync_inline(app, sync_run_id: uuid.UUID) -> None:
    async with async_session() as db:
        status, _error = await run_sync_run(
            db,
            run_id=sync_run_id,
            embedder=app.state.embedder,
            llm=app.state.llm,
        )
        if status != "completed":
            return

        sync_run = await db.get(SyncRun, sync_run_id)
        if sync_run is None or sync_run.generation <= 0:
            return

        palace_run, created = await create_or_get_palace_run(
            db,
            tenant_id=sync_run.tenant_id,
            triggered_by="sync",
            source_sync_run_id=sync_run.id,
        )
        if created:
            await run_palace_run(db, run_id=palace_run.id)


@router.get("", response_model=PalaceOverview, dependencies=[Depends(require_api_capability("read"))])
async def palace_overview(request: Request, db: AsyncSession = Depends(get_db)) -> PalaceOverview:
    return await build_overview(db, request.state.tenant_id)


@router.get("/control-tower", response_model=PalaceControlTower, dependencies=[Depends(require_api_capability("read"))])
async def palace_control_tower(request: Request, db: AsyncSession = Depends(get_db)) -> PalaceControlTower:
    return await build_control_tower(db, request.state.tenant_id, arq_pool=request.app.state.arq_pool)


@router.get("/mcp-clients", response_model=McpOAuthClientListResponse, dependencies=[Depends(require_api_capability("admin"))])
async def list_palace_mcp_clients(request: Request, db: AsyncSession = Depends(get_db)) -> McpOAuthClientListResponse:
    tenant_id = request.state.tenant_id
    rows = (
        await db.execute(
            text(
                """
                SELECT c.id, c.tenant_id, c.client_key, c.display_name, c.allowed_scopes, c.metadata,
                       c.oauth_revoked_at, c.oauth_token_ttl_seconds, c.created_at, c.last_seen_at,
                       COUNT(e.id) AS request_count,
                       COUNT(e.id) FILTER (WHERE e.status = 'success') AS success_count,
                       COUNT(e.id) FILTER (WHERE e.status = 'denied') AS denied_count,
                       COUNT(e.id) FILTER (WHERE e.status = 'error') AS error_count,
                       MAX(e.created_at) AS last_request_at
                FROM mcp_clients c
                LEFT JOIN mcp_request_audit_events e ON e.client_id = c.id AND e.tenant_id = c.tenant_id
                WHERE c.tenant_id = :tenant_id
                GROUP BY c.id, c.tenant_id, c.client_key, c.display_name, c.allowed_scopes, c.metadata,
                         c.oauth_revoked_at, c.oauth_token_ttl_seconds, c.created_at, c.last_seen_at
                ORDER BY COALESCE(MAX(e.created_at), c.last_seen_at, c.created_at) DESC
                """
            ),
            {"tenant_id": tenant_id},
        )
    ).mappings().all()
    return McpOAuthClientListResponse(
        tenant_id=tenant_id,
        clients=[_serialize_mcp_client(row) for row in rows],
        config_snippets=_config_snippets(request),
        scope_catalog=serialize_mcp_scope_catalog(),
    )


@router.post("/mcp-clients/register", response_model=McpOAuthClientRegisterResponse, status_code=201, dependencies=[Depends(require_api_capability("admin"))])
async def register_palace_mcp_client(
    body: McpOAuthClientRegisterRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> McpOAuthClientRegisterResponse:
    tenant_id = request.state.tenant_id
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
                      oauth_revoked_at, oauth_token_ttl_seconds, created_at, last_seen_at
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
    client = _serialize_mcp_client(result.mappings().one())
    return McpOAuthClientRegisterResponse(
        tenant_id=tenant_id,
        client=client,
        client_secret=raw_secret,
        config_snippets=_config_snippets(request, client_key=client.client_key, scopes=client.allowed_scopes),
    )


@router.post("/mcp-clients/{client_id}/revoke", response_model=McpOAuthClientRevokeResponse, dependencies=[Depends(require_api_capability("admin"))])
async def revoke_palace_mcp_client(
    client_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> McpOAuthClientRevokeResponse:
    tenant_id = request.state.tenant_id
    result = await db.execute(
        text(
            """
            UPDATE mcp_clients
            SET oauth_revoked_at = COALESCE(oauth_revoked_at, CURRENT_TIMESTAMP)
            WHERE tenant_id = :tenant_id AND id = :client_id
            RETURNING id, tenant_id, client_key, display_name, allowed_scopes, metadata,
                      oauth_revoked_at, oauth_token_ttl_seconds, created_at, last_seen_at
            """
        ),
        {"tenant_id": tenant_id, "client_id": client_id},
    )
    row = result.mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="MCP client not found")
    await db.execute(
        text(
            "UPDATE mcp_oauth_access_tokens "
            "SET revoked_at = COALESCE(revoked_at, CURRENT_TIMESTAMP) "
            "WHERE tenant_id = :tenant_id AND client_id = :client_id"
        ),
        {"tenant_id": tenant_id, "client_id": client_id},
    )
    await db.commit()
    return McpOAuthClientRevokeResponse(tenant_id=tenant_id, client=_serialize_mcp_client(row))


@router.post("/browser-extension-tokens", response_model=BrowserExtensionTokenIssueResponse, status_code=201, dependencies=[Depends(require_api_capability("admin"))])
async def issue_browser_extension_token(
    body: BrowserExtensionTokenIssueRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BrowserExtensionTokenIssueResponse:
    tenant_id = request.state.tenant_id
    scopes = ["capture:write", "capture:job:read"]
    access_token = secrets.token_urlsafe(48)
    client_key = f"browser-extension:{secrets.token_urlsafe(18)}"
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=body.token_ttl_seconds)
    metadata = {
        "client_type": "browser_extension",
        "extension_version": body.extension_version,
        "token_model": "public_capture_token",
    }
    result = await db.execute(
        text(
            """
            INSERT INTO mcp_clients
                (tenant_id, client_key, display_name, allowed_scopes, metadata,
                 oauth_client_secret_hash, oauth_revoked_at, oauth_token_ttl_seconds)
            VALUES
                (:tenant_id, :client_key, :display_name, CAST(:allowed_scopes AS jsonb),
                 CAST(:metadata AS jsonb), NULL, NULL, :token_ttl_seconds)
            RETURNING id
            """
        ),
        {
            "tenant_id": tenant_id,
            "client_key": client_key,
            "display_name": body.display_name,
            "allowed_scopes": json.dumps(scopes),
            "metadata": json.dumps(metadata),
            "token_ttl_seconds": body.token_ttl_seconds,
        },
    )
    client_id = result.mappings().one()["id"]
    await db.execute(
        text(
            """
            INSERT INTO mcp_oauth_access_tokens
                (tenant_id, client_id, token_hash, scopes, expires_at)
            VALUES
                (:tenant_id, :client_id, :token_hash, CAST(:scopes AS jsonb), :expires_at)
            """
        ),
        {
            "tenant_id": tenant_id,
            "client_id": client_id,
            "token_hash": hash_secret(access_token),
            "scopes": json.dumps(scopes),
            "expires_at": expires_at,
        },
    )
    await db.execute(
        text(
            """
            INSERT INTO mcp_request_audit_events
                (tenant_id, client_id, client_key, client_name, operation, required_scope,
                 params_summary, status, app_version)
            VALUES
                (:tenant_id, :client_id, :client_key, :client_name, 'browser_extension.token_issue', NULL,
                 CAST(:params_summary AS jsonb), 'success', :app_version)
            """
        ),
        {
            "tenant_id": tenant_id,
            "client_id": client_id,
            "client_key": client_key,
            "client_name": body.display_name,
            "params_summary": json.dumps(
                {
                    "scopes": scopes,
                    "token_ttl_seconds": body.token_ttl_seconds,
                    "extension_version": body.extension_version,
                }
            ),
            "app_version": body.extension_version,
        },
    )
    await db.commit()
    return BrowserExtensionTokenIssueResponse(
        access_token=access_token,
        expires_in=body.token_ttl_seconds,
        scope=" ".join(scopes),
        tenant_id=tenant_id,
        client_key=client_key,
        expires_at=expires_at,
    )


@router.get("/facts", response_model=list[PalaceTemporalFactSummary], dependencies=[Depends(require_api_capability("read"))])
async def palace_facts(
    request: Request,
    current_only: bool = True,
    limit: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> list[PalaceTemporalFactSummary]:
    rows = await list_temporal_facts(
        db,
        tenant_id=request.state.tenant_id,
        current_only=current_only,
        limit=limit,
    )
    return [PalaceTemporalFactSummary.model_validate(row) for row in rows]


@router.get("/sync-sources", response_model=list[SyncSourceSummary], dependencies=[Depends(require_api_capability("read"))])
async def get_sync_sources(
    request: Request,
    include_disabled: bool = False,
    db: AsyncSession = Depends(get_db),
) -> list[SyncSourceSummary]:
    return await list_sync_sources(db, request.state.tenant_id, include_disabled=include_disabled)


@router.post("/sync-sources", response_model=SyncSourceSummary, status_code=201, dependencies=[Depends(require_api_capability("write"))])
async def post_sync_source(
    body: SyncSourceCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> SyncSourceSummary:
    source = await create_sync_source(db, tenant_id=request.state.tenant_id, body=body)
    return _sync_source_response(source)


@router.patch("/sync-sources/{source_id}", response_model=SyncSourceSummary, dependencies=[Depends(require_api_capability("write"))])
async def patch_sync_source(
    source_id: uuid.UUID,
    body: SyncSourceUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> SyncSourceSummary:
    source = await db.get(SyncSource, source_id)
    if source is None or source.tenant_id != request.state.tenant_id:
        raise HTTPException(status_code=404, detail="Sync source not found")
    updated = await update_sync_source(db, tenant_id=request.state.tenant_id, source=source, body=body)
    return _sync_source_response(updated)


@router.delete("/sync-sources/{source_id}", response_model=SyncSourceDeleteResponse, dependencies=[Depends(require_api_capability("write"))])
async def remove_sync_source(
    source_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> SyncSourceDeleteResponse:
    source = await db.get(SyncSource, source_id)
    if source is None or source.tenant_id != request.state.tenant_id:
        raise HTTPException(status_code=404, detail="Sync source not found")
    items_deactivated = await delete_sync_source(
        db,
        tenant_id=request.state.tenant_id,
        source=source,
        actor_type=getattr(request.state, "auth_mode", "api"),
        actor_id=getattr(request.state, "key_hash", None),
    )
    if items_deactivated:
        palace_run, created = await create_or_get_palace_run(
            db,
            tenant_id=request.state.tenant_id,
            triggered_by="source-delete",
        )
        if created:
            await enqueue_palace_job(request.app.state.arq_pool, "palace_run_build", palace_run_id=str(palace_run.id))
    return SyncSourceDeleteResponse(
        deleted=True,
        items_deactivated=items_deactivated,
        sync_source_id=source.id,
        sync_source_name=source.name,
        status="disabled",
    )


@router.post("/sync-sources/{source_id}/restore", response_model=SyncSourceSummary, dependencies=[Depends(require_api_capability("write"))])
async def post_restore_sync_source(
    source_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> SyncSourceSummary:
    source = await db.get(SyncSource, source_id)
    if source is None or source.tenant_id != request.state.tenant_id:
        raise HTTPException(status_code=404, detail="Sync source not found")
    restored = await restore_sync_source(
        db,
        tenant_id=request.state.tenant_id,
        source=source,
        actor_type=getattr(request.state, "auth_mode", "api"),
        actor_id=getattr(request.state, "key_hash", None),
    )
    return _sync_source_response(restored)


def _sync_source_response(source: SyncSource) -> SyncSourceSummary:
    return SyncSourceSummary(
        id=source.id,
        name=source.name,
        root_path=source.root_path,
        source_kind=source.source_kind,
        credential_type=source.credential_type or "none",
        has_stored_credential=bool(source.credential_ciphertext),
        status="active" if source.status == "active" else "disabled",
        disabled_at=source.disabled_at,
        disabled_reason=source.disabled_reason,
        scan_interval_seconds=source.scan_interval_seconds,
        allowed_extensions=source.allowed_extensions or [],
        bucket=source.bucket,
        prefix=source.prefix,
        endpoint_url=source.endpoint_url,
        region=source.region,
        force_path_style=bool(source.force_path_style),
        last_synced_at=source.last_synced_at,
        last_error=source.last_error,
    )


@router.post("/sync-sources/{source_id}/sync", response_model=SyncRunSummary, status_code=202, dependencies=[Depends(require_api_capability("write"))])
async def start_sync_source(
    source_id: uuid.UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    run_inline: bool = False,
    db: AsyncSession = Depends(get_db),
) -> SyncRunSummary:
    source = await db.get(SyncSource, source_id)
    if source is None or source.tenant_id != request.state.tenant_id:
        raise HTTPException(status_code=404, detail="Sync source not found")
    run, created = await create_or_get_sync_run(
        db,
        tenant_id=request.state.tenant_id,
        source=source,
        triggered_by="manual",
    )
    run_status = getattr(run, "status", "queued")
    if run_inline and run_status == "queued":
        background_tasks.add_task(_run_sync_inline, request.app, run.id)
    elif created:
        await enqueue_palace_job(request.app.state.arq_pool, "run_sync_source", sync_run_id=str(run.id))
    rows = await list_sync_runs(db, request.state.tenant_id, limit=20)
    return next(row for row in rows if row.id == run.id)


@router.get("/sync-runs", response_model=list[SyncRunSummary], dependencies=[Depends(require_api_capability("read"))])
async def get_sync_runs(request: Request, db: AsyncSession = Depends(get_db)) -> list[SyncRunSummary]:
    return await list_sync_runs(db, request.state.tenant_id)


@router.get("/runs", response_model=list[PalaceRunSummary], dependencies=[Depends(require_api_capability("read"))])
async def get_palace_runs(request: Request, db: AsyncSession = Depends(get_db)) -> list[PalaceRunSummary]:
    return await list_palace_runs(db, request.state.tenant_id)


@router.post("/runs", response_model=PalaceRunSummary, status_code=202, dependencies=[Depends(require_api_capability("write"))])
async def start_palace_run(request: Request, db: AsyncSession = Depends(get_db)) -> PalaceRunSummary:
    run, created = await create_or_get_palace_run(
        db,
        tenant_id=request.state.tenant_id,
        triggered_by="manual",
    )
    if created:
        await enqueue_palace_job(request.app.state.arq_pool, "palace_run_build", palace_run_id=str(run.id))
    logger.info(
        "POST /palace/runs tenant=%s run_id=%s created=%s status=%s requested_generation=%s",
        request.state.tenant_id,
        run.id,
        created,
        run.status,
        run.requested_generation,
    )
    rows = await list_palace_runs(db, request.state.tenant_id, limit=20)
    return next(row for row in rows if row.id == run.id)


@router.post("/runs/{run_id}/retry", response_model=PalaceRunSummary, status_code=202, dependencies=[Depends(require_api_capability("write"))])
async def retry_palace_run(
    run_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> PalaceRunSummary:
    existing = await db.get(PalaceRun, run_id)
    if existing is None or existing.tenant_id != request.state.tenant_id:
        raise HTTPException(status_code=404, detail="Palace run not found")
    if existing.status != "failed":
        raise HTTPException(status_code=409, detail="Only failed Palace runs can be retried")

    retry_run = PalaceRun(
        tenant_id=existing.tenant_id,
        status="queued",
        triggered_by="retry",
        requested_generation=existing.requested_generation,
        attempt=existing.attempt + 1,
        source_sync_run_id=existing.source_sync_run_id,
    )
    db.add(retry_run)
    await db.commit()
    await db.refresh(retry_run)
    await enqueue_palace_job(request.app.state.arq_pool, "palace_run_build", palace_run_id=str(retry_run.id))
    logger.info(
        "POST /palace/runs/%s/retry tenant=%s retry_run_id=%s requested_generation=%s attempt=%s",
        run_id,
        request.state.tenant_id,
        retry_run.id,
        retry_run.requested_generation,
        retry_run.attempt,
    )

    rows = await list_palace_runs(db, request.state.tenant_id, limit=20)
    return next(row for row in rows if row.id == retry_run.id)


@router.get("/rooms/{room_id}", response_model=PalaceRoomDetail, dependencies=[Depends(require_api_capability("read"))])
async def get_palace_room(
    room_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> PalaceRoomDetail:
    return await get_room_detail(db, request.state.tenant_id, room_id)


@router.get("/sources/{item_id}", response_model=PalaceItemSourceSummary, dependencies=[Depends(require_api_capability("read"))])
async def get_palace_item_sources(
    item_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> PalaceItemSourceSummary:
    summary = await get_item_source_summary(db, tenant_id=request.state.tenant_id, item_id=item_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return PalaceItemSourceSummary(
        tenant_id=summary.tenant_id,
        item_id=summary.item_id,
        source_records=[
            {
                "id": record.id,
                "item_id": record.item_id,
                "source_kind": record.source_kind,
                "source_uri": record.source_uri,
                "source_version": record.source_version,
                "content_hash": record.content_hash,
                "status": record.status,
                "failure_reason": record.failure_reason,
                "metadata": record.metadata,
                "chunk_count": record.chunk_count,
                "chunks": [
                    {
                        "id": chunk.id,
                        "chunk_index": chunk.chunk_index,
                        "chunk_digest": chunk.chunk_digest,
                        "token_count": chunk.token_count,
                        "preview": chunk.preview,
                    }
                    for chunk in record.chunks
                ],
            }
            for record in summary.source_records
        ],
    )


@router.get("/claims/support", response_model=PalaceClaimSupportReport, dependencies=[Depends(require_api_capability("read"))])
async def get_palace_claim_support(
    request: Request,
    status: str | None = Query(None, pattern="^(draft|active|stale|conflicted|rejected|superseded)$"),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> PalaceClaimSupportReport:
    report = await get_claim_support_report(
        db,
        tenant_id=request.state.tenant_id,
        status=status,
        limit=limit,
    )
    return PalaceClaimSupportReport(
        tenant_id=report.tenant_id,
        claims=[_serialize_claim_support_summary(claim) for claim in report.claims],
    )


@router.get("/answers/audit", response_model=PalaceAnswerAuditReport, dependencies=[Depends(require_api_capability("read"))])
async def get_palace_answer_audit(
    request: Request,
    claim_id: uuid.UUID | None = None,
    status: str | None = Query(None, pattern="^(draft|active|stale|conflicted|rejected|superseded)$"),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> PalaceAnswerAuditReport:
    report = await get_answer_audit_report(
        db,
        tenant_id=request.state.tenant_id,
        claim_id=claim_id,
        status=status,
        limit=limit,
    )
    return PalaceAnswerAuditReport(
        tenant_id=report.tenant_id,
        audit_scope=report.audit_scope,
        items=[_serialize_answer_audit_item(item) for item in report.items],
    )


@router.post("/claims/{claim_id}/review", response_model=PalaceClaimSupportSummary, dependencies=[Depends(require_api_capability("write"))])
async def review_palace_decision_claim(
    claim_id: uuid.UUID,
    body: PalaceClaimReviewRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> PalaceClaimSupportSummary:
    try:
        claim = await review_decision_claim(
            db,
            tenant_id=request.state.tenant_id,
            claim_id=claim_id,
            action=body.action,
            reviewed_by=body.reviewed_by,
            review_role=body.review_role,
            rationale=body.rationale,
        )
    except ClaimReviewError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code, "message": exc.message}) from exc
    if claim is None:
        raise HTTPException(status_code=404, detail="Decision claim not found")
    return PalaceClaimSupportSummary.model_validate(_serialize_claim_support_summary(claim))


@router.patch("/rooms/{room_id}", response_model=PalaceRoomDetail, dependencies=[Depends(require_api_capability("write"))])
async def patch_palace_room(
    room_id: uuid.UUID,
    body: PalaceRoomUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> PalaceRoomDetail:
    return await update_room(db, tenant_id=request.state.tenant_id, room_id=room_id, body=body)


@router.post("/retrieve", response_model=PalaceRetrieveResponse, dependencies=[Depends(require_api_capability("read"))])
async def retrieve_in_palace(
    body: PalaceRetrieveRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> PalaceRetrieveResponse:
    return await retrieve_palace(
        db,
        tenant_id=request.state.tenant_id,
        embedder=request.app.state.embedder,
        body=body,
    )


@router.post("/rooms/{room_id}/pins", status_code=204, dependencies=[Depends(require_api_capability("write"))])
async def pin_item(
    room_id: uuid.UUID,
    body: PalacePinRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    await pin_room_membership(db, tenant_id=request.state.tenant_id, room_id=room_id, body=body)
    run, created = await create_or_get_palace_run(db, tenant_id=request.state.tenant_id, triggered_by="curation")
    if created:
        await enqueue_palace_job(request.app.state.arq_pool, "palace_run_build", palace_run_id=str(run.id))
    return Response(status_code=204)


@router.delete("/rooms/{room_id}/pins/{item_id}", status_code=204, dependencies=[Depends(require_api_capability("write"))])
async def unpin_item(
    room_id: uuid.UUID,
    item_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    await unpin_room_membership(db, tenant_id=request.state.tenant_id, room_id=room_id, item_id=item_id)
    run, created = await create_or_get_palace_run(db, tenant_id=request.state.tenant_id, triggered_by="curation")
    if created:
        await enqueue_palace_job(request.app.state.arq_pool, "palace_run_build", palace_run_id=str(run.id))
    return Response(status_code=204)
