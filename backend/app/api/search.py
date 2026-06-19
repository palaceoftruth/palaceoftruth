from datetime import datetime
from time import perf_counter

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_api_key
from app.database import get_db
from app.schemas.search import SearchRequest, SearchResponse, TagsMode
from app.services.retrieval_capture import build_capture_record, capture_retrieval
from app.services.search import SearchService
from app.services.retrieval_lenses import validate_retrieval_lens_name

router = APIRouter(prefix="/search", tags=["search"], dependencies=[Depends(verify_api_key)])


@router.get("", response_model=SearchResponse)
async def search(
    request: Request,
    q: str = Query(..., description="Natural language search query"),
    limit: int = Query(10, ge=1, le=50),
    candidate_limit: int | None = Query(None, ge=1, le=200),
    include_neighbor_chunks: bool = Query(False),
    neighbor_chunk_window: int = Query(1, ge=1, le=5),
    context_budget_chars: int | None = Query(None, ge=200, le=20000),
    source_type: str | None = Query(None),
    retrieval_lens: str | None = Query(None),
    tags: str | None = Query(None, description="Comma-separated tags"),
    tags_mode: TagsMode = Query("any", description="'any' or 'all' tag match"),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    min_score: float | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    try:
        retrieval_lens = validate_retrieval_lens_name(retrieval_lens)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    started = perf_counter()
    svc = SearchService(db, request.app.state.embedder, request.state.tenant_id)
    results = await svc.vector_search(
        query=q,
        limit=limit,
        candidate_limit=candidate_limit,
        include_neighbor_chunks=include_neighbor_chunks,
        neighbor_chunk_window=neighbor_chunk_window,
        context_budget_chars=context_budget_chars,
        source_type=source_type,
        retrieval_lens=retrieval_lens,
        tags=tag_list,
        tags_mode=tags_mode,
        date_from=date_from,
        date_to=date_to,
        min_score=min_score,
    )
    capture_retrieval(
        build_capture_record(
            endpoint="/api/v1/search",
            tenant_id=request.state.tenant_id,
            query=q,
            request_params={
                "limit": limit,
                "candidate_limit": candidate_limit,
                "include_neighbor_chunks": include_neighbor_chunks,
                "neighbor_chunk_window": neighbor_chunk_window,
                "context_budget_chars": context_budget_chars,
                "source_type": source_type,
                "retrieval_lens": retrieval_lens,
                "tags": tag_list,
                "tags_mode": tags_mode,
                "date_from": date_from,
                "date_to": date_to,
                "min_score": min_score,
            },
            results=results,
            latency_ms=(perf_counter() - started) * 1000,
            trace=svc.last_ranking_trace,
        )
    )
    return SearchResponse(results=results, total=len(results), trace=svc.last_ranking_trace)


@router.post("", response_model=SearchResponse)
async def search_post(
    body: SearchRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    started = perf_counter()
    svc = SearchService(db, request.app.state.embedder, request.state.tenant_id)
    results = await svc.vector_search(
        query=body.query,
        limit=body.limit,
        candidate_limit=body.candidate_limit,
        include_neighbor_chunks=body.include_neighbor_chunks,
        neighbor_chunk_window=body.neighbor_chunk_window,
        context_budget_chars=body.context_budget_chars,
        source_type=body.source_type,
        retrieval_lens=body.retrieval_lens,
        tags=body.tags,
        tags_mode=body.tags_mode,
        date_from=body.date_from,
        date_to=body.date_to,
        min_score=body.min_score,
    )
    capture_retrieval(
        build_capture_record(
            endpoint="/api/v1/search",
            tenant_id=request.state.tenant_id,
            query=body.query,
            request_params={
                "limit": body.limit,
                "candidate_limit": body.candidate_limit,
                "include_neighbor_chunks": body.include_neighbor_chunks,
                "neighbor_chunk_window": body.neighbor_chunk_window,
                "context_budget_chars": body.context_budget_chars,
                "source_type": body.source_type,
                "retrieval_lens": body.retrieval_lens,
                "tags": body.tags,
                "tags_mode": body.tags_mode,
                "date_from": body.date_from,
                "date_to": body.date_to,
                "min_score": body.min_score,
            },
            results=results,
            latency_ms=(perf_counter() - started) * 1000,
            trace=svc.last_ranking_trace,
        )
    )
    return SearchResponse(results=results, total=len(results), trace=svc.last_ranking_trace)
