from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import admin
from app.database import get_db
from app.services.relationship_canary_contract import FIXTURE_SHA256


class FakeSession:
    pass


def _client(monkeypatch, fake_run) -> TestClient:
    app = FastAPI()
    app.include_router(admin.router, prefix="/api/v1")
    app.state.embedder = object()
    app.state.llm = object()

    async def override_get_db():
        yield FakeSession()

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(admin, "_ADMIN_SECRET", "test-admin-secret")
    monkeypatch.setattr(admin, "run_live_canary", fake_run)

    @asynccontextmanager
    async def fake_lock():
        yield

    monkeypatch.setattr(admin, "live_canary_lock", fake_lock)
    return TestClient(app)


def test_sar1083_canary_requires_admin_secret(monkeypatch) -> None:
    async def fake_run(*_args, **_kwargs):
        raise AssertionError("unauthorized request reached the canary")

    client = _client(monkeypatch, fake_run)
    response = client.post(
        "/api/v1/admin/canaries/sar-1083",
        json={
            "authorization_id": "linear-comment:approval",
            "expected_app_version": "deadbeef",
            "fixture_sha256": FIXTURE_SHA256,
        },
    )

    assert response.status_code == 403


def test_sar1083_canary_rejects_noncanonical_fixture_digest(monkeypatch) -> None:
    async def fake_run(*_args, **_kwargs):
        raise AssertionError("tampered request reached the canary")

    client = _client(monkeypatch, fake_run)
    response = client.post(
        "/api/v1/admin/canaries/sar-1083",
        headers={"X-Admin-Secret": "test-admin-secret"},
        json={
            "authorization_id": "linear-comment:approval",
            "expected_app_version": "deadbeef",
            "fixture_sha256": "0" * 64,
        },
    )

    assert response.status_code == 422


def test_sar1083_canary_passes_only_bounded_inputs_to_runner(monkeypatch) -> None:
    calls = []

    async def fake_run(db, **kwargs):
        calls.append((db, kwargs))
        return {"task_id": "SAR-1083", "passed": True}

    client = _client(monkeypatch, fake_run)
    response = client.post(
        "/api/v1/admin/canaries/sar-1083",
        headers={"X-Admin-Secret": "test-admin-secret"},
        json={
            "authorization_id": "linear-comment:approval",
            "expected_app_version": "deadbeef",
            "fixture_sha256": FIXTURE_SHA256,
        },
    )

    assert response.status_code == 200
    assert response.json() == {"task_id": "SAR-1083", "passed": True}
    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["authorization_id"] == "linear-comment:approval"
    assert kwargs["expected_app_version"] == "deadbeef"
    assert set(kwargs) == {
        "embedder",
        "llm",
        "authorization_id",
        "expected_app_version",
    }
