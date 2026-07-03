import asyncio
import logging
from contextlib import asynccontextmanager

from arq import create_pool
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from app.api import (
    admin,
    capture,
    chat,
    conversations,
    curation_artifacts,
    export,
    feeds,
    graph,
    ingest,
    items,
    jobs,
    mcp_oauth,
    memory,
    palace,
    search,
    source_subscriptions,
    system,
    tags,
    web_saves,
)
from app.config import settings, make_redis_settings
from app.database import async_session
from app.models.palace import SyncSource
from app.schemas.palace import SyncSourceCreate
from app.services.embedder import EmbeddingService
from app.services.llm import LLMService
from app.services.palace import create_sync_source
from app.services.prometheus_metrics import HttpMetricsRecorder, monotonic_seconds

logger = logging.getLogger(__name__)
_HTTP_METRICS = HttpMetricsRecorder()


async def _seed_default_api_key() -> None:
    """Seed the static API_KEY from settings as the 'default' tenant key.

    Idempotent — safe to run on every startup.
    """
    import hashlib
    from sqlalchemy import text as sa_text

    if not settings.api_key:
        return

    key_hash = hashlib.sha256(settings.api_key.encode()).hexdigest()

    async with async_session() as db:
        existing = await db.scalar(
            sa_text("SELECT 1 FROM api_keys WHERE key_hash = :hash LIMIT 1"),
            {"hash": key_hash},
        )
        if existing is None:
            await db.execute(
                sa_text(
                    "INSERT INTO api_keys (tenant_id, key_hash, description) "
                    "VALUES ('default', :hash, 'seeded from API_KEY env var')"
                ),
                {"hash": key_hash},
            )
            await db.commit()
    logger.info("Default API key seeded")


def _parse_default_s3_extensions() -> list[str]:
    return [
        part.strip()
        for part in settings.palace_default_s3_allowed_extensions.split(",")
        if part.strip()
    ]


async def _seed_default_palace_sync_source() -> None:
    """Seed one default S3 Palace sync source for the default tenant.

    Idempotent — safe to run on every startup.
    """
    if not settings.palace_default_s3_source_name or not settings.palace_default_s3_bucket:
        return

    body = SyncSourceCreate(
        name=settings.palace_default_s3_source_name,
        source_kind="s3",
        bucket=settings.palace_default_s3_bucket,
        prefix=settings.palace_default_s3_prefix or None,
        endpoint_url=settings.palace_default_s3_endpoint_url or None,
        region=settings.palace_default_s3_region or None,
        allowed_extensions=_parse_default_s3_extensions(),
        scan_interval_seconds=settings.palace_default_s3_scan_interval_seconds,
        force_path_style=settings.palace_default_s3_force_path_style,
    )

    async with async_session() as db:
        existing = await db.scalar(
            select(SyncSource.id)
            .where(SyncSource.tenant_id == "default")
            .where(SyncSource.source_kind == "s3")
            .where(SyncSource.bucket == body.bucket)
            .where(SyncSource.prefix == body.prefix)
            .limit(1)
        )
        if existing is not None:
            logger.info("Default Palace S3 sync source already present")
            return

        await create_sync_source(db, tenant_id="default", body=body)
    logger.info("Default Palace S3 sync source seeded")


def run_migrations() -> None:
    from alembic.config import Config
    from alembic import command

    alembic_cfg = Config("alembic.ini")
    command.upgrade(alembic_cfg, "head")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Run DB migrations on startup
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, run_migrations)
    logger.info("Database migrations complete")

    await _seed_default_api_key()
    await _seed_default_palace_sync_source()

    # ARQ Redis pool for enqueueing tasks
    app.state.arq_pool = await create_pool(make_redis_settings())
    logger.info("ARQ pool ready")

    # Shared AI services for search/chat API path
    app.state.embedder = EmbeddingService()
    app.state.llm = LLMService()
    logger.info("Embedder and LLM ready")

    yield

    await app.state.arq_pool.close()


app = FastAPI(
    title="Palace of Truth",
    version="0.1.0",
    lifespan=lifespan,
    openapi_url="/api/openapi.json",  # reachable through nginx /api/ proxy
)

def _cors_allowed_origins() -> list[str]:
    origins = [origin.strip() for origin in settings.cors_allowed_origins.split(",") if origin.strip()]
    if not origins or "*" in origins:
        raise RuntimeError("CORS_ALLOWED_ORIGINS must be an explicit comma-separated origin allowlist")
    return origins


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allowed_origins(),
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-MCP-Scope", "X-MCP-Scopes"],
)


@app.middleware("http")
async def record_http_metrics(request, call_next):
    start = monotonic_seconds()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        route = getattr(request.scope.get("route"), "path", None) or "unmatched"
        if route != "/api/v1/metrics":
            _HTTP_METRICS.record(
                method=request.method,
                route=route,
                status_code=status_code,
                duration_seconds=monotonic_seconds() - start,
            )


app.state.prometheus_http_metrics = _HTTP_METRICS

app.include_router(system.router, prefix="/api/v1")
app.include_router(ingest.router, prefix="/api/v1")
app.include_router(capture.router, prefix="/api/v1")
app.include_router(web_saves.router, prefix="/api/v1")
app.include_router(search.router, prefix="/api/v1")
app.include_router(chat.router, prefix="/api/v1")
app.include_router(memory.router, prefix="/api/v1")
app.include_router(mcp_oauth.router, prefix="/api/v1")
app.include_router(mcp_oauth.metadata_router)
app.include_router(items.router, prefix="/api/v1")
app.include_router(jobs.router, prefix="/api/v1")
app.include_router(graph.router, prefix="/api/v1")
app.include_router(feeds.router, prefix="/api/v1")
app.include_router(source_subscriptions.router, prefix="/api/v1")
app.include_router(curation_artifacts.router, prefix="/api/v1")
app.include_router(conversations.router, prefix="/api/v1")
app.include_router(tags.router, prefix="/api/v1")
app.include_router(export.router, prefix="/api/v1")
app.include_router(admin.router, prefix="/api/v1")
app.include_router(palace.router, prefix="/api/v1")
