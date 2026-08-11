"""Redis-backed request throttling for security-sensitive API surfaces."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Limit:
    name: str
    requests: int
    window_seconds: int


AUTH_LIMIT = Limit("auth", 10, 300)
TOKEN_LIMIT = Limit("oauth-token", 20, 60)
READ_LIMIT = Limit("read", 120, 60)
WRITE_LIMIT = Limit("write", 60, 60)


def _limit_for(request: Request) -> Limit | None:
    path = request.url.path
    if path == "/api/v1/browser/session":
        return AUTH_LIMIT
    if path == "/api/v1/memory/mcp/oauth/token":
        return TOKEN_LIMIT
    if path.startswith(("/api/v1/search", "/api/v1/memory/retrieve")):
        return READ_LIMIT
    if path.startswith(("/api/v1/ingest", "/api/v1/capture")):
        return WRITE_LIMIT
    return None


def _source_key(request: Request) -> str:
    client_ip = request.client.host if request.client else "unknown"
    credential = request.headers.get("X-API-Key") or request.headers.get("Authorization") or ""
    credential_fingerprint = hashlib.sha256(credential.encode()).hexdigest()[:20] if credential else "anonymous"
    return f"{client_ip}:{credential_fingerprint}"


async def _increment(redis, key: str, window_seconds: int) -> int:
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, window_seconds)
    return int(count)


class SecurityRateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        limit = _limit_for(request)
        redis = getattr(request.app.state, "arq_pool", None)
        # Some test and maintenance contexts attach an enqueue-only ARQ pool.
        # Rate limiting needs the Redis counter interface, so leave those
        # contexts unchanged instead of failing the protected request.
        if limit is None or redis is None or not all(
            hasattr(redis, method) for method in ("incr", "expire", "get", "delete")
        ):
            return await call_next(request)

        source = _source_key(request)
        bucket = f"palace:security-rate:{limit.name}:{source}"
        count = await _increment(redis, bucket, limit.window_seconds)
        if count > limit.requests:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests"},
                headers={"Retry-After": str(limit.window_seconds)},
            )

        failure_key = f"palace:security-auth-failures:{source}"
        if limit in {AUTH_LIMIT, TOKEN_LIMIT}:
            failures = await redis.get(failure_key)
            if failures is not None and int(failures) >= AUTH_LIMIT.requests:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Authentication temporarily locked"},
                    headers={"Retry-After": str(AUTH_LIMIT.window_seconds)},
                )

        response = await call_next(request)
        if limit in {AUTH_LIMIT, TOKEN_LIMIT}:
            if response.status_code in {401, 403}:
                failures = await _increment(redis, failure_key, AUTH_LIMIT.window_seconds)
                if failures == AUTH_LIMIT.requests:
                    logger.warning("Authentication failure lockout activated for source fingerprint %s", source)
            elif response.status_code < 400:
                await redis.delete(failure_key)
        return response
