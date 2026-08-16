from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.models.item import Item
from app.models.job import Job
from app.services.image_candidates import ImageCandidateError
from scripts import backfill_image_summaries as backfill


TENANT = "tenant-test"


def _item(*, status: str = "captured_not_processed", claim: dict | None = None, error: dict | None = None) -> Item:
    image = {
        "source": "browser_image_candidate",
        "status": status,
        "source_post_url": "https://social.example/post/1",
        "candidate_url": "https://cdn.example/image.png",
        "final_url": "https://cdn.example/image.png",
        "media_type": "image/png",
        "byte_hash": hashlib.sha256(b"image-bytes").hexdigest(),
        "byte_size": 11,
    }
    if claim is not None:
        image["backfill_claim"] = claim
    if error is not None:
        image["backfill_error"] = error
    return Item(
        id=uuid.uuid4(),
        tenant_id=TENANT,
        source_type="image_candidate",
        title="candidate",
        status="captured",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        metadata_={"browser_capture_image": image},
    )


class _Result:
    def __init__(self, rows=(), scalar=None):
        self.rows = list(rows)
        self.scalar = scalar

    def scalars(self):
        return self

    def all(self):
        return self.rows

    def scalar_one_or_none(self):
        return self.scalar


class _Session:
    def __init__(self, item: Item, role: str):
        self.item = item
        self.role = role
        self.commits = 0
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, _statement):
        self.calls += 1
        if self.role == "initial":
            return _Result([self.item])
        if self.calls == 1:
            return _Result([self.item], scalar=self.item)
        return _Result(scalar=None)

    def add(self, _value):
        self.added = _value

    async def flush(self):
        if getattr(self, "added", None) is not None and getattr(self.added, "id", None) is None:
            self.added.id = uuid.uuid4()

    async def commit(self):
        self.commits += 1


class _Factory:
    def __init__(self, item: Item):
        self.item = item
        self.roles = iter(("initial", "claim", "materialize"))
        self.sessions: list[_Session] = []

    def __call__(self, _tenant: str):
        session = _Session(self.item, next(self.roles, "materialize"))
        self.sessions.append(session)
        return session


def test_eligibility_accepts_retryable_and_stale_claims_but_excludes_terminal_states():
    now = datetime.now(timezone.utc)
    assert backfill._eligible(_item())
    assert backfill._eligible(_item(error={"classification": "retryable"}))
    assert backfill._eligible(
        _item(
            claim={
                "token": "old",
                "claimed_at": (now - timedelta(minutes=16)).isoformat(),
            }
        ),
        now=now,
    )
    for status in ("completed", "queued", "processing", "deleted", "permanent_failed"):
        assert not backfill._eligible(_item(status=status))
    assert not backfill._eligible(_item(error={"classification": "permanent"}))
    assert not backfill._eligible(
        _item(
            claim={"token": "fresh", "claimed_at": now.isoformat()},
            error={"classification": "retryable"},
        ),
        now=now,
    )


def test_failure_classification_is_sanitized_and_bounded():
    assert backfill._classify_download_error(TimeoutError("secret response")).retryable
    assert backfill._classify_download_error(SimpleNamespace(status_code=429)).code == "rate_limited"
    assert not backfill._classify_download_error(SimpleNamespace(status_code=404)).retryable
    assert "secret" not in backfill._classify_download_error(ValueError("secret response")).message
    assert backfill._classify_download_error(
        ImageCandidateError("raw provider body", code="server_error", retryable=True)
    ).code == "server_error"


def test_candidate_query_is_tenant_scoped_deterministic_and_bounded():
    sql = str(
        backfill._candidate_query(
            tenant_id=TENANT,
            limit=7,
            now=datetime(2026, 8, 15, tzinfo=timezone.utc),
        ).compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )

    assert f"items.tenant_id = '{TENANT}'" in sql
    assert "items.source_type = 'image_candidate'" in sql
    assert "ORDER BY items.created_at ASC, items.id ASC" in sql
    assert "LIMIT 7" in sql


@pytest.mark.asyncio
async def test_dry_run_only_selects_and_does_not_download_or_write():
    item = _item()
    factory = _Factory(item)
    calls = []

    async def downloader(*_args):
        calls.append("download")

    report = await backfill.run_backfill(
        tenant_id=TENANT,
        limit=10,
        dry_run=True,
        session_factory=factory,
        downloader=downloader,
    )
    assert report.eligible == 1
    assert report.queued == 0
    assert calls == []
    assert factory.sessions[0].commits == 0


@pytest.mark.asyncio
async def test_success_uses_mocked_download_and_queue_and_creates_one_job(monkeypatch):
    item = _item()
    factory = _Factory(item)
    queued = []
    content = b"image-bytes"
    downloaded = SimpleNamespace(
        content=content,
        byte_hash=hashlib.sha256(content).hexdigest(),
        media_type="image/png",
        extension=".png",
        final_url="https://cdn.example/image.png",
    )

    async def downloader(source_url, candidate_url):
        assert source_url.startswith("https://social.example/")
        assert candidate_url.startswith("https://cdn.example/")
        return downloaded

    async def enqueue(pool, name, **kwargs):
        queued.append((pool, name, kwargs))

    monkeypatch.setattr(backfill, "persist_upload_artifact_bytes", lambda *args, **kwargs: "/tmp/tenant/image.png")
    report = await backfill.run_backfill(
        tenant_id=TENANT,
        limit=1,
        session_factory=factory,
        downloader=downloader,
        queue_pool=object(),
        enqueue=enqueue,
    )
    assert report.eligible == 1
    assert report.queued == 1
    assert len(queued) == 1
    assert queued[0][1] == "process_image"
    assert queued[0][2]["tenant_id"] == TENANT
    assert factory.sessions[0].commits == 0
    assert factory.sessions[1].commits == 1
    assert factory.sessions[2].commits == 1
    assert factory.sessions[2].item.status == "processing"
    assert factory.sessions[2].item.metadata_["browser_capture_image"]["final_url"] == "https://cdn.example/image.png"


@pytest.mark.asyncio
async def test_hash_mismatch_writes_no_artifact_or_job_and_keeps_original_metadata(monkeypatch):
    item = _item()
    original_metadata = {
        "browser_capture_image": dict(item.metadata_["browser_capture_image"])
    }
    factory = _Factory(item)
    queued = []
    persisted = []

    async def downloader(*_args):
        content = b"changed-remote-bytes"
        return SimpleNamespace(
            content=content,
            byte_hash=hashlib.sha256(content).hexdigest(),
            media_type="image/png",
            extension=".png",
            final_url="https://other.example/changed.png",
        )

    async def enqueue(*args, **kwargs):
        queued.append((args, kwargs))

    monkeypatch.setattr(
        backfill,
        "persist_upload_artifact_bytes",
        lambda *args, **kwargs: persisted.append((args, kwargs)),
    )
    report = await backfill.run_backfill(
        tenant_id=TENANT,
        limit=1,
        session_factory=factory,
        downloader=downloader,
        queue_pool=object(),
        enqueue=enqueue,
    )

    assert report.hash_mismatches == 1
    assert report.queued == 0
    assert persisted == []
    assert queued == []
    browser = item.metadata_["browser_capture_image"]
    assert browser["byte_hash"] == original_metadata["browser_capture_image"]["byte_hash"]
    assert browser["candidate_url"] == original_metadata["browser_capture_image"]["candidate_url"]
    assert browser["final_url"] == original_metadata["browser_capture_image"]["final_url"]
    assert browser["backfill_error"]["classification"] == "permanent"


@pytest.mark.asyncio
async def test_claim_skips_existing_active_image_job_without_mutation():
    item = _item()

    class ActiveJobSession:
        def __init__(self):
            self.calls = 0
            self.commits = 0

        async def execute(self, _statement):
            self.calls += 1
            if self.calls == 1:
                return _Result(scalar=item)
            return _Result(scalar=uuid.uuid4())

        async def commit(self):
            self.commits += 1

    session = ActiveJobSession()
    snapshot = await backfill._claim_one(
        session,
        tenant_id=TENANT,
        item=item,
        now=datetime.now(timezone.utc),
    )

    assert snapshot is None
    assert session.commits == 0
    assert "backfill_claim" not in item.metadata_["browser_capture_image"]


@pytest.mark.asyncio
async def test_enqueue_failure_keeps_artifact_and_marks_retryable():
    item = _item(status="queued")
    item.status = "processing"
    item.metadata_["browser_capture_image"]["artifact"] = {
        "storage_path": "/safe/tenant/image.png"
    }
    item.metadata_["image_analysis"] = {
        "status": "queued",
        "artifact": {"storage_path": "/safe/tenant/image.png"},
        "vision": {"provider": "openrouter", "error": None},
    }
    job = Job(
        id=uuid.uuid4(),
        item_id=item.id,
        tenant_id=TENANT,
        job_type="image",
        status="queued",
    )

    class FailureSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def scalar(self, statement):
            return job if "FROM jobs" in str(statement) else item

        async def commit(self):
            return None

    await backfill._record_enqueue_failure(
        backfill.CandidateSnapshot(
            item_id=item.id,
            tenant_id=TENANT,
            metadata=item.metadata_,
            browser_image=item.metadata_["browser_capture_image"],
            claim_token="claim",
        ),
        str(job.id),
        session_factory=lambda _tenant: FailureSession(),
    )

    assert job.status == "failed"
    assert item.status == "failed"
    assert item.metadata_["browser_capture_image"]["artifact"]["storage_path"] == "/safe/tenant/image.png"
    assert item.metadata_["browser_capture_image"]["backfill_error"]["classification"] == "retryable"
    assert item.metadata_["image_analysis"]["vision"]["error"]["retryable"] is True
