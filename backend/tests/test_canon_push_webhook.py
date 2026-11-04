"""A push hook: the signature, the lookup, and what a delivery does.

A hook is a machine caller, so every refusal must be a status and a sentence, and
every refusal must leave the database unchanged. An unsigned or badly signed
delivery must never start a run.

Set ``CANON_TEST_DATABASE_URL`` to a database at the current alembic head.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.canon import post_canon_sync_hook
from app.models.palace import SyncRun, SyncSource, Wing
from app.services.canon_sync import (
    binding_for_webhook,
    disable_push_webhook,
    enable_push_webhook,
    start_canon_sync_run,
    verify_github_signature,
    webhook_secret,
)

DATABASE_URL = os.environ.get("CANON_TEST_DATABASE_URL")

TEST_KEY = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode()

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="CANON_TEST_DATABASE_URL is not set; a hook needs a real database",
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "master")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "canon").mkdir(exist_ok=True)
    (root / "canon" / "one.md").write_text(
        "---\ntitle: One\nsummary: One page.\nsection: rules\n---\n# One\n\nBody.\n"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return root


class _Request:
    """The two things the handler reads: the raw body and the headers."""

    def __init__(self, body: bytes, headers: dict[str, str]) -> None:
        self._body = body
        self.headers = headers

    async def body(self) -> bytes:
        return self._body


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _payload(*, ref: str = "refs/heads/master", default: str = "master") -> bytes:
    return json.dumps(
        {"ref": ref, "repository": {"default_branch": default, "full_name": "o/r"}}
    ).encode()


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.palace.settings.palaceoftruth_sync_source_credential_key", TEST_KEY
    )
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"canon-hook-{uuid.uuid4().hex[:10]}"
    root = _repo(tmp_path / "corpus")

    async with factory() as db:
        db.add(Wing(tenant_id=tenant, slug="company", name="Company"))
        await db.commit()
        source = SyncSource(
            tenant_id=tenant,
            name="corpus",
            root_path=str(root),
            source_kind="folder",
            credential_type="none",
            status="active",
            scan_interval_seconds=900,
            target_kind="canon",
            canon_section_default="reference",
            canon_owns_pages=True,
        )
        db.add(source)
        await db.commit()
        yield {"db": db, "tenant": tenant, "source": source, "factory": factory}

    await engine.dispose()


async def _runs(db, tenant: str) -> int:
    return int(
        await db.scalar(
            select(func.count()).select_from(SyncRun).where(SyncRun.tenant_id == tenant)
        )
    )


# --- the signature ----------------------------------------------------------


def test_a_correct_signature_is_accepted() -> None:
    body = _payload()

    assert verify_github_signature(secret="s3cret", body=body, header=_sign("s3cret", body))


def test_a_signature_from_another_secret_is_refused() -> None:
    body = _payload()

    assert not verify_github_signature(
        secret="s3cret", body=body, header=_sign("not-the-secret", body)
    )


def test_a_missing_or_malformed_header_is_refused() -> None:
    body = _payload()

    assert not verify_github_signature(secret="s3cret", body=body, header=None)
    assert not verify_github_signature(secret="s3cret", body=body, header="")
    assert not verify_github_signature(secret="s3cret", body=body, header="sha1=abcd")
    assert not verify_github_signature(secret="s3cret", body=body, header="sha256=tooshort")


def test_a_changed_body_breaks_the_signature() -> None:
    body = _payload()
    header = _sign("s3cret", body)

    assert not verify_github_signature(
        secret="s3cret", body=body + b" ", header=header
    )


# --- the stored pair --------------------------------------------------------


@pytest.mark.asyncio
async def test_enabling_a_hook_stores_a_token_and_a_decryptable_secret(env) -> None:
    db, source = env["db"], env["source"]

    token, secret = await enable_push_webhook(db, source=source)

    assert source.webhook_token == token
    assert webhook_secret(source) == secret, "the stored secret must decrypt to what was shown"
    assert await binding_for_webhook(db, token=token) is not None


@pytest.mark.asyncio
async def test_rotating_a_hook_replaces_the_old_pair(env) -> None:
    db, source = env["db"], env["source"]
    first_token, _ = await enable_push_webhook(db, source=source)

    second_token, second_secret = await enable_push_webhook(db, source=source)

    assert second_token != first_token
    assert await binding_for_webhook(db, token=first_token) is None
    assert webhook_secret(source) == second_secret


@pytest.mark.asyncio
async def test_disabling_a_hook_removes_both_halves(env) -> None:
    db, source = env["db"], env["source"]
    token, _ = await enable_push_webhook(db, source=source)

    await disable_push_webhook(db, source=source)

    assert source.webhook_token is None
    assert source.webhook_secret_ciphertext is None
    assert await binding_for_webhook(db, token=token) is None


@pytest.mark.asyncio
async def test_an_unknown_or_oversized_token_finds_nothing(env) -> None:
    db = env["db"]

    assert await binding_for_webhook(db, token="no-such-token") is None
    assert await binding_for_webhook(db, token="x" * 500) is None


# --- what a delivery does ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_ping_is_answered_and_starts_nothing(env) -> None:
    db, tenant, source = env["db"], env["tenant"], env["source"]
    token, secret = await enable_push_webhook(db, source=source)
    body = b'{"zen":"hello"}'

    result = await post_canon_sync_hook(
        _Request(body, {"X-Hub-Signature-256": _sign(secret, body), "X-GitHub-Event": "ping"}),
        token,
        db,
    )

    assert result.status == "pong"
    assert await _runs(db, tenant) == 0


@pytest.mark.asyncio
async def test_a_push_to_another_branch_is_ignored(env) -> None:
    db, tenant, source = env["db"], env["tenant"], env["source"]
    token, secret = await enable_push_webhook(db, source=source)
    body = _payload(ref="refs/heads/feature")

    result = await post_canon_sync_hook(
        _Request(body, {"X-Hub-Signature-256": _sign(secret, body), "X-GitHub-Event": "push"}),
        token,
        db,
    )

    assert result.status == "ignored"
    assert "not the default branch" in (result.reason or "")
    assert await _runs(db, tenant) == 0


@pytest.mark.asyncio
async def test_a_push_to_the_default_branch_queues_one_run(env) -> None:
    db, tenant, source = env["db"], env["tenant"], env["source"]
    token, secret = await enable_push_webhook(db, source=source)
    body = _payload()

    result = await post_canon_sync_hook(
        _Request(body, {"X-Hub-Signature-256": _sign(secret, body), "X-GitHub-Event": "push"}),
        token,
        db,
    )

    assert result.status == "queued"
    assert result.run_id is not None
    run = await db.get(SyncRun, result.run_id)
    assert run is not None
    assert run.triggered_by == "push"
    assert await _runs(db, tenant) == 1


@pytest.mark.asyncio
async def test_a_bad_signature_is_refused_and_starts_nothing(env) -> None:
    db, tenant, source = env["db"], env["tenant"], env["source"]
    token, _ = await enable_push_webhook(db, source=source)
    body = _payload()

    with pytest.raises(HTTPException) as excinfo:
        await post_canon_sync_hook(
            _Request(body, {"X-Hub-Signature-256": _sign("wrong", body), "X-GitHub-Event": "push"}),
            token,
            db,
        )

    assert excinfo.value.status_code == 401
    assert await _runs(db, tenant) == 0


@pytest.mark.asyncio
async def test_an_unknown_token_is_a_404_before_anything_else(env) -> None:
    db = env["db"]
    body = _payload()

    with pytest.raises(HTTPException) as excinfo:
        await post_canon_sync_hook(
            _Request(body, {"X-Hub-Signature-256": _sign("whatever", body), "X-GitHub-Event": "push"}),
            "no-such-token",
            db,
        )

    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_a_push_during_a_run_is_coalesced_not_dropped(env) -> None:
    db, tenant, source = env["db"], env["tenant"], env["source"]
    token, secret = await enable_push_webhook(db, source=source)
    await start_canon_sync_run(db, tenant_id=tenant, source=source)
    body = _payload()

    result = await post_canon_sync_hook(
        _Request(body, {"X-Hub-Signature-256": _sign(secret, body), "X-GitHub-Event": "push"}),
        token,
        db,
    )

    assert result.status == "coalesced"
    await db.refresh(source)
    assert source.last_synced_at is None, "the next poll must treat this binding as due"
    assert await _runs(db, tenant) == 1, "a coalesced push must not queue a second run"
