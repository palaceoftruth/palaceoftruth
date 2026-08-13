"""Security response headers must reach every backend response (M-14)."""

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from app.security_headers import (
    API_CONTENT_SECURITY_POLICY,
    DOCS_CONTENT_SECURITY_POLICY,
    STRICT_TRANSPORT_SECURITY,
    SecurityHeadersMiddleware,
)


def _client(*, hsts: bool = True) -> TestClient:
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware, hsts=hsts)

    @app.get("/api/v1/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/docs")
    def docs() -> PlainTextResponse:
        return PlainTextResponse("swagger")

    @app.get("/api/v1/denied")
    def denied() -> dict:
        raise HTTPException(status_code=403, detail="Missing API key")

    @app.get("/api/v1/own-policy")
    def own_policy() -> PlainTextResponse:
        return PlainTextResponse("custom", headers={"Content-Security-Policy": "default-src 'self'"})

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    "header,expected",
    [
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "strict-origin-when-cross-origin"),
        ("Cross-Origin-Opener-Policy", "same-origin"),
        ("Strict-Transport-Security", STRICT_TRANSPORT_SECURITY),
    ],
)
def test_api_responses_carry_defence_in_depth_headers(header: str, expected: str) -> None:
    response = _client().get("/api/v1/health")

    assert response.status_code == 200
    assert response.headers[header] == expected


def test_api_responses_deny_framing_and_loading() -> None:
    response = _client().get("/api/v1/health")

    assert response.headers["Content-Security-Policy"] == API_CONTENT_SECURITY_POLICY
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert "Permissions-Policy" in response.headers


def test_docs_get_only_the_cdn_policy_required_by_fastapi() -> None:
    client = _client()

    docs = client.get("/docs")
    api = client.get("/api/v1/health")

    assert docs.headers["Content-Security-Policy"] == DOCS_CONTENT_SECURITY_POLICY
    assert api.headers["Content-Security-Policy"] == API_CONTENT_SECURITY_POLICY
    assert "https://cdn.jsdelivr.net" in docs.headers["Content-Security-Policy"]


def test_headers_reach_error_responses_too() -> None:
    client = _client()

    denied = client.get("/api/v1/denied")
    missing = client.get("/api/v1/no-such-route")

    for response in (denied, missing):
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Content-Security-Policy"] == API_CONTENT_SECURITY_POLICY


def test_a_route_that_sets_its_own_policy_keeps_it() -> None:
    response = _client().get("/api/v1/own-policy")

    assert response.headers["Content-Security-Policy"] == "default-src 'self'"


def test_hsts_can_be_disabled() -> None:
    response = _client(hsts=False).get("/api/v1/health")

    assert "Strict-Transport-Security" not in response.headers
