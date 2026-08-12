from __future__ import annotations

import uuid

from app.config import settings
from app.services.data_lifecycle import (
    _purge_staged_paths,
    _restore_staged_paths,
    _stage_item_artifacts,
    _stage_tenant_artifacts,
    _tenant_artifact_directory,
)


def test_tenant_artifact_staging_is_reversible(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "upload_artifact_dir", str(tmp_path))
    active = _tenant_artifact_directory("tenant-a")
    active.mkdir()
    (active / "artifact.bin").write_bytes(b"tenant data")

    staged = _stage_tenant_artifacts("tenant-a")

    assert not active.exists()
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

    staged = _stage_item_artifacts("tenant-a", item_id)

    assert len(staged) == 2
    assert (active / f"{item_id}-other.pdf").exists()
    _purge_staged_paths(staged)
    for quarantine in {path.parent for _original, path in staged}:
        quarantine.rmdir()
    assert sorted(path.name for path in active.iterdir()) == [f"{item_id}-other.pdf"]
