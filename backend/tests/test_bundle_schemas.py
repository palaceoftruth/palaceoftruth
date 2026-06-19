import asyncio
import io
import json
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.embedding_profile import DEFAULT_EMBEDDING_MODEL
from app.models.item import Item
from app.schemas.bundle import BUNDLE_VERSION
from app.services.bundle import (
    BundleValidationError,
    build_bundle_archive,
    materialize_bundle_upload_artifacts,
    parse_bundle_archive,
)


def _make_bundle(
    *,
    bundle_version: int = BUNDLE_VERSION,
    role: str = "user",
    upload_artifact: dict[str, object] | None = None,
    artifact_in_metadata: bool = False,
    artifact_bytes: bytes | None = None,
) -> bytes:
    now = datetime.now(timezone.utc).isoformat()
    manifest = {
        "bundle_version": bundle_version,
        "exported_at": now,
        "source_instance": {"app": "palaceoftruth", "tenant_id": "tenant-a"},
        "embedding": {"source_model": DEFAULT_EMBEDDING_MODEL, "rebuild_required": True},
        "items_file": "items.json",
        "conversations_file": "conversations.json",
        "artifacts_dir": "artifacts" if upload_artifact else None,
    }
    items = [
        {
            "id": str(uuid.uuid4()),
            "source_type": "note",
            "source_url": None,
            "title": "One",
            "summary": "Summary",
            "raw_content": "Hello world",
            "content_chunks": [{"index": 0, "text": "Hello world"}],
            "metadata": {
                "a": 1,
                **({"upload_artifact": upload_artifact} if artifact_in_metadata and upload_artifact else {}),
            },
            **({"upload_artifact": upload_artifact} if upload_artifact and not artifact_in_metadata else {}),
            "tags": ["x"],
            "categories": ["y"],
            "content_hash": "abc123",
            "created_at": now,
            "updated_at": now,
        }
    ]
    conversations = [
        {
            "id": str(uuid.uuid4()),
            "title": "Conversation",
            "created_at": now,
            "updated_at": now,
            "messages": [
                {
                    "id": str(uuid.uuid4()),
                    "role": role,
                    "content": "Hi",
                    "created_at": now,
                }
            ],
        }
    ]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("items.json", json.dumps(items))
        zf.writestr("conversations.json", json.dumps(conversations))
        if upload_artifact and artifact_bytes is not None:
            bundle_path = upload_artifact.get("bundle_path")
            if isinstance(bundle_path, str):
                zf.writestr(bundle_path, artifact_bytes)
    return buf.getvalue()


def test_parse_bundle_archive_round_trip_preserves_content_hash() -> None:
    payload = parse_bundle_archive(_make_bundle())

    assert payload.manifest.bundle_version == BUNDLE_VERSION
    assert payload.items[0].content_hash == "abc123"
    assert payload.conversations[0].messages[0].role == "user"


def test_parse_bundle_archive_accepts_explicit_upload_artifact_reference() -> None:
    payload = parse_bundle_archive(
        _make_bundle(
            upload_artifact={
                "source": "user_upload",
                "filename": "brief.pdf",
                "media_type": "application/pdf",
                "extension": ".pdf",
                "bundle_path": "artifacts/item-1.pdf",
            },
            artifact_bytes=b"%PDF original",
        )
    )

    assert payload.manifest.artifacts_dir == "artifacts"
    assert payload.items[0].metadata == {"a": 1}
    assert payload.items[0].upload_artifact is not None
    assert payload.items[0].upload_artifact.filename == "brief.pdf"
    assert payload.items[0].upload_artifact.bundle_path == "artifacts/item-1.pdf"


def test_parse_bundle_archive_rejects_missing_upload_artifact_bytes() -> None:
    with pytest.raises(BundleValidationError, match="Bundle is missing upload artifact"):
        parse_bundle_archive(
            _make_bundle(
                upload_artifact={
                    "source": "user_upload",
                    "filename": "brief.pdf",
                    "media_type": "application/pdf",
                    "extension": ".pdf",
                    "bundle_path": "artifacts/item-1.pdf",
                }
            )
        )


def test_parse_bundle_archive_rejects_unsafe_upload_artifact_path() -> None:
    with pytest.raises(BundleValidationError, match="Bundle upload artifact path is invalid"):
        parse_bundle_archive(
            _make_bundle(
                upload_artifact={
                    "source": "user_upload",
                    "filename": "brief.pdf",
                    "media_type": "application/pdf",
                    "extension": ".pdf",
                    "bundle_path": "../item-1.pdf",
                },
                artifact_bytes=b"%PDF original",
            )
        )


def test_parse_bundle_archive_accepts_legacy_upload_artifact_metadata() -> None:
    payload = parse_bundle_archive(
        _make_bundle(
            upload_artifact={
                "source": "user_upload",
                "filename": "brief.pdf",
                "media_type": "application/pdf",
                "extension": ".pdf",
            },
            artifact_in_metadata=True,
        )
    )

    assert payload.items[0].upload_artifact is None
    assert payload.items[0].metadata["upload_artifact"]["filename"] == "brief.pdf"


def test_parse_bundle_archive_rejects_unsupported_version() -> None:
    with pytest.raises(BundleValidationError, match="Unsupported bundle_version"):
        parse_bundle_archive(_make_bundle(bundle_version=999))


def test_parse_bundle_archive_rejects_invalid_message_role() -> None:
    with pytest.raises(BundleValidationError, match="Bundle payload is invalid"):
        parse_bundle_archive(_make_bundle(role="system"))


class _ScalarResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def scalars(self) -> "_ScalarResult":
        return self

    def all(self) -> list[object]:
        return self._rows


class _BundleSession:
    def __init__(self, *, items: list[Item]) -> None:
        self._items = items
        self._item_query_count = 0

    async def execute(self, statement):
        entity = statement.column_descriptions[0].get("entity")
        if entity is Item:
            if self._item_query_count == 0:
                self._item_query_count += 1
                return _ScalarResult(self._items)
            return _ScalarResult([])
        return _ScalarResult([])


def test_build_bundle_archive_writes_upload_artifact_bytes(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    artifact_path = tmp_path / "stored-brief.pdf"
    artifact_path.write_bytes(b"%PDF-1.7 original bytes")
    item = Item(
        id=uuid.uuid4(),
        source_type="doc",
        title="Recovered brief",
        summary="Summary",
        raw_content="Recovered text",
        content_chunks=[{"index": 0, "text": "Recovered text"}],
        metadata_={
            "upload_artifact": {
                "source": "user_upload",
                "filename": "brief.pdf",
                "media_type": "application/pdf",
                "extension": ".pdf",
                "storage_path": str(artifact_path),
            },
            "doc_title": "Recovered brief",
        },
        tags=["brief"],
        categories=["docs"],
        content_hash="abc123",
        tenant_id="tenant-a",
        status="ready",
        created_at=now,
        updated_at=now,
    )
    session = _BundleSession(items=[item])
    out_path = tmp_path / "bundle.zip"

    asyncio.run(build_bundle_archive(session, "tenant-a", str(out_path)))

    with zipfile.ZipFile(out_path) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        items = json.loads(zf.read("items.json"))
        artifact_bytes = zf.read(f"artifacts/{item.id}.pdf")

    assert manifest["artifacts_dir"] == "artifacts"
    assert items[0]["metadata"] == {"doc_title": "Recovered brief"}
    assert items[0]["upload_artifact"] == {
        "source": "user_upload",
        "filename": "brief.pdf",
        "media_type": "application/pdf",
        "extension": ".pdf",
        "bundle_path": f"artifacts/{item.id}.pdf",
    }
    assert "storage_path" not in items[0]["upload_artifact"]
    assert artifact_bytes == b"%PDF-1.7 original bytes"


def test_materialize_bundle_upload_artifacts_persists_imported_bytes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("app.services.bundle.settings.upload_artifact_dir", str(tmp_path / "uploads"))
    artifact = {
        "source": "user_upload",
        "filename": "brief.pdf",
        "media_type": "application/pdf",
        "extension": ".pdf",
        "bundle_path": "artifacts/item-1.pdf",
    }
    bundle_bytes = _make_bundle(upload_artifact=artifact, artifact_bytes=b"%PDF original")

    payload = parse_bundle_archive(bundle_bytes)
    materialize_bundle_upload_artifacts(bundle_bytes, payload, tenant_id="tenant-a")

    assert payload.items[0].upload_artifact is not None
    storage_path = payload.items[0].upload_artifact.storage_path
    assert storage_path is not None
    assert Path(storage_path).read_bytes() == b"%PDF original"


@pytest.mark.parametrize(
    "bundle_path",
    [
        "../brief.pdf",
        "/artifacts/brief.pdf",
        "other/item-1.pdf",
        "artifacts/../brief.pdf",
        "artifacts\\brief.pdf",
    ],
)
def test_parse_bundle_archive_rejects_unsafe_bundle_path(
    tmp_path: Path,
    monkeypatch,
    bundle_path: str,
) -> None:
    monkeypatch.setattr("app.services.bundle.settings.upload_artifact_dir", str(tmp_path / "uploads"))
    artifact = {
        "source": "user_upload",
        "filename": "brief.pdf",
        "media_type": "application/pdf",
        "extension": ".pdf",
        "bundle_path": bundle_path,
    }
    bundle_bytes = _make_bundle(upload_artifact=artifact, artifact_bytes=b"%PDF original")

    with pytest.raises(BundleValidationError, match="Bundle upload artifact path is invalid"):
        parse_bundle_archive(bundle_bytes)


def test_materialize_bundle_upload_artifacts_rejects_unsafe_extension(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("app.services.bundle.settings.upload_artifact_dir", str(tmp_path / "uploads"))
    artifact = {
        "source": "user_upload",
        "filename": "brief.pdf",
        "media_type": "application/pdf",
        "extension": "./../brief",
        "bundle_path": "artifacts/item-1",
    }
    bundle_bytes = _make_bundle(upload_artifact=artifact, artifact_bytes=b"%PDF original")

    payload = parse_bundle_archive(bundle_bytes)
    with pytest.raises(BundleValidationError, match="Upload artifact extension"):
        materialize_bundle_upload_artifacts(bundle_bytes, payload, tenant_id="tenant-a")
