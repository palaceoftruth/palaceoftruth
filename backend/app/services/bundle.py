import hashlib
import io
import json
import logging
import os
import shutil
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence, TextIO

from pydantic import ValidationError
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.conversation import Conversation, ConversationMessage
from app.embedding_profile import is_default_embedding_profile, resolve_embedding_profile
from app.models.embedding import Embedding, EmbeddingProfileVector
from app.models.item import Item
from app.models.job import Job
from app.schemas.bundle import (
    AdminJobResponse,
    BUNDLE_VERSION,
    BundleConversationMessageRecord,
    BundleConversationRecord,
    BundleEmbeddingMetadata,
    BundleItemRecord,
    BundleManifest,
    BundlePayload,
    BundleSourceInstance,
    BundleUploadArtifactReference,
)
from app.services.chunker import chunk_text
from app.services.embedder import EmbeddingService
from app.services.embedding_storage import embedding_record_for_profile
from app.services.job_progress import job_event_status_for_job_status, record_job_progress_event

logger = logging.getLogger(__name__)

RESTORE_JOB_TYPE = "bundle_restore"
RESTORE_TERMINAL_STATUSES = {"ready", "failed", "cancelled"}
ARTIFACTS_DIR = "artifacts"


class BundleValidationError(ValueError):
    pass


def _json_dump(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _batch(values: Sequence[uuid.UUID], size: int) -> Iterable[Sequence[uuid.UUID]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _normalize_upload_artifact_extension(extension: str | None) -> str:
    if not extension:
        return ""
    if not extension.startswith("."):
        return ""
    if extension in {".", ".."} or "/" in extension or "\\" in extension:
        raise BundleValidationError("Upload artifact extension must be a simple file suffix")
    return extension


def artifact_storage_path(tenant_id: str, item_id: uuid.UUID, extension: str | None) -> Path:
    normalized_extension = _normalize_upload_artifact_extension(extension)
    tenant_dir = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()
    return Path(settings.upload_artifact_dir) / tenant_dir / f"{item_id}{normalized_extension}"


def persist_upload_artifact(
    source_path: str,
    *,
    tenant_id: str,
    item_id: uuid.UUID,
    extension: str | None,
) -> str:
    destination = artifact_storage_path(tenant_id, item_id, extension)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_path, destination)
    return str(destination)


async def build_bundle_archive(
    db: AsyncSession,
    tenant_id: str,
    out_path: str,
) -> None:
    items_payload, has_upload_artifacts = await _collect_bundle_items(db, tenant_id)
    manifest = BundleManifest(
        exported_at=datetime.now(timezone.utc),
        source_instance=BundleSourceInstance(tenant_id=tenant_id),
        embedding=BundleEmbeddingMetadata(source_model=settings.embedding_model),
        artifacts_dir=ARTIFACTS_DIR if has_upload_artifacts else None,
    )

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", _json_dump(manifest.model_dump(mode="json")))
        _write_items_json(zf, items_payload)
        _write_upload_artifacts(zf, items_payload)
        await _write_conversations_json(zf, db, tenant_id)


async def _collect_bundle_items(
    db: AsyncSession,
    tenant_id: str,
) -> tuple[list[BundleItemRecord], bool]:
    payload: list[BundleItemRecord] = []
    has_upload_artifacts = False
    offset = 0
    page_size = 250
    while True:
        rows = (
            (
                await db.execute(
                    select(Item)
                    .where(Item.tenant_id == tenant_id)
                    .where(Item.status == "ready")
                    .where(Item.deleted_at.is_(None))
                    .order_by(Item.created_at.asc())
                    .offset(offset)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            break
        for item in rows:
            item_metadata, upload_artifact = _extract_upload_artifact_reference(
                item.id,
                item.metadata_,
            )
            has_upload_artifacts = has_upload_artifacts or (
                upload_artifact is not None and upload_artifact.bundle_path is not None
            )
            payload.append(
                BundleItemRecord(
                    id=item.id,
                    source_type=item.source_type,
                    source_url=item.source_url,
                    title=item.title,
                    summary=item.summary,
                    raw_content=item.raw_content,
                    content_chunks=item.content_chunks,
                    metadata=item_metadata,
                    upload_artifact=upload_artifact,
                    tags=item.tags or [],
                    categories=item.categories or [],
                    content_hash=item.content_hash,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                )
            )
        offset += page_size
    return payload, has_upload_artifacts


def _write_items_json(zf: zipfile.ZipFile, items: Sequence[BundleItemRecord]) -> None:
    with zf.open("items.json", "w") as raw:
        text_stream = io.TextIOWrapper(raw, encoding="utf-8")
        text_stream.write("[")
        first = True
        for item in items:
            if not first:
                text_stream.write(",\n")
            first = False
            text_stream.write(_json_dump(_item_bundle_dump(item)))
        text_stream.write("]")
        text_stream.flush()


def _item_bundle_dump(item: BundleItemRecord) -> dict[str, object]:
    return item.model_dump(
        mode="json",
        exclude_none=True,
        exclude={"upload_artifact": {"storage_path"}},
    )


def _write_upload_artifacts(zf: zipfile.ZipFile, items: Sequence[BundleItemRecord]) -> None:
    for item in items:
        if item.upload_artifact is None:
            continue
        if not item.upload_artifact.bundle_path or not item.upload_artifact.storage_path:
            continue
        try:
            zf.write(item.upload_artifact.storage_path, item.upload_artifact.bundle_path)
        except OSError as exc:
            logger.warning("Skipping missing upload artifact for bundle item %s: %s", item.id, exc)


def _extract_upload_artifact_reference(
    item_id: uuid.UUID,
    metadata: dict[str, object] | None,
) -> tuple[dict[str, object], BundleUploadArtifactReference | None]:
    item_metadata = dict(metadata or {})
    raw_upload_artifact = item_metadata.pop("upload_artifact", None)
    if not isinstance(raw_upload_artifact, dict):
        if raw_upload_artifact is not None:
            item_metadata["upload_artifact"] = raw_upload_artifact
        return item_metadata, None

    filename = raw_upload_artifact.get("filename")
    if not isinstance(filename, str) or not filename:
        item_metadata["upload_artifact"] = raw_upload_artifact
        return item_metadata, None

    extension = raw_upload_artifact.get("extension")
    try:
        normalized_extension = (
            _normalize_upload_artifact_extension(extension)
            if isinstance(extension, str)
            else ""
        ) or None
    except BundleValidationError:
        item_metadata["upload_artifact"] = raw_upload_artifact
        return item_metadata, None
    media_type = raw_upload_artifact.get("media_type")
    source = raw_upload_artifact.get("source")
    storage_path = raw_upload_artifact.get("storage_path")
    storage_path = storage_path if isinstance(storage_path, str) and storage_path else None
    bundle_path = (
        f"{ARTIFACTS_DIR}/{item_id}{normalized_extension or ''}"
        if storage_path and os.path.isfile(storage_path)
        else None
    )
    return item_metadata, BundleUploadArtifactReference(
        source=source if isinstance(source, str) and source else "user_upload",
        filename=filename,
        media_type=media_type if isinstance(media_type, str) and media_type else None,
        extension=normalized_extension,
        bundle_path=bundle_path,
        storage_path=storage_path,
    )


def _restore_upload_artifact_metadata(item: BundleItemRecord) -> dict[str, object]:
    metadata = dict(item.metadata or {})
    if item.upload_artifact is None:
        return metadata

    # Preserve user-facing upload provenance on restored items without persisting
    # bundle-internal archive paths in generic item metadata.
    metadata["upload_artifact"] = item.upload_artifact.model_dump(
        exclude_none=True,
        exclude={"bundle_path"},
    )
    return metadata


async def _write_conversations_json(zf: zipfile.ZipFile, db: AsyncSession, tenant_id: str) -> None:
    conversations = (
        (
            await db.execute(
                select(Conversation)
                .where(Conversation.tenant_id == tenant_id)
                .order_by(Conversation.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    with zf.open("conversations.json", "w") as raw:
        text_stream = io.TextIOWrapper(raw, encoding="utf-8")
        text_stream.write("[")
        first = True
        for conversation in conversations:
            messages = (
                (
                    await db.execute(
                        select(ConversationMessage)
                        .where(ConversationMessage.tenant_id == tenant_id)
                        .where(ConversationMessage.conversation_id == conversation.id)
                        .order_by(ConversationMessage.created_at.asc())
                    )
                )
                .scalars()
                .all()
            )
            if not first:
                text_stream.write(",\n")
            first = False
            text_stream.write(
                _json_dump(
                    BundleConversationRecord(
                        id=conversation.id,
                        title=conversation.title,
                        created_at=conversation.created_at,
                        updated_at=conversation.updated_at,
                        messages=[
                            BundleConversationMessageRecord(
                                id=message.id,
                                role=message.role,
                                content=message.content,
                                created_at=message.created_at,
                            )
                            for message in messages
                        ],
                    ).model_dump(mode="json")
                )
            )
        text_stream.write("]")
        text_stream.flush()


def parse_bundle_archive(bundle_bytes: bytes) -> BundlePayload:
    try:
        with zipfile.ZipFile(io.BytesIO(bundle_bytes)) as zf:
            manifest = _load_json_file(zf, "manifest.json")
            items = _load_json_file(zf, manifest.get("items_file", "items.json"))
            conversations = _load_json_file(
                zf,
                manifest.get("conversations_file", "conversations.json"),
            )
            archive_names = set(zf.namelist())
    except zipfile.BadZipFile as exc:
        raise BundleValidationError("Bundle is not a valid zip archive") from exc
    except KeyError as exc:
        raise BundleValidationError(f"Bundle is missing required file: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise BundleValidationError(f"Bundle JSON is invalid: {exc.msg}") from exc

    try:
        payload = BundlePayload(
            manifest=BundleManifest.model_validate(manifest),
            items=[BundleItemRecord.model_validate(item) for item in items],
            conversations=[
                BundleConversationRecord.model_validate(conversation)
                for conversation in conversations
            ],
        )
    except ValidationError as exc:
        raise BundleValidationError(f"Bundle payload is invalid: {exc.errors()[0]['msg']}") from exc
    if payload.manifest.bundle_version != BUNDLE_VERSION:
        raise BundleValidationError(
            f"Unsupported bundle_version={payload.manifest.bundle_version}; "
            f"expected {BUNDLE_VERSION}"
        )
    _validate_bundle_upload_artifact_paths(payload, archive_names)
    for item in payload.items:
        if item.upload_artifact is not None:
            item.upload_artifact.storage_path = None
    return payload


def _validate_bundle_upload_artifact_paths(payload: BundlePayload, archive_names: set[str]) -> None:
    artifacts_dir = payload.manifest.artifacts_dir or ARTIFACTS_DIR
    artifacts_prefix = f"{artifacts_dir.rstrip('/')}/"
    for item in payload.items:
        if item.upload_artifact is None or not item.upload_artifact.bundle_path:
            continue
        bundle_path = item.upload_artifact.bundle_path
        path_parts = Path(bundle_path).parts
        if (
            bundle_path.startswith("/")
            or not bundle_path.startswith(artifacts_prefix)
            or any(part in {"", ".", ".."} for part in path_parts)
        ):
            raise BundleValidationError(f"Bundle upload artifact path is invalid: {bundle_path}")
        if bundle_path not in archive_names:
            raise BundleValidationError(f"Bundle is missing upload artifact: {bundle_path}")


def materialize_bundle_upload_artifacts(
    bundle_bytes: bytes,
    payload: BundlePayload,
    *,
    tenant_id: str,
) -> BundlePayload:
    try:
        with zipfile.ZipFile(io.BytesIO(bundle_bytes)) as zf:
            for item in payload.items:
                if item.upload_artifact is None or not item.upload_artifact.bundle_path:
                    continue
                _validate_upload_artifact_bundle_path(item.upload_artifact.bundle_path)
                destination = artifact_storage_path(
                    tenant_id,
                    item.id,
                    item.upload_artifact.extension,
                )
                try:
                    with zf.open(item.upload_artifact.bundle_path) as source:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with destination.open("wb") as target:
                            shutil.copyfileobj(source, target)
                except KeyError as exc:
                    raise BundleValidationError(
                        f"Bundle is missing upload artifact: {item.upload_artifact.bundle_path}"
                    ) from exc
                item.upload_artifact.storage_path = str(destination)
    except zipfile.BadZipFile as exc:
        raise BundleValidationError("Bundle is not a valid zip archive") from exc
    except OSError as exc:
        raise BundleValidationError(f"Could not persist upload artifact: {exc}") from exc
    return payload


def _validate_upload_artifact_bundle_path(bundle_path: str) -> None:
    if "\\" in bundle_path:
        raise BundleValidationError("Upload artifact bundle_path must use POSIX separators")
    path = PurePosixPath(bundle_path)
    if (
        path.is_absolute()
        or len(path.parts) < 2
        or path.parts[0] != ARTIFACTS_DIR
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise BundleValidationError("Upload artifact bundle_path must stay under artifacts/")


def _load_json_file(zf: zipfile.ZipFile, filename: str) -> object:
    with zf.open(filename) as handle:
        text_stream: TextIO = io.TextIOWrapper(handle, encoding="utf-8")
        return json.load(text_stream)


async def tenant_has_state(
    db: AsyncSession,
    tenant_id: str,
    *,
    include_api_keys: bool = True,
) -> bool:
    checks = [
        text("SELECT 1 FROM items WHERE tenant_id = :tenant_id LIMIT 1"),
        text("SELECT 1 FROM conversations WHERE tenant_id = :tenant_id LIMIT 1"),
        text("SELECT 1 FROM conversation_messages WHERE tenant_id = :tenant_id LIMIT 1"),
    ]
    # Bundle restore safely preserves control-plane credentials, so callers can
    # decide whether existing API keys should count as tenant state.
    if include_api_keys:
        checks.append(text("SELECT 1 FROM api_keys WHERE tenant_id = :tenant_id LIMIT 1"))
    for query in checks:
        if await db.scalar(query, {"tenant_id": tenant_id}):
            return True
    return False


async def create_restore_job(
    db: AsyncSession,
    tenant_id: str,
    payload: BundlePayload,
) -> Job:
    job = Job(
        job_type=RESTORE_JOB_TYPE,
        status="validated",
        progress=5,
        tenant_id=tenant_id,
        payload=payload.model_dump(mode="json"),
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job


def serialize_admin_job(job: Job) -> AdminJobResponse:
    return AdminJobResponse.model_validate({**job.__dict__, "payload": None})


async def reset_tenant_restore_state(
    db: AsyncSession,
    tenant_id: str,
    *,
    preserve_job_id: uuid.UUID | None = None,
) -> None:
    item_ids = (
        (
            await db.execute(
                select(Item.id).where(Item.tenant_id == tenant_id)
            )
        )
        .scalars()
        .all()
    )
    if item_ids:
        for batch in _batch(item_ids, 500):
            await db.execute(delete(Embedding).where(Embedding.item_id.in_(batch)))
    await db.execute(
        delete(ConversationMessage).where(ConversationMessage.tenant_id == tenant_id)
    )
    await db.execute(delete(Conversation).where(Conversation.tenant_id == tenant_id))
    await db.execute(delete(Item).where(Item.tenant_id == tenant_id))
    if preserve_job_id is None:
        await db.execute(
            delete(Job)
            .where(Job.tenant_id == tenant_id)
            .where(Job.job_type == RESTORE_JOB_TYPE)
        )
    else:
        await db.execute(
            delete(Job)
            .where(Job.tenant_id == tenant_id)
            .where(Job.job_type == RESTORE_JOB_TYPE)
            .where(Job.id != preserve_job_id)
        )
    await db.commit()


async def run_restore_job(
    db: AsyncSession,
    embedder: EmbeddingService,
    job_id: uuid.UUID,
) -> None:
    job = await db.get(Job, job_id)
    if not job:
        raise ValueError(f"Job {job_id} not found")
    if job.job_type != RESTORE_JOB_TYPE:
        raise ValueError(f"Job {job_id} is not a restore job")
    if not job.payload:
        raise BundleValidationError("Restore job payload missing")

    payload = BundlePayload.model_validate(job.payload)
    tenant_id = job.tenant_id

    try:
        await reset_tenant_restore_state(db, tenant_id, preserve_job_id=job.id)
        await _update_job(db, job, status="importing", progress=15)

        for conversation in payload.conversations:
            db.add(
                Conversation(
                    id=conversation.id,
                    title=conversation.title,
                    tenant_id=tenant_id,
                    created_at=conversation.created_at,
                    updated_at=conversation.updated_at,
                )
            )
            for message in conversation.messages:
                db.add(
                    ConversationMessage(
                        id=message.id,
                        conversation_id=conversation.id,
                        tenant_id=tenant_id,
                        role=message.role,
                        content=message.content,
                        created_at=message.created_at,
                    )
                )

        for item in payload.items:
            db.add(
                Item(
                    id=item.id,
                    source_type=item.source_type,
                    source_url=item.source_url,
                    title=item.title,
                    summary=item.summary,
                    raw_content=item.raw_content,
                    content_chunks=item.content_chunks,
                    metadata_=_restore_upload_artifact_metadata(item),
                    tags=item.tags,
                    categories=item.categories,
                    content_hash=item.content_hash,
                    tenant_id=tenant_id,
                    status="processing" if item.raw_content else "ready",
                    created_at=item.created_at,
                    updated_at=item.updated_at or item.created_at,
                )
            )

        await db.commit()
        await _update_job(db, job, status="imported_structural_data", progress=45)

        total_items = max(len(payload.items), 1)
        for index, item in enumerate(payload.items, start=1):
            if not item.raw_content:
                continue
            restored_item = await db.get(Item, item.id)
            if not restored_item:
                raise ValueError(f"Restored item {item.id} not found during re-embedding")
            chunks = item.content_chunks or chunk_text(item.raw_content)
            restored_item.content_chunks = chunks
            vectors = await embedder.embed_texts([chunk["text"] for chunk in chunks]) if chunks else []
            embedding_profile = getattr(embedder, "profile", resolve_embedding_profile())
            if not is_default_embedding_profile(embedding_profile):
                await db.execute(
                    delete(EmbeddingProfileVector)
                    .where(EmbeddingProfileVector.item_id == restored_item.id)
                    .where(EmbeddingProfileVector.profile_name == embedding_profile.profile_name)
                )
            for chunk_index, (chunk, vector) in enumerate(zip(chunks, vectors)):
                db.add(
                    embedding_record_for_profile(
                        item_id=restored_item.id,
                        chunk_index=chunk.get("index", chunk_index),
                        chunk_text=chunk["text"],
                        vector=vector,
                        profile=embedding_profile,
                    )
                )
            restored_item.status = "ready"
            await db.commit()
            progress = 45 + int((index / total_items) * 50)
            await _update_job(db, job, status="reembedding", progress=min(progress, 95))

        await _update_job(
            db,
            job,
            status="ready",
            progress=100,
            completed_at=datetime.now(timezone.utc),
        )
    except Exception as exc:
        logger.exception("Restore job %s failed: %s", job_id, exc)
        await _update_job(
            db,
            job,
            status="failed",
            error_message=str(exc)[:500],
            completed_at=datetime.now(timezone.utc),
        )
        raise


async def retry_restore_job(
    db: AsyncSession,
    job: Job,
) -> Job:
    if job.job_type != RESTORE_JOB_TYPE:
        raise ValueError("retry_restore_job only supports bundle restore jobs")
    job.status = "validated"
    job.progress = 5
    job.error_message = None
    job.completed_at = None
    await db.commit()
    await db.refresh(job)
    return job


async def _update_job(
    db: AsyncSession,
    job: Job,
    *,
    status: str | None = None,
    progress: int | None = None,
    error_message: str | None = None,
    completed_at: datetime | None = None,
) -> None:
    if status is not None:
        job.status = status
    if progress is not None:
        job.progress = progress
    if error_message is not None:
        job.error_message = error_message
    if completed_at is not None:
        job.completed_at = completed_at
    await record_job_progress_event(
        db,
        job=job,
        phase=status or "progress",
        status=job_event_status_for_job_status(status),
        progress=job.progress if progress is None else progress,
        message=error_message,
        metadata={"error_class": "RestoreJobError"} if error_message else None,
    )
    await db.commit()
    await db.refresh(job)
