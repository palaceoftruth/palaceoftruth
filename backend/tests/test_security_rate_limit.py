from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin_host import AdminHostMiddleware
from app.security_headers import SecurityHeadersMiddleware
from app.security_rate_limit import SecurityRateLimitMiddleware


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    async def expire(self, _key: str, _seconds: int) -> None:
        return None

    async def get(self, key: str):
        value = self.values.get(key)
        return str(value).encode() if value is not None else None

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)


def _client(status_code: int = 200) -> TestClient:
    app = FastAPI()
    app.state.arq_pool = FakeRedis()
    app.add_middleware(SecurityRateLimitMiddleware)

    @app.post("/api/v1/browser/session", status_code=status_code)
    async def session():
        return {"ok": True}

    return TestClient(app)


def test_auth_endpoint_is_rate_limited_without_exposing_credential() -> None:
    client = _client()
    for _ in range(10):
        assert client.post("/api/v1/browser/session", headers={"X-API-Key": "top-secret"}).status_code == 200
    response = client.post("/api/v1/browser/session", headers={"X-API-Key": "top-secret"})

    assert response.status_code == 429
    assert response.headers["retry-after"] == "300"
    assert "top-secret" not in str(client.app.state.arq_pool.values)


def test_failed_authentication_enters_temporary_lockout() -> None:
    client = _client(status_code=401)
    for _ in range(10):
        client.post("/api/v1/browser/session")

    response = client.post("/api/v1/browser/session")
    assert response.status_code == 429


def test_security_headers_cover_rate_limit_and_admin_host_denials() -> None:
    app = FastAPI()
    app.state.arq_pool = FakeRedis()
    app.add_middleware(AdminHostMiddleware)
    app.add_middleware(SecurityRateLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware, hsts=False)

    @app.post("/api/v1/browser/session")
    async def session():
        return {"ok": True}

    client = TestClient(app)
    for _ in range(10):
        client.post("/api/v1/browser/session")

    rate_limited = client.post("/api/v1/browser/session")
    wrong_admin_host = client.get("/api/v1/admin/tenants")

    assert rate_limited.status_code == 429
    assert wrong_admin_host.status_code == 404
    assert rate_limited.headers["x-content-type-options"] == "nosniff"
    assert wrong_admin_host.headers["x-content-type-options"] == "nosniff"
