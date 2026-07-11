from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.main import _parse_default_s3_extensions, _seed_default_palace_sync_source, wait_for_database_startup


def test_parse_default_s3_extensions_splits_csv(monkeypatch) -> None:
    monkeypatch.setattr("app.main.settings.palace_default_s3_allowed_extensions", ".md, txt , .md")
    assert _parse_default_s3_extensions() == [".md", "txt", ".md"]


@pytest.mark.asyncio
async def test_backend_startup_waits_for_writable_database(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_wait(database_url: str, **kwargs) -> None:
        captured["database_url"] = database_url
        captured.update(kwargs)

    monkeypatch.setattr("app.main.settings.database_url", "postgresql+asyncpg://palace")
    monkeypatch.setattr("app.main.wait_for_writable_database", fake_wait)

    await wait_for_database_startup()

    assert captured == {
        "database_url": "postgresql+asyncpg://palace",
        "timeout_seconds": 300,
        "interval_seconds": 5,
        "connect_timeout_seconds": 5,
    }


@pytest.mark.asyncio
async def test_seed_default_palace_sync_source_creates_source_when_missing(monkeypatch) -> None:
    created: dict[str, object] = {}

    monkeypatch.setattr("app.main.settings.palace_default_s3_source_name", "Hermes staging corpus")
    monkeypatch.setattr("app.main.settings.palace_default_s3_bucket", "palaceoftruth-corpus")
    monkeypatch.setattr("app.main.settings.palace_default_s3_prefix", "staging")
    monkeypatch.setattr(
        "app.main.settings.palace_default_s3_endpoint_url",
        "https://4885b5ea3d09d9e10223a0e179815353.r2.cloudflarestorage.com",
    )
    monkeypatch.setattr("app.main.settings.palace_default_s3_region", "auto")
    monkeypatch.setattr("app.main.settings.palace_default_s3_allowed_extensions", ".md")
    monkeypatch.setattr("app.main.settings.palace_default_s3_scan_interval_seconds", 900)
    monkeypatch.setattr("app.main.settings.palace_default_s3_force_path_style", False)

    class FakeDb:
        async def scalar(self, _statement):
            return None

    class FakeSessionManager:
        async def __aenter__(self):
            return FakeDb()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_create_sync_source(db, *, tenant_id: str, body):
        created["tenant_id"] = tenant_id
        created["body"] = body
        return SimpleNamespace(id="source-id")

    monkeypatch.setattr("app.main.async_session", lambda: FakeSessionManager())
    monkeypatch.setattr("app.main.create_sync_source", fake_create_sync_source)

    await _seed_default_palace_sync_source()

    assert created["tenant_id"] == "default"
    body = created["body"]
    assert body.name == "Hermes staging corpus"
    assert body.bucket == "palaceoftruth-corpus"
    assert body.prefix == "staging"
    assert body.endpoint_url == "https://4885b5ea3d09d9e10223a0e179815353.r2.cloudflarestorage.com"
    assert body.region == "auto"
    assert body.allowed_extensions == [".md"]


@pytest.mark.asyncio
async def test_seed_default_palace_sync_source_skips_when_existing(monkeypatch) -> None:
    monkeypatch.setattr("app.main.settings.palace_default_s3_source_name", "Hermes staging corpus")
    monkeypatch.setattr("app.main.settings.palace_default_s3_bucket", "palaceoftruth-corpus")
    monkeypatch.setattr("app.main.settings.palace_default_s3_prefix", "staging")

    class FakeDb:
        async def scalar(self, _statement):
            return "existing-id"

    class FakeSessionManager:
        async def __aenter__(self):
            return FakeDb()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fail_create_sync_source(*_args, **_kwargs):
        raise AssertionError("create_sync_source should not be called when the source already exists")

    monkeypatch.setattr("app.main.async_session", lambda: FakeSessionManager())
    monkeypatch.setattr("app.main.create_sync_source", fail_create_sync_source)

    await _seed_default_palace_sync_source()
