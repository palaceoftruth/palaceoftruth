from __future__ import annotations

import uuid

import pytest
from arq.jobs import serialize_job

from app.config import settings
from app.services.bundle import BundleValidationError, item_artifact_tombstone, persist_upload_artifact
from app.services import data_lifecycle
from app.services.data_lifecycle import (
    _finalize_committed_tenant_erasure,
    _purge_tenant_arq_jobs,
    _purge_staged_paths,
    _purge_quarantine_paths,
    _restore_staged_paths,
    _stage_item_artifacts,
    _stage_tenant_artifacts,
    _tenant_artifact_directory,
)
from app.workers.serialization import job_deserializer, job_serializer


class _FakeArqPool:
    default_queue_name = "arq:queue"
    job_deserializer = staticmethod(job_deserializer)

    def __init__(self, payloads: dict[bytes, bytes]) -> None:
        self.payloads = payloads
        self.aborts: dict[str, float] = {}
        self.removed_from_queues: list[tuple[str, str]] = []

    async def scan_iter(self, *, match: str):
        assert match == "arq:job:*"
        for key in list(self.payloads):
            yield key

    async def get(self, key):
        return self.payloads.get(key)

    async def exists(self, _key):
        return 0

    async def zadd(self, key, values):
        assert key == "arq:abort"
        self.aborts.update(values)

    async def zrem(self, queue_name, job_id):
        self.removed_from_queues.append((queue_name, job_id))

    async def delete(self, *keys):
        for key in keys:
            encoded = key.encode() if isinstance(key, str) else key
            self.payloads.pop(encoded, None)


def test_tenant_artifact_staging_is_reversible(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "upload_artifact_dir", str(tmp_path))
    active = _tenant_artifact_directory("tenant-a")
    active.mkdir()
    (active / "artifact.bin").write_bytes(b"tenant data")

    staged = _stage_tenant_artifacts("tenant-a")

    assert active.is_file()
    assert (staged[0][1] / "artifact.bin").read_bytes() == b"tenant data"
    _restore_staged_paths(staged)
    assert (active / "artifact.bin").read_bytes() == b"tenant data"


def test_item_artifact_staging_matches_exact_id_and_extensions(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "upload_artifact_dir", str(tmp_path))
    item_id = uuid.uuid4()
    active = _tenant_artifact_directory("tenant-a")
    active.mkdir()
    (active / str(item_id)).write_bytes(b"exact")
    (active / f"{item_id}.pdf").write_bytes(b"extension")
    (active / f"{item_id}-other.pdf").write_bytes(b"unrelated")
    (active / f".uploading-{item_id}-{uuid.uuid4()}").write_bytes(b"interrupted")

    staged = _stage_item_artifacts("tenant-a", item_id)

    assert len(staged) == 3
    assert (active / f"{item_id}-other.pdf").exists()
    _purge_staged_paths(staged)
    for quarantine in {path.parent for _original, path in staged}:
        quarantine.rmdir()
    assert sorted(path.name for path in active.iterdir()) == [f"{item_id}-other.pdf"]


def test_quarantine_purge_failure_is_reported_and_can_be_retried(
    monkeypatch, tmp_path
) -> None:
    quarantine = tmp_path / ".erasing-item"
    quarantine.mkdir()
    (quarantine / "artifact.bin").write_bytes(b"tenant data")
    real_rmtree = data_lifecycle.shutil.rmtree

    def fail_purge(_path):
        raise PermissionError("purge denied")

    monkeypatch.setattr(data_lifecycle.shutil, "rmtree", fail_purge)
    with pytest.raises(PermissionError, match="purge denied"):
        _purge_quarantine_paths([quarantine])

    assert quarantine.exists()
    monkeypatch.setattr(data_lifecycle.shutil, "rmtree", real_rmtree)
    _purge_quarantine_paths([quarantine])
    assert not quarantine.exists()


def test_item_tombstone_prevents_artifact_recreation(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "upload_artifact_dir", str(tmp_path))
    item_id = uuid.uuid4()
    source = tmp_path / "source.bin"
    source.write_bytes(b"late tenant data")
    tombstone = item_artifact_tombstone("tenant-a", item_id)
    tombstone.parent.mkdir(parents=True)
    tombstone.touch()

    with pytest.raises(BundleValidationError, match="permanently deleted"):
        persist_upload_artifact(
            str(source),
            tenant_id="tenant-a",
            item_id=item_id,
            extension=".bin",
        )

    assert not (tombstone.parent / f"{item_id}.bin").exists()


@pytest.mark.asyncio
async def test_tenant_erasure_removes_only_matching_arq_payloads() -> None:
    tenant_job_id = str(uuid.uuid4())

    def payload(job_id: str, kwargs: dict) -> tuple[bytes, bytes]:
        return (
            f"arq:job:{job_id}".encode(),
            serialize_job(
                "process_note",
                (),
                kwargs,
                None,
                0,
                serializer=job_serializer,
            ),
        )

    pool = _FakeArqPool(
        dict(
            [
                payload("tenant-direct", {"tenant_id": "tenant-a"}),
                payload("tenant-reference", {"job_id": tenant_job_id}),
                payload("other", {"tenant_id": "tenant-b"}),
            ]
        )
    )

    purged = await _purge_tenant_arq_jobs(
        pool,
        tenant_id="tenant-a",
        tenant_identifiers={tenant_job_id},
    )

    assert purged == 2
    assert set(pool.aborts) == {"tenant-direct", "tenant-reference"}
    assert set(pool.payloads) == {b"arq:job:other"}


@pytest.mark.asyncio
async def test_tenant_erasure_does_not_match_arbitrary_payload_text() -> None:
    key = b"arq:job:other-tenant"
    pool = _FakeArqPool(
        {
            key: serialize_job(
                "process_note",
                (),
                {"tenant_id": "tenant-b", "title": "tenant-a", "content": "tenant-a"},
                None,
                0,
                serializer=job_serializer,
            )
        }
    )

    purged = await _purge_tenant_arq_jobs(
        pool,
        tenant_id="tenant-a",
        tenant_identifiers=set(),
    )

    assert purged == 0
    assert key in pool.payloads


@pytest.mark.asyncio
async def test_tenant_erasure_matches_uuid_shaped_legacy_positional_ids() -> None:
    item_id = str(uuid.uuid4())
    key = b"arq:job:legacy-positional"
    pool = _FakeArqPool(
        {
            key: serialize_job(
                "extract_relationships",
                (item_id,),
                {},
                None,
                0,
                serializer=job_serializer,
            )
        }
    )

    candidates = await data_lifecycle._queued_identifier_candidates(pool)
    purged = await _purge_tenant_arq_jobs(
        pool,
        tenant_id="tenant-a",
        tenant_identifiers={item_id},
    )

    assert candidates == {item_id}
    assert purged == 1
    assert key not in pool.payloads


def test_item_erasure_identifier_match_does_not_match_tenant_id_alone() -> None:
    item_id = str(uuid.uuid4())

    assert data_lifecycle._payload_references_identifiers(
        {"kwargs": {"tenant_id": "tenant-a", "item_id": item_id}},
        {item_id},
    )
    assert not data_lifecycle._payload_references_identifiers(
        {"kwargs": {"tenant_id": "tenant-a", "item_id": str(uuid.uuid4())}},
        {item_id},
    )


@pytest.mark.asyncio
async def test_tenant_erasure_fails_closed_on_uninspectable_arq_payload() -> None:
    pool = _FakeArqPool({b"arq:job:corrupt": b"not-a-job"})

    with pytest.raises(RuntimeError, match="Could not inspect ARQ payload"):
        await _purge_tenant_arq_jobs(
            pool,
            tenant_id="tenant-a",
            tenant_identifiers=set(),
        )

    assert b"arq:job:corrupt" in pool.payloads


@pytest.mark.asyncio
async def test_committed_tenant_erasure_purges_files_when_final_queue_scan_fails(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(settings, "upload_artifact_dir", str(tmp_path))
    original = _tenant_artifact_directory("tenant-a")
    staged = original.with_name(f".{original.name}.erasing-previous")
    staged.mkdir()
    (staged / "artifact.bin").write_bytes(b"tenant data")

    async def fail_queue_scan(*args, **kwargs):
        raise ConnectionError("redis unavailable")

    monkeypatch.setattr(data_lifecycle, "_purge_tenant_arq_jobs", fail_queue_scan)

    with pytest.raises(ConnectionError, match="redis unavailable"):
        await _finalize_committed_tenant_erasure(
            object(),
            tenant_id="tenant-a",
            tenant_identifiers=set(),
            staged_paths=[(original, staged)],
        )

    assert not staged.exists()
