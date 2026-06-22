import json
import logging
import uuid
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_mcp_scope, verify_memory_auth
from app.config import settings
from app.database import get_db
from app.models.job import Job
from app.schemas.memory import (
    AgentMemoryRetrieveRequest,
    AgentMemoryRetrieveResponse,
    LegacyMemoryArtifactRequest,
    MemoryArtifactAcceptedResponse,
    MemoryEntryBatchRequest,
    MemoryEntryBatchResponse,
    MemoryEntryBatchResult,
    MemoryEntryRequest,
    MemoryEntryListResponse,
    MemoryJobListResponse,
    MemoryQueueHint,
    MemoryJobResponse,
    MemoryWriteContractStatus,
    MemoryRetrievalDoctorAuthShape,
    MemoryRetrievalDoctorRequest,
    MemoryRetrievalDoctorResponse,
    MemoryTrajectoryRequest,
    MemoryTrajectoryResponse,
    McpRequestAuditRequest,
    McpRequestAuditResponse,
    MemoryRetrieveRequest,
    MemoryRetrieveResponse,
    MemoryScope,
    MemoryScopeListResponse,
    MemoryWakeupBriefResponse,
    MemoryWhoAmIResponse,
    RelationshipBackfillAcceptedResponse,
    RelationshipBackfillRequest,
    TagsMode,
)
from app.services.memory import (
    MEMORY_JOB_TYPE,
    accept_canonical_memory_entry,
    accept_memory_artifact,
    build_memory_retrieval_doctor,
    build_memory_acceptance_response,
    get_memory_wakeup_brief,
    list_memory_entries,
    list_memory_jobs,
    list_memory_scopes,
    delegated_agent_memory_policy_from_config,
    retrieve_agent_memory,
    retrieve_memory,
    retry_memory_job,
    serialize_memory_job,
)
from app.services.memory_admission import evaluate_memory_write_admission
from app.services.memory_trajectory import retrieve_memory_trajectory
from app.services.job_progress import record_job_progress_event
from app.services.queue_telemetry import build_memory_queue_hint
from app.services.retrieval_capture import build_capture_record, capture_retrieval, query_fingerprint
from app.workers.queues import enqueue_singleton_job

router = APIRouter(prefix="/memory", tags=["memory"])
logger = logging.getLogger(__name__)


def _clean_query_tags(tags: list[str] | None) -> list[str] | None:
    if tags is None:
        return None
    cleaned = [tag.strip() for tag in tags]
    if any(not tag for tag in cleaned):
        raise HTTPException(status_code=422, detail="tags must not contain blank values")
    return cleaned or None


def _tenant_mismatch_detail() -> dict[str, Any]:
    return {
        "status": "permanent_tenant_mismatch",
        "message": "Request tenant_id does not match the authenticated tenant",
        "retryable": False,
    }


async def _mark_memory_enqueue_unavailable(db: AsyncSession, *, job: Job, error: Exception) -> None:
    payload = dict(job.payload or {})
    payload["contract_status"] = "dependency_unavailable"
    job.payload = payload
    job.status = "failed"
    job.progress = 100
    job.error_message = "Memory queue unavailable; retry the memory job after dependency recovery"
    job.completed_at = datetime.now(timezone.utc)
    await record_job_progress_event(
        db,
        job=job,
        phase="enqueue",
        status="failed",
        progress=100,
        message="Memory queue unavailable; job was accepted but not queued",
        metadata={"error_class": error.__class__.__name__},
    )
    await db.commit()
    await db.refresh(job)


def _memory_job_poll_url(request: Request, job: Job) -> str:
    return str(request.url_for("get_memory_job", job_id=str(job.id)))


def _set_memory_contract_headers(
    response: Response,
    *,
    job_id: uuid.UUID,
    contract_status: str,
    poll_after_seconds: int,
    queue: MemoryQueueHint | None = None,
    retry_after_seconds: int | None = None,
) -> None:
    response.headers["X-Palace-Memory-Job-Id"] = str(job_id)
    response.headers["X-Palace-Memory-Contract-Status"] = contract_status
    response.headers["X-Palace-Memory-Poll-After"] = str(poll_after_seconds)
    response.headers["X-Palace-Rate-Limit-State"] = "not_enforced"
    if queue is not None:
        response.headers["X-Palace-Memory-Queue-State"] = queue.state
        if queue.queued_depth is not None:
            response.headers["X-Palace-Memory-Queue-Depth"] = str(queue.queued_depth)
    if retry_after_seconds is not None:
        response.headers["Retry-After"] = str(retry_after_seconds)


def _set_memory_batch_contract_headers(
    response: Response,
    *,
    contract_status: str,
    poll_after_seconds: int,
    queue: MemoryQueueHint | None = None,
    retry_after_seconds: int | None = None,
) -> None:
    response.headers["X-Palace-Memory-Contract-Status"] = contract_status
    response.headers["X-Palace-Memory-Poll-After"] = str(poll_after_seconds)
    response.headers["X-Palace-Rate-Limit-State"] = "not_enforced"
    if queue is not None:
        response.headers["X-Palace-Memory-Queue-State"] = queue.state
        if queue.queued_depth is not None:
            response.headers["X-Palace-Memory-Queue-Depth"] = str(queue.queued_depth)
    if retry_after_seconds is not None:
        response.headers["Retry-After"] = str(retry_after_seconds)


async def _memory_queue_contract_hint(request: Request) -> MemoryQueueHint:
    return MemoryQueueHint.model_validate(await build_memory_queue_hint(getattr(request.app.state, "arq_pool", None)))


async def _enqueue_memory_job_or_raise(request: Request, db: AsyncSession, *, job: Job) -> None:
    try:
        await request.app.state.arq_pool.enqueue_job("memory_artifact", job_id=str(job.id))
    except Exception as exc:
        logger.warning(
            "memory write enqueue failed tenant=%s job_id=%s error_class=%s",
            request.state.tenant_id,
            job.id,
            exc.__class__.__name__,
        )
        await _mark_memory_enqueue_unavailable(db, job=job, error=exc)
        raise HTTPException(
            status_code=503,
            detail={
                "status": "dependency_unavailable",
                "message": "Memory queue unavailable; retry the accepted job after dependency recovery",
                "retryable": True,
                "job_id": str(job.id),
                "contract_status": "dependency_unavailable",
                "poll_url": _memory_job_poll_url(request, job),
                "poll_after_seconds": 10,
                "retry_after_seconds": 30,
                "rate_limit_state": "not_enforced",
            },
            headers={
                "Retry-After": "30",
                "X-Palace-Memory-Job-Id": str(job.id),
                "X-Palace-Memory-Contract-Status": "dependency_unavailable",
                "X-Palace-Memory-Poll-After": "10",
                "X-Palace-Memory-Queue-State": "unknown",
                "X-Palace-Rate-Limit-State": "not_enforced",
            },
        ) from exc


def _scope_label(scope: Any) -> str:
    scope_type = getattr(scope, "type", None) or (scope.get("type") if isinstance(scope, dict) else None)
    scope_key = getattr(scope, "key", None) or (scope.get("key") if isinstance(scope, dict) else None)
    if scope_type == "tenant_shared":
        return "tenant_shared"
    if scope_type and scope_key:
        return f"{scope_type}/{scope_key}"
    return str(scope_type or "unknown")


def _result_diagnostics(results: list[Any], *, limit: int = 8) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    for rank, result in enumerate(results[:limit], start=1):
        row = {
            "rank": rank,
            "item_id": str(getattr(result, "item_id", "")),
            "source_type": getattr(result, "source_type", None),
            "score": getattr(result, "score", None),
            "trust_class": getattr(result, "trust_class", None),
            "source_support_state": getattr(result, "source_support_state", None),
            "freshness": getattr(result, "freshness", None),
            "derived_raw_classification": getattr(result, "derived_raw_classification", None),
        }
        retrieved_scope = getattr(result, "retrieved_scope_label", None) or getattr(result, "scope_label", None)
        if retrieved_scope:
            row["scope"] = retrieved_scope
        diagnostics.append({key: value for key, value in row.items() if value not in (None, "")})
    return diagnostics


def _trace_count_field(trace: Any, key: str) -> dict[str, int]:
    search_trace = getattr(trace, "search_ranking_trace", None)
    if isinstance(search_trace, dict) and isinstance(search_trace.get(key), dict):
        return {
            str(name): count
            for name, count in search_trace[key].items()
            if isinstance(name, str) and isinstance(count, int) and not isinstance(count, bool) and count > 0
        }
    ranking_traces = getattr(trace, "ranking_traces", []) or []
    for ranking_trace in ranking_traces:
        value = getattr(ranking_trace, key, None)
        if isinstance(value, dict):
            return {
                str(name): count
                for name, count in value.items()
                if isinstance(name, str) and isinstance(count, int) and not isinstance(count, bool) and count > 0
            }
    return {}


def _trace_reuse_metrics(trace: Any) -> dict[str, Any]:
    search_trace = getattr(trace, "search_ranking_trace", None)
    if isinstance(search_trace, dict) and isinstance(search_trace.get("reuse_metrics"), dict):
        return {
            str(name): value
            for name, value in search_trace["reuse_metrics"].items()
            if isinstance(name, str) and isinstance(value, (str, int, float, bool))
        }
    ranking_traces = getattr(trace, "ranking_traces", []) or []
    for ranking_trace in ranking_traces:
        value = getattr(ranking_trace, "reuse_metrics", None)
        if isinstance(value, dict):
            return {
                str(name): field_value
                for name, field_value in value.items()
                if isinstance(name, str) and isinstance(field_value, (str, int, float, bool))
            }
    return {}


def _trace_diagnostics(trace: Any) -> dict[str, Any]:
    ranking_traces = getattr(trace, "ranking_traces", []) or []
    diagnostics = {
        "fallback_used": getattr(trace, "fallback_used", None),
        "route_confidence": getattr(trace, "route_confidence", None),
        "route_score": getattr(trace, "route_score", None),
        "route_candidate_count": getattr(trace, "route_candidate_count", None),
        "route_room_candidate_count": getattr(trace, "route_room_candidate_count", None),
        "route_global_candidate_count": getattr(trace, "route_global_candidate_count", None),
        "routed_room_id": None,
        "selected_wing": getattr(trace, "selected_wing", None),
        "candidate_rooms": getattr(trace, "candidate_rooms", []) or [],
        "expanded_rooms": getattr(trace, "expanded_rooms", []) or [],
        "global_merge_rescued_results": getattr(trace, "global_merge_rescued_results", None),
        "merge_routes": [getattr(entry, "route", None) for entry in ranking_traces if getattr(entry, "route", None)],
        "trust_class_counts": _trace_count_field(trace, "trust_class_counts"),
        "source_support_counts": _trace_count_field(trace, "source_support_counts"),
        "freshness_counts": _trace_count_field(trace, "freshness_counts"),
        "derived_raw_counts": _trace_count_field(trace, "derived_raw_counts"),
        "reuse_metrics": _trace_reuse_metrics(trace),
        "budget_truncated": getattr(trace, "context_budget_truncated", None),
        "completeness_warning": getattr(trace, "completeness_warning", None),
    }
    searched_scopes = getattr(trace, "searched_scopes", None)
    if searched_scopes is not None:
        diagnostics.update(
            {
                "searched_scopes": [_scope_label(scope) for scope in searched_scopes],
                "searched_scope_count": len(searched_scopes),
                "caller_agent_scope_key": getattr(trace, "caller_agent_scope_key", None),
                "requested_agent_scope_keys": getattr(trace, "requested_agent_scope_keys", []) or [],
                "authorized_agent_scope_keys": getattr(trace, "authorized_agent_scope_keys", []) or [],
                "denied_agent_scope_keys": getattr(trace, "denied_agent_scope_keys", []) or [],
                "delegated_agent_policy_id": getattr(trace, "delegated_agent_policy_id", None),
                "delegated_agent_policy_source": getattr(trace, "delegated_agent_policy_source", None),
                "delegated_agent_decision": getattr(trace, "delegated_agent_decision", None),
                "delegated_agent_deny_reasons": getattr(trace, "delegated_agent_deny_reasons", []) or [],
                "access_reason_required": getattr(trace, "access_reason_required", None),
                "access_reason_present": getattr(trace, "access_reason_present", None),
                "result_counts_by_scope": getattr(trace, "result_counts_by_scope", {}) or {},
                "workspace_strict": getattr(trace, "workspace_strict", None),
                "workspace_scope_exhausted": getattr(trace, "workspace_scope_exhausted", None),
                "tenant_shared_policy": getattr(trace, "tenant_shared_policy", None),
                "tenant_shared_fallback_used": getattr(trace, "tenant_shared_fallback_used", None),
                "broad_corpus_policy": getattr(trace, "broad_corpus_policy", None),
                "selected_scope_query_count": getattr(trace, "selected_scope_query_count", None),
                "selected_scope_result_count": getattr(trace, "selected_scope_result_count", None),
                "broad_corpus_searched": getattr(trace, "broad_corpus_searched", None),
                "broad_corpus_skipped_reason": getattr(trace, "broad_corpus_skipped_reason", None),
                "broad_result_count": getattr(trace, "broad_result_count", None),
                "deduped_result_count": getattr(trace, "deduped_result_count", None),
                "selected_scope_duration_ms": getattr(trace, "selected_scope_duration_ms", None),
                "broad_corpus_duration_ms": getattr(trace, "broad_corpus_duration_ms", None),
                "merge_duration_ms": getattr(trace, "merge_duration_ms", None),
                "total_duration_ms": getattr(trace, "total_duration_ms", None),
                "budget_truncated": getattr(trace, "budget_truncated", None),
                "context_budget_truncated": getattr(trace, "context_budget_truncated", None),
                "completeness_warnings": getattr(trace, "completeness_warnings", []) or [],
            }
        )
    return {key: value for key, value in diagnostics.items() if value not in (None, [], {})}


def _log_retrieval_diagnostics(
    *,
    endpoint: str,
    tenant_id: str,
    query: str,
    latency_ms: float,
    status: str,
    request_summary: dict[str, Any],
    trace: Any | None = None,
    results: list[Any] | None = None,
    error_class: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "endpoint": endpoint,
        "tenant_id": tenant_id,
        "query_fingerprint": query_fingerprint(query),
        "latency_ms": round(latency_ms, 3),
        "status": status,
        "request": request_summary,
        "error_class": error_class,
    }
    if trace is not None:
        payload["trace"] = _trace_diagnostics(trace)
    if results is not None:
        payload["result_count"] = len(results)
        payload["results"] = _result_diagnostics(results)
    logger.info("memory retrieval diagnostics %s", json.dumps(payload, sort_keys=True, default=str))


@router.get("/whoami", response_model=MemoryWhoAmIResponse, dependencies=[Depends(require_mcp_scope("read"))])
async def whoami(request: Request) -> MemoryWhoAmIResponse:
    return MemoryWhoAmIResponse(tenant_id=request.state.tenant_id)


@router.post(
    "/mcp/audit",
    response_model=McpRequestAuditResponse,
    status_code=201,
    dependencies=[Depends(verify_memory_auth)],
)
async def record_mcp_request_audit(
    body: McpRequestAuditRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> McpRequestAuditResponse:
    tenant_id = request.state.tenant_id
    if (
        getattr(request.state, "auth_mode", None) == "mcp_oauth"
        and body.client.client_key != getattr(request.state, "mcp_client_key", None)
    ):
        raise HTTPException(status_code=403, detail="MCP audit client does not match bearer token")
    client_metadata = {
        **body.client.metadata,
        "allowed_scopes": body.client.allowed_scopes,
    }
    client_result = await db.execute(
        text(
            """
        INSERT INTO mcp_clients (tenant_id, client_key, display_name, allowed_scopes, metadata, last_seen_at)
        VALUES (:tenant_id, :client_key, :display_name, CAST(:allowed_scopes AS jsonb), CAST(:metadata AS jsonb), CURRENT_TIMESTAMP)
        ON CONFLICT (tenant_id, client_key) DO UPDATE
        SET display_name = EXCLUDED.display_name,
            allowed_scopes = EXCLUDED.allowed_scopes,
            metadata = EXCLUDED.metadata,
            last_seen_at = CURRENT_TIMESTAMP
        RETURNING id
        """
        ),
        {
            "tenant_id": tenant_id,
            "client_key": body.client.client_key,
            "display_name": body.client.display_name,
            "allowed_scopes": json.dumps(body.client.allowed_scopes),
            "metadata": json.dumps(client_metadata),
        },
    )
    client_id = client_result.mappings().one()["id"]
    audit_result = await db.execute(
        text(
            """
        INSERT INTO mcp_request_audit_events
        (tenant_id, client_id, client_key, client_name, operation, required_scope, params_summary,
         status, latency_ms, error_class, app_version)
        VALUES
        (:tenant_id, :client_id, :client_key, :client_name, :operation, :required_scope,
         CAST(:params_summary AS jsonb), :status, :latency_ms, :error_class, :app_version)
        RETURNING id
        """
        ),
        {
            "tenant_id": tenant_id,
            "client_id": client_id,
            "client_key": body.client.client_key,
            "client_name": body.client.display_name,
            "operation": body.operation,
            "required_scope": body.required_scope,
            "params_summary": json.dumps(body.params_summary),
            "status": body.status,
            "latency_ms": body.latency_ms,
            "error_class": body.error_class,
            "app_version": body.app_version,
        },
    )
    await db.commit()
    return McpRequestAuditResponse(
        audit_event_id=audit_result.mappings().one()["id"],
        client_id=client_id,
        tenant_id=tenant_id,
    )


@router.post(
    "/entries",
    response_model=MemoryArtifactAcceptedResponse,
    status_code=202,
    dependencies=[Depends(require_mcp_scope("write"))],
)
async def create_memory_entry(
    body: MemoryEntryRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> MemoryArtifactAcceptedResponse:
    if body.tenant_id != request.state.tenant_id:
        raise HTTPException(status_code=403, detail=_tenant_mismatch_detail())

    admission = evaluate_memory_write_admission(
        body=body,
        auth_mode=getattr(request.state, "auth_mode", None),
        allowed_scopes=list(getattr(request.state, "mcp_allowed_scopes", None) or []),
        mcp_client_key=getattr(request.state, "mcp_client_key", None),
    )
    if not admission.accepted:
        raise HTTPException(status_code=admission.http_status_code, detail=admission.response_detail())

    result = await accept_canonical_memory_entry(
        db,
        body=body,
        signing_key=request.state.key_hash,
        admission_audit=admission.audit,
    )
    queue = await _memory_queue_contract_hint(request)
    if result.enqueue_requested:
        await _enqueue_memory_job_or_raise(request, db, job=result.job)
    accepted = build_memory_acceptance_response(result, poll_url=_memory_job_poll_url(request, result.job), queue=queue)
    _set_memory_contract_headers(
        response,
        job_id=result.job.id,
        contract_status=accepted.contract_status,
        poll_after_seconds=accepted.poll_after_seconds,
        queue=queue,
        retry_after_seconds=accepted.retry_after_seconds,
    )
    return accepted


@router.post(
    "/entries:batch",
    response_model=MemoryEntryBatchResponse,
    status_code=202,
    dependencies=[Depends(require_mcp_scope("write"))],
)
async def create_memory_entries_batch(
    body: MemoryEntryBatchRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> MemoryEntryBatchResponse:
    queue = await _memory_queue_contract_hint(request)
    results: list[MemoryEntryBatchResult] = []
    accepted_count = 0
    failed_count = 0

    for index, entry in enumerate(body.entries):
        if entry.tenant_id != request.state.tenant_id:
            failed_count += 1
            results.append(
                MemoryEntryBatchResult(
                    index=index,
                    status="failed",
                    contract_status="permanent_tenant_mismatch",
                    retryable=False,
                    error=_tenant_mismatch_detail(),
                )
            )
            continue

        admission = evaluate_memory_write_admission(
            body=entry,
            auth_mode=getattr(request.state, "auth_mode", None),
            allowed_scopes=list(getattr(request.state, "mcp_allowed_scopes", None) or []),
            mcp_client_key=getattr(request.state, "mcp_client_key", None),
        )
        if not admission.accepted:
            failed_count += 1
            detail = admission.response_detail()
            results.append(
                MemoryEntryBatchResult(
                    index=index,
                    status="failed",
                    contract_status=cast(MemoryWriteContractStatus, detail.get("status", "rejected")),
                    retryable=bool(detail.get("retryable", False)),
                    error=detail,
                )
            )
            continue

        result = await accept_canonical_memory_entry(
            db,
            body=entry,
            signing_key=request.state.key_hash,
            admission_audit=admission.audit,
        )
        try:
            if result.enqueue_requested:
                await _enqueue_memory_job_or_raise(request, db, job=result.job)
        except HTTPException as exc:
            failed_count += 1
            detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
            results.append(
                MemoryEntryBatchResult(
                    index=index,
                    status="failed",
                    contract_status=cast(MemoryWriteContractStatus, detail.get("contract_status", "dependency_unavailable")),
                    retryable=bool(detail.get("retryable", True)),
                    job_id=result.job.id,
                    poll_url=detail.get("poll_url") if isinstance(detail.get("poll_url"), str) else _memory_job_poll_url(request, result.job),
                    poll_after_seconds=detail.get("poll_after_seconds") if isinstance(detail.get("poll_after_seconds"), int) else 10,
                    retry_after_seconds=detail.get("retry_after_seconds") if isinstance(detail.get("retry_after_seconds"), int) else 30,
                    accepted_as=result.accepted_as,
                    scope={"type": result.scope_type, "key": result.scope_key},
                    error=detail,
                )
            )
            continue

        accepted = build_memory_acceptance_response(result, poll_url=_memory_job_poll_url(request, result.job), queue=queue)
        item_contract_status = accepted.contract_status
        if accepted.status in {"complete", "duplicate"}:
            item_contract_status = "completed"
        accepted_count += 1
        results.append(
            MemoryEntryBatchResult(
                index=index,
                status=accepted.status,
                contract_status=item_contract_status,
                retryable=accepted.retryable,
                job_id=accepted.job_id,
                poll_url=accepted.poll_url,
                poll_after_seconds=accepted.poll_after_seconds,
                retry_after_seconds=accepted.retry_after_seconds,
                accepted_as=accepted.accepted_as,
                scope=accepted.scope,
            )
        )

    retry_after_seconds = max((result.retry_after_seconds or 0 for result in results), default=0) or None
    response_status = "accepted" if failed_count == 0 else "failed" if accepted_count == 0 else "partial"
    contract_status = "accepted"
    if any(result.contract_status in {"retryable_degraded", "dependency_unavailable"} for result in results):
        contract_status = "retryable_degraded"
    elif failed_count:
        contract_status = "rejected"
    poll_after_seconds = queue.poll_after_seconds if queue else 5
    _set_memory_batch_contract_headers(
        response,
        contract_status=contract_status,
        poll_after_seconds=poll_after_seconds,
        queue=queue,
        retry_after_seconds=retry_after_seconds,
    )
    return MemoryEntryBatchResponse(
        status=cast(Any, response_status),
        accepted=accepted_count,
        failed=failed_count,
        poll_after_seconds=poll_after_seconds,
        retryable=any(result.retryable for result in results),
        retry_after_seconds=retry_after_seconds,
        queue=queue,
        results=results,
    )


@router.get("/entries", response_model=MemoryEntryListResponse, dependencies=[Depends(require_mcp_scope("read"))])
async def get_memory_entries(
    request: Request,
    scope_type: str = Query("tenant_shared"),
    scope_key: str | None = Query(None),
    tags: list[str] | None = Query(None),
    tags_mode: str = Query("any", pattern="^(any|all)$"),
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> MemoryEntryListResponse:
    try:
        scope = MemoryScope.model_validate({"type": scope_type, "key": scope_key})
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    created_before = None
    if cursor:
        try:
            created_before = datetime.fromisoformat(cursor.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="cursor must be an ISO 8601 timestamp") from exc
    return await list_memory_entries(
        db,
        tenant_id=request.state.tenant_id,
        scope=scope,
        tags=_clean_query_tags(tags),
        tags_mode=cast(TagsMode, tags_mode),
        limit=limit,
        cursor=created_before,
    )


@router.get("/scopes", response_model=MemoryScopeListResponse, dependencies=[Depends(require_mcp_scope("read"))])
async def get_memory_scopes(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    sample_limit: int = Query(8, ge=0, le=25),
    db: AsyncSession = Depends(get_db),
) -> MemoryScopeListResponse:
    return await list_memory_scopes(
        db,
        tenant_id=request.state.tenant_id,
        limit=limit,
        sample_limit=sample_limit,
    )


@router.post(
    "/artifacts",
    response_model=MemoryArtifactAcceptedResponse,
    status_code=202,
    dependencies=[Depends(require_mcp_scope("write"))],
)
async def create_memory_artifact(
    body: LegacyMemoryArtifactRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> MemoryArtifactAcceptedResponse:
    if body.tenant_id != request.state.tenant_id:
        raise HTTPException(status_code=403, detail=_tenant_mismatch_detail())

    result = await accept_memory_artifact(
        db,
        body=body,
        signing_key=request.state.key_hash,
    )
    queue = await _memory_queue_contract_hint(request)
    if result.enqueue_requested:
        await _enqueue_memory_job_or_raise(request, db, job=result.job)
    accepted = build_memory_acceptance_response(result, poll_url=_memory_job_poll_url(request, result.job), queue=queue)
    _set_memory_contract_headers(
        response,
        job_id=result.job.id,
        contract_status=accepted.contract_status,
        poll_after_seconds=accepted.poll_after_seconds,
        queue=queue,
        retry_after_seconds=accepted.retry_after_seconds,
    )
    return accepted


@router.post(
    "/relationships/backfill",
    response_model=RelationshipBackfillAcceptedResponse,
    status_code=202,
    dependencies=[Depends(require_mcp_scope("write"))],
)
async def enqueue_relationship_backfill(
    body: RelationshipBackfillRequest,
    request: Request,
) -> RelationshipBackfillAcceptedResponse:
    job, lease_key = await enqueue_singleton_job(
        request.app.state.arq_pool,
        "backfill_deferred_relationships",
        request.state.tenant_id,
        tenant_id=request.state.tenant_id,
        limit=body.limit,
        defer_seconds=body.defer_seconds,
    )
    return RelationshipBackfillAcceptedResponse(
        status="queued" if job is not None else "active",
        tenant_id=request.state.tenant_id,
        limit=body.limit,
        defer_seconds=body.defer_seconds,
        lease_key=lease_key,
        lease_holder=lease_key,
    )


@router.get("/jobs/{job_id}", response_model=MemoryJobResponse, dependencies=[Depends(require_mcp_scope("read"))])
async def get_memory_job(
    job_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> MemoryJobResponse:
    job = await db.get(Job, job_id)
    if not job or job.tenant_id != request.state.tenant_id or job.job_type != MEMORY_JOB_TYPE:
        raise HTTPException(status_code=404, detail="Memory job not found")
    return serialize_memory_job(job)


@router.get("/jobs", response_model=MemoryJobListResponse, dependencies=[Depends(require_mcp_scope("read"))])
async def get_memory_jobs(
    request: Request,
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> MemoryJobListResponse:
    return await list_memory_jobs(
        db,
        tenant_id=request.state.tenant_id,
        status=status,
        page=page,
        per_page=per_page,
    )


@router.post(
    "/jobs/{job_id}/retry",
    response_model=MemoryJobResponse,
    dependencies=[Depends(require_mcp_scope("admin"))],
)
async def retry_memory_artifact_job(
    job_id: uuid.UUID,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> MemoryJobResponse:
    job = await retry_memory_job(
        db,
        tenant_id=request.state.tenant_id,
        job_id=job_id,
    )
    await _enqueue_memory_job_or_raise(request, db, job=job)
    serialized = serialize_memory_job(job)
    _set_memory_contract_headers(
        response,
        job_id=job.id,
        contract_status=serialized.contract_status,
        poll_after_seconds=serialized.poll_after_seconds,
        retry_after_seconds=serialized.retry_after_seconds,
    )
    return serialized


@router.get(
    "/wakeup-brief",
    response_model=MemoryWakeupBriefResponse,
    dependencies=[Depends(require_mcp_scope("read"))],
)
async def get_latest_wakeup_brief(
    request: Request,
    scope_type: str = Query("tenant"),
    scope_key: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> MemoryWakeupBriefResponse:
    return await get_memory_wakeup_brief(
        db,
        tenant_id=request.state.tenant_id,
        scope_type=scope_type,
        scope_key=scope_key,
    )


@router.post("/retrieve", response_model=MemoryRetrieveResponse, dependencies=[Depends(require_mcp_scope("read"))])
async def retrieve_memory_artifacts(
    body: MemoryRetrieveRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> MemoryRetrieveResponse:
    started = perf_counter()
    request_params = {
        "limit": body.limit,
        "candidate_limit": body.candidate_limit,
        "include_neighbor_chunks": body.include_neighbor_chunks,
        "neighbor_chunk_window": body.neighbor_chunk_window,
        "context_budget_chars": body.context_budget_chars,
        "tags": body.tags,
        "tags_mode": body.tags_mode,
        "min_score": body.min_score,
        "date_from": body.date_from,
        "date_to": body.date_to,
        "scope": body.scope,
        "room_id": body.room_id,
    }
    try:
        response = await retrieve_memory(
            db,
            embedder=request.app.state.embedder,
            tenant_id=request.state.tenant_id,
            body=body,
        )
    except Exception as exc:
        latency_ms = (perf_counter() - started) * 1000
        _log_retrieval_diagnostics(
            endpoint="/api/v1/memory/retrieve",
            tenant_id=request.state.tenant_id,
            query=body.query,
            latency_ms=latency_ms,
            status="error",
            request_summary={"scope": _scope_label(body.scope), "limit": body.limit},
            error_class=exc.__class__.__name__,
        )
        capture_retrieval(
            build_capture_record(
                endpoint="/api/v1/memory/retrieve",
                tenant_id=request.state.tenant_id,
                query=body.query,
                request_params=request_params,
                results=[],
                trace=None,
                latency_ms=latency_ms,
                status="error",
                error_class=exc.__class__.__name__,
            )
        )
        raise
    latency_ms = (perf_counter() - started) * 1000
    _log_retrieval_diagnostics(
        endpoint="/api/v1/memory/retrieve",
        tenant_id=request.state.tenant_id,
        query=body.query,
        latency_ms=latency_ms,
        status="ok",
        request_summary={"scope": _scope_label(body.scope), "limit": body.limit},
        trace=response.trace,
        results=response.results,
    )
    capture_retrieval(
        build_capture_record(
            endpoint="/api/v1/memory/retrieve",
            tenant_id=request.state.tenant_id,
            query=body.query,
            request_params=request_params,
            results=response.results,
            trace=response.trace,
            latency_ms=latency_ms,
        )
    )
    return response


@router.post(
    "/retrieve-agent",
    response_model=AgentMemoryRetrieveResponse,
    dependencies=[Depends(require_mcp_scope("read"))],
)
async def retrieve_agent_memory_artifacts(
    body: AgentMemoryRetrieveRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> AgentMemoryRetrieveResponse:
    started = perf_counter()
    request_params = {
        "agent_scope_key": body.agent_scope_key,
        "include_agent_scope_keys": body.include_agent_scope_keys,
        "include_all_permitted_agent_scopes": body.include_all_permitted_agent_scopes,
        "access_reason_present": body.access_reason is not None,
        "workspace_scope_keys": body.workspace_scope_keys,
        "session_scope_key": body.session_scope_key,
        "include_tenant_shared": body.include_tenant_shared,
        "include_broad_corpus": body.include_broad_corpus,
        "limit": body.limit,
        "candidate_limit": body.candidate_limit,
        "broad_candidate_limit": body.broad_candidate_limit,
        "display_limit": body.display_limit,
        "context_budget_chars": body.context_budget_chars,
        "include_derived_artifacts": body.include_derived_artifacts,
        "tags": body.tags,
        "tags_mode": body.tags_mode,
        "min_score": body.min_score,
        "date_from": body.date_from,
        "date_to": body.date_to,
    }
    request_summary = {
        "agent_scope_key": body.agent_scope_key,
        "include_agent_scope_count": len(body.include_agent_scope_keys),
        "include_all_permitted_agent_scopes": body.include_all_permitted_agent_scopes,
        "access_reason_present": body.access_reason is not None,
        "workspace_scope_count": len(body.workspace_scope_keys),
        "session_scope_present": body.session_scope_key is not None,
        "include_tenant_shared": body.include_tenant_shared,
        "include_broad_corpus": body.include_broad_corpus,
        "limit": body.limit,
        "candidate_limit": body.candidate_limit,
        "broad_candidate_limit": body.broad_candidate_limit,
        "display_limit": body.display_limit,
        "context_budget_chars": body.context_budget_chars,
    }
    try:
        try:
            delegated_policy = delegated_agent_memory_policy_from_config(
                tenant_id=request.state.tenant_id,
                agent_scope_key=body.agent_scope_key,
                raw_policies=settings.palaceoftruth_delegated_agent_memory_read_policies,
            )
        except ValueError as config_error:
            logger.error("invalid delegated agent memory policy config: %s", config_error)
            raise HTTPException(
                status_code=500,
                detail="Delegated agent memory policy configuration is invalid",
            ) from config_error
        response = await retrieve_agent_memory(
            db,
            embedder=request.app.state.embedder,
            tenant_id=request.state.tenant_id,
            body=body,
            delegated_policy=delegated_policy,
        )
    except Exception as exc:
        latency_ms = (perf_counter() - started) * 1000
        _log_retrieval_diagnostics(
            endpoint="/api/v1/memory/retrieve-agent",
            tenant_id=request.state.tenant_id,
            query=body.query,
            latency_ms=latency_ms,
            status="error",
            request_summary=request_summary,
            error_class=exc.__class__.__name__,
        )
        capture_retrieval(
            build_capture_record(
                endpoint="/api/v1/memory/retrieve-agent",
                tenant_id=request.state.tenant_id,
                query=body.query,
                request_params=request_params,
                results=[],
                trace=None,
                latency_ms=latency_ms,
                status="error",
                error_class=exc.__class__.__name__,
            )
        )
        raise

    latency_ms = (perf_counter() - started) * 1000
    _log_retrieval_diagnostics(
        endpoint="/api/v1/memory/retrieve-agent",
        tenant_id=request.state.tenant_id,
        query=body.query,
        latency_ms=latency_ms,
        status="ok",
        request_summary=request_summary,
        trace=response.trace,
        results=response.results,
    )
    capture_retrieval(
        build_capture_record(
            endpoint="/api/v1/memory/retrieve-agent",
            tenant_id=request.state.tenant_id,
            query=body.query,
            request_params=request_params,
            results=response.results,
            trace=response.trace,
            latency_ms=latency_ms,
        )
    )
    return response


@router.post(
    "/trajectory",
    response_model=MemoryTrajectoryResponse,
    dependencies=[Depends(require_mcp_scope("read"))],
)
async def retrieve_memory_trajectory_artifacts(
    body: MemoryTrajectoryRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> MemoryTrajectoryResponse:
    try:
        delegated_policy = delegated_agent_memory_policy_from_config(
            tenant_id=request.state.tenant_id,
            agent_scope_key=body.agent_scope_key,
            raw_policies=settings.palaceoftruth_delegated_agent_memory_read_policies,
        )
    except ValueError as config_error:
        logger.error("invalid delegated agent memory policy config: %s", config_error)
        raise HTTPException(
            status_code=500,
            detail="Delegated agent memory policy configuration is invalid",
        ) from config_error
    return await retrieve_memory_trajectory(
        db,
        embedder=request.app.state.embedder,
        tenant_id=request.state.tenant_id,
        body=body,
        delegated_policy=delegated_policy,
    )


@router.post(
    "/retrieval-doctor",
    response_model=MemoryRetrievalDoctorResponse,
    dependencies=[Depends(require_mcp_scope("read"))],
)
async def get_memory_retrieval_doctor(
    body: MemoryRetrievalDoctorRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> MemoryRetrievalDoctorResponse:
    return await build_memory_retrieval_doctor(
        db,
        embedder=request.app.state.embedder,
        tenant_id=request.state.tenant_id,
        body=body,
        auth=MemoryRetrievalDoctorAuthShape(
            auth_mode=getattr(request.state, "auth_mode", None),
            mcp_client_key=getattr(request.state, "mcp_client_key", None),
            allowed_scopes=list(getattr(request.state, "mcp_allowed_scopes", None) or []),
        ),
        arq_pool=getattr(request.app.state, "arq_pool", None),
    )
