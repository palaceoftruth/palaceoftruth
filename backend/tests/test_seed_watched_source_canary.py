import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from scripts.seed_watched_source_canary import (
    DEFAULT_HOST,
    DEFAULT_TENANT_ID,
    DEFAULT_URL,
    parse_args,
    seed,
)


def test_canary_seed_defaults_are_internal_and_zero_write() -> None:
    args = parse_args([])

    assert args.tenant_id == DEFAULT_TENANT_ID
    assert args.url == DEFAULT_URL
    assert args.allowed_host == DEFAULT_HOST
    assert args.write is False
    assert args.refresh_slo_seconds == 900


@pytest.mark.parametrize(
    "argv",
    [
        ["--url", DEFAULT_URL, "--allowed-host", DEFAULT_HOST],
        ["--tenant-id", DEFAULT_TENANT_ID],
        ["--refresh-slo-seconds", "60"],
        ["--refresh-slo-seconds", "7200"],
    ],
)
def test_canary_seed_rejects_scope_expansion(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        parse_args(argv)


@pytest.mark.asyncio
async def test_canary_dry_run_never_loads_database(monkeypatch) -> None:
    import app.database

    def fail_if_opened():
        raise AssertionError("dry-run must not open a database session")

    monkeypatch.setattr(app.database, "async_session", fail_if_opened)

    report = await seed(parse_args([]))

    assert report["mode"] == "dry_run"
    assert report["created"] is False


class _Nested:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSession:
    def __init__(self, *, existing=None, conflict_once: bool = False) -> None:
        self.existing = existing
        self.conflict_once = conflict_once
        self.added = []
        self.committed = False

    def begin_nested(self):
        return _Nested()

    async def scalar(self, _statement):
        return self.existing

    def add(self, value) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        if self.conflict_once:
            self.conflict_once = False
            self.existing = type("Existing", (), {"id": uuid.uuid4()})()
            raise IntegrityError("insert", {}, Exception("duplicate"))
        if self.added and getattr(self.added[0], "id", None) is None:
            self.added[0].id = uuid.uuid4()

    async def commit(self) -> None:
        self.committed = True


class _Manager:
    def __init__(self, session) -> None:
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_canary_write_is_additive_and_replay_preserves_existing(monkeypatch) -> None:
    import app.database

    existing = type("Existing", (), {"id": uuid.uuid4(), "status": "paused"})()
    session = _FakeSession(existing=existing)
    monkeypatch.setattr(app.database, "async_session", lambda: _Manager(session))

    report = await seed(parse_args(["--write"]))

    assert report["already_present"] is True
    assert report["resource_id"] == str(existing.id)
    assert existing.status == "paused"
    assert session.added == []
    assert session.committed is False


@pytest.mark.asyncio
async def test_canary_write_recovers_idempotency_race(monkeypatch) -> None:
    import app.database

    session = _FakeSession(conflict_once=True)
    monkeypatch.setattr(app.database, "async_session", lambda: _Manager(session))

    report = await seed(parse_args(["--write"]))

    assert report["already_present"] is True
    assert report["created"] is False
    assert report["resource_id"] == str(session.existing.id)
