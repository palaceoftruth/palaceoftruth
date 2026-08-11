"""Keep control-plane routes off public application hosts."""

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings


class AdminHostMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/api/v1/admin"):
            allowed = {host.strip().lower() for host in settings.admin_allowed_hosts.split(",") if host.strip()}
            host = (request.url.hostname or "").lower()
            if host not in allowed:
                return JSONResponse(status_code=404, content={"detail": "Not found"})
        return await call_next(request)
