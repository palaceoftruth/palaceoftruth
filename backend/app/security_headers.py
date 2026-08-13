"""Response security headers for every backend-served response.

The API is reachable both through the frontend nginx `/api/` proxy and directly
on its own ingress host, so the headers are set by the application rather than
by one proxy. nginx sets its own headers for the static SPA it serves; the two
sets never land on the same response.

An application exception handler stamps the same headers on unhandled 500s.
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
DOCS_CONTENT_SECURITY_POLICY = (
    "default-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
    "font-src 'self' data: https://cdn.jsdelivr.net https://fonts.gstatic.com; "
    "img-src 'self' data: https://cdn.jsdelivr.net https://fastapi.tiangolo.com; "
    "connect-src 'self'; worker-src 'self' blob:"
)

PERMISSIONS_POLICY = (
    "accelerometer=(), camera=(), display-capture=(), geolocation=(), gyroscope=(), "
    "interest-cohort=(), magnetometer=(), microphone=(), payment=(), usb=()"
)

STRICT_TRANSPORT_SECURITY = "max-age=63072000; includeSubDomains; preload"
DOCS_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc"})


def apply_security_headers(
    response: Response,
    *,
    hsts: bool = True,
    content_security_policy: str = API_CONTENT_SECURITY_POLICY,
) -> Response:
    headers = {
        "Content-Security-Policy": content_security_policy,
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": PERMISSIONS_POLICY,
        "Cross-Origin-Opener-Policy": "same-origin",
    }
    if hsts:
        headers["Strict-Transport-Security"] = STRICT_TRANSPORT_SECURITY
    for name, value in headers.items():
        response.headers.setdefault(name, value)
    return response


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
        policy = (
            DOCS_CONTENT_SECURITY_POLICY
            if request.url.path in DOCS_PATHS
            else API_CONTENT_SECURITY_POLICY
        )
        return apply_security_headers(
            response,
            hsts=self._hsts,
            content_security_policy=policy,
        )
