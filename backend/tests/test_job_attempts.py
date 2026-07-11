import uuid
from datetime import datetime, timezone

import pytest

from app.models.job import Job, JobAttempt
from app.services.job_attempts import (
    MAX_ERROR_SUMMARY_LENGTH,
    create_job_attempt,
    mark_job_attempt_completed,
    mark_job_attempt_dead_lettered,
    mark_job_attempt_failed,
    mark_job_attempt_started,
    sanitize_error_summary,
)


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalar_one(self):
        return self.value


class _Session:
    def __init__(self, *results):
        self.results = list(results)
        self.added = []
        self.flushed = False

    async def execute(self, _statement):
        return _ScalarResult(self.results.pop(0))

    async def get(self, _model, _key, **_kwargs):
        return self.results.pop(0)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flushed = True


def test_job_attempt_schema_has_required_guards():
    table = JobAttempt.__table__
    assert table.c.trigger.type.length == 32
    assert table.c.status.type.length == 24
    assert table.c.failure_kind.type.length == 48
    assert table.c.job_id.foreign_keys.pop().ondelete == "CASCADE"
    assert table.c.recovered_from_id.foreign_keys.pop().ondelete == "SET NULL"
    assert {index.name for index in table.indexes} >= {
        "ix_job_attempts_tenant_created",
        "ix_job_attempts_job_status",
        "ix_job_attempts_arq_job_id",
        "uq_job_attempts_active_job",
    }


@pytest.mark.asyncio
async def test_create_job_attempt_locks_parent_and_allocates_next_number():
    job_id = uuid.uuid4()
    job = Job(id=job_id, tenant_id="tenant-a", job_type="ingest")
    db = _Session(job, None, 3)

    attempt = await create_job_attempt(
        db,
        job_id=job_id,
        tenant_id="tenant-a",
        trigger="retry",
        arq_job_id="arq-3",
        job_try=3,
    )

    assert attempt.attempt_number == 3
    assert attempt.status == "queued"
    assert attempt.arq_job_id == "arq-3"
    assert db.added == [attempt]
    assert db.flushed is True


@pytest.mark.asyncio
async def test_create_job_attempt_rejects_existing_active_attempt():
    job_id = uuid.uuid4()
    job = Job(id=job_id, tenant_id="tenant-a", job_type="ingest")
    db = _Session(job, uuid.uuid4())

    with pytest.raises(ValueError, match="already has an active attempt"):
        await create_job_attempt(db, job_id=job_id, tenant_id="tenant-a", trigger="retry")


@pytest.mark.asyncio
async def test_lifecycle_helpers_are_monotonic_and_idempotent():
    attempt = JobAttempt(
        id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        tenant_id="tenant-a",
        attempt_number=1,
        trigger="initial",
        status="queued",
    )
    started_at = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)
    completed_at = datetime(2026, 7, 11, 12, 1, tzinfo=timezone.utc)

    await mark_job_attempt_started(_Session(attempt), attempt_id=attempt.id, at=started_at)
    assert attempt.status == "processing"
    assert attempt.started_at == started_at

    await mark_job_attempt_completed(_Session(attempt), attempt_id=attempt.id, at=completed_at)
    assert attempt.status == "completed"
    assert attempt.completed_at == completed_at

    await mark_job_attempt_failed(
        _Session(attempt),
        attempt_id=attempt.id,
        failure_kind="worker_error",
        error="should not overwrite completion",
    )
    assert attempt.status == "completed"
    assert attempt.failure_kind is None


@pytest.mark.asyncio
async def test_failure_and_dead_letter_helpers_sanitize_diagnostics():
    failed = JobAttempt(
        id=uuid.uuid4(), job_id=uuid.uuid4(), tenant_id="t", attempt_number=1,
        trigger="initial", status="processing",
    )
    await mark_job_attempt_failed(
        _Session(failed), attempt_id=failed.id, failure_kind="x" * 100,
        error="token=super-secret " + "details " * 100,
    )
    assert failed.status == "failed"
    assert failed.failure_kind == "x" * 48
    assert "super-secret" not in failed.error_summary
    assert len(failed.error_summary) == MAX_ERROR_SUMMARY_LENGTH

    dead = JobAttempt(
        id=uuid.uuid4(), job_id=uuid.uuid4(), tenant_id="t", attempt_number=2,
        trigger="recovery", status="queued", recovered_from_id=failed.id,
    )
    await mark_job_attempt_dead_lettered(
        _Session(dead), attempt_id=dead.id, failure_kind="retries_exhausted", error=None,
    )
    assert dead.status == "dead_lettered"
    assert dead.dead_lettered_at is not None


def test_error_summary_normalizes_whitespace_and_handles_none():
    assert sanitize_error_summary(None) is None
    assert sanitize_error_summary("bad\n  request") == "bad request"
