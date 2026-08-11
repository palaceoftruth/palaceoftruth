"""Response security headers for every backend-served response.

The API is reachable both through the frontend nginx `/api/` proxy and directly
on its own ingress host, so the headers are set by the application rather than
by one proxy. nginx sets its own headers for the static SPA it serves; the two
sets never land on the same response.

Known gap: Starlette's ``ServerErrorMiddleware`` sits above every application
middleware, so the bare 500 it emits for an unhandled exception is not stamped.
That body is a fixed string with no attacker-controlled content.
"""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

# API responses are data, not documents: nothing should be loadable or
# executable from them, and they must never be framed. `sandbox` is left out on
# purpose - it blocks downloads in some browsers, and the export routes return
# files the user navigates to directly.
API_CONTENT_SECURITY_POLICY = (
    "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)

# Swagger UI and ReDoc are served by FastAPI from a CDN and use an inline
# bootstrap script, so these two documents need their own policy. Keep it as
# narrow as those tools allow, and never reuse it for anything else.
DOCS_CONTENT_SECURITY_POLICY = (
    "default-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
    "font-src 'self' data: https://cdn.jsdelivr.net https://fonts.gstatic.com; "
    "img-src 'self' data: https://cdn.jsdelivr.net https://fastapi.tiangolo.com; "
    "connect-src 'self'; worker-src 'self' blob:"
)

DOCS_PATHS = ("/docs", "/redoc")

PERMISSIONS_POLICY = (
    "accelerometer=(), camera=(), display-capture=(), geolocation=(), gyroscope=(), "
    "interest-cohort=(), magnetometer=(), microphone=(), payment=(), usb=()"
)

STRICT_TRANSPORT_SECURITY = "max-age=63072000; includeSubDomains; preload"


def _is_docs_path(path: str) -> bool:
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in DOCS_PATHS)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach defence-in-depth headers to every response.

    Existing headers are never overwritten, so a route that needs a different
    policy can set one and keep it.
    """

    def __init__(self, app: ASGIApp, *, hsts: bool = True) -> None:
        super().__init__(app)
        self._hsts = hsts

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        headers = {
            "Content-Security-Policy": (
                DOCS_CONTENT_SECURITY_POLICY
                if _is_docs_path(request.url.path)
                else API_CONTENT_SECURITY_POLICY
            ),
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "strict-origin-when-cross-origin",
            "Permissions-Policy": PERMISSIONS_POLICY,
            "Cross-Origin-Opener-Policy": "same-origin",
        }
        if self._hsts:
            headers["Strict-Transport-Security"] = STRICT_TRANSPORT_SECURITY
        for name, value in headers.items():
            response.headers.setdefault(name, value)
        return response
