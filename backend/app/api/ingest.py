import asyncio
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, File, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import record_oauth_client_audit_event, verify_api_key, verify_capture_write_auth
from app.database import get_db
from app.models.item import Item
from app.models.job import Job
from app.services.bundle import persist_upload_artifact
from app.services.image_analysis import build_image_analysis_metadata, image_bytes_hash
from app.services.item_dates import apply_effective_date
from app.utils.hash import compute_content_hash
from app.utils.job_payloads import build_retry_payload
from app.utils.webhook import maybe_dispatch_webhook, validate_webhook_url
from app.workers.queues import enqueue_worker_job

from app.schemas.ingest import (
    IngestMediaRequest,
    IngestWebpageRequest,
    IngestNoteRequest,
    IngestResponse,
    BatchIngestRequest,
    BatchIngestResponse,
    BatchIngestResult,
    BatchIngestItem,
)

_DOC_EXTRACTION_TIMEOUT = 110.0  # seconds — just under the 120s proxy timeout

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ingest", tags=["ingest"])

_ALLOWED_DOC_EXTS = frozenset({".pdf", ".docx", ".xlsx", ".md", ".txt"})
_DOC_SIZE_LIMIT = 50 * 1024 * 1024  # 50 MB

_ALLOWED_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp"})
_IMAGE_SIZE_LIMIT = 20 * 1024 * 1024  # 20 MB

_IMAGE_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

_BATCH_TYPE_MAP = {
    "youtube": ("media", "process_media"),
    "media": ("media", "process_media"),
    "webpage": ("webpage", "process_webpage"),
    "note": ("note", "process_note"),
}


async def _create_item_and_job(
    db: AsyncSession,
    source_type: str,
    title: str,
    tenant_id: str,
    source_url: str | None = None,
    webhook_url: str | None = None,
    signing_key: str | None = None,
    payload: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    tags: list[str] | None = None,
) -> tuple[Item, Job]:
    """Create an item (status=processing) and job (status=queued) in the DB.

    If a failed item with the same source_url already exists, it is deleted
    before the new item is created so the user doesn't have to do it manually.
    """
    if source_url:
        existing = await db.execute(
            select(Item).where(Item.source_url == source_url).where(Item.tenant_id == tenant_id)
        )
        for existing_item in existing.scalars().all():
            if existing_item.status == "deleted" or existing_item.deleted_at is not None:
                continue
            if existing_item.status == "failed":
                # Clean up failed attempt so re-ingestion works automatically
                await db.delete(existing_item)
            else:
                # Already ingested (ready/processing) — raise a clear 409
                raise HTTPException(
                    status_code=409,
                    detail=f"URL already ingested (item {existing_item.id}, status: {existing_item.status})",
                )
        await db.flush()

    item = Item(
        source_type=source_type,
        source_url=source_url,
        title=title,
        status="processing",
        metadata_=metadata or {},
        tags=tags or [],
        tenant_id=tenant_id,
    )
    apply_effective_date(item, metadata=metadata or {})
    db.add(item)
    await db.flush()  # populate item.id

    job = Job(
        item_id=item.id,
        job_type=source_type,
        status="queued",
        progress=0,
        tenant_id=tenant_id,
        webhook_url=webhook_url,
        signing_key=signing_key,
        payload=payload,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return item, job


def _build_upload_provenance(
    *,
    filename: str,
    media_type: str | None,
    extension: str | None,
    storage_path: str | None = None,
) -> dict[str, Any]:
    # Keep upload provenance under one stable key so future bundle exporters can
    # reference original filenames/media types without scraping derived metadata.
    return {
        "upload_artifact": {
            "source": "user_upload",
            "filename": filename,
            "media_type": media_type,
            "extension": extension,
            **({"storage_path": storage_path} if storage_path else {}),
        }
    }


async def _stream_to_tmp(file: UploadFile, suffix: str, size_limit: int) -> str:
    """Stream an UploadFile to /tmp/palaceoftruth in 1 MB chunks.

    Raises HTTPException 413 if the file exceeds size_limit bytes.
    Returns the path of the written temp file.
    """
    os.makedirs("/tmp/palaceoftruth", exist_ok=True)
    with tempfile.NamedTemporaryFile(dir="/tmp/palaceoftruth", suffix=suffix, delete=False) as tmp:
        tmp_path = tmp.name
        total = 0
        while chunk := await file.read(1024 * 1024):
            total += len(chunk)
            if total > size_limit:
                tmp.close()
                os.unlink(tmp_path)
                raise HTTPException(
                    status_code=413,
                    detail=f"File too large (limit: {size_limit // (1024 * 1024)} MB)",
                )
            tmp.write(chunk)
    return tmp_path


async def _mark_enqueue_failed(
    *,
    request: Request,
    db: AsyncSession,
    job: Job,
    item: Item,
    exc: Exception,
) -> None:
    """Persist truthful failure state when the worker job could not be queued."""
    job.status = "failed"
    job.progress = 0
    job.error_message = f"Failed to enqueue ingest task: {exc}"
    job.completed_at = datetime.now(timezone.utc)
    item.status = "failed"
    await db.commit()

    if job.webhook_url:
        try:
            await maybe_dispatch_webhook(request.app.state.arq_pool, str(job.id))
        except Exception:
            logger.exception("webhook dispatch failed after enqueue failure for job %s", job.id)


async def _enqueue_ingest_job(
    *,
    request: Request,
    db: AsyncSession,
    job: Job,
    item: Item,
    task_name: str,
    task_kwargs: dict[str, Any],
) -> bool:
    try:
        await enqueue_worker_job(request.app.state.arq_pool, task_name, job_id=str(job.id), **task_kwargs)
    except Exception as exc:
        logger.exception("failed to enqueue ingest job %s (%s)", job.id, task_name)
        try:
            await _mark_enqueue_failed(request=request, db=db, job=job, item=item, exc=exc)
        except Exception:
            logger.exception("failed to persist enqueue failure state for ingest job %s", job.id)
            raise HTTPException(
                status_code=503,
                detail="Ingest enqueue failed and failure state could not be persisted",
            ) from exc
        return False
    return True


async def _record_extension_capture_audit(
    *,
    request: Request,
    route: str,
    job: Job,
    item: Item,
) -> None:
    await record_oauth_client_audit_event(
        request,
        operation="browser_extension.capture",
        required_scope="capture:write",
        status="success",
        params_summary={
            "route": route,
            "job_id": str(job.id),
            "item_id": str(item.id),
        },
        app_version=request.headers.get("X-Palace-Extension-Version"),
    )


async def _attach_persisted_upload_artifact(
    *,
    db: AsyncSession,
    item: Item,
    job: Job,
    tmp_path: str,
    tenant_id: str,
    filename: str,
    media_type: str | None,
    extension: str | None,
) -> str:
    try:
        storage_path = persist_upload_artifact(
            tmp_path,
            tenant_id=tenant_id,
            item_id=item.id,
            extension=extension,
        )
    except OSError as exc:
        item.status = "failed"
        job.status = "failed"
        job.error_message = f"Failed to persist upload artifact: {exc}"
        job.completed_at = datetime.now(timezone.utc)
        await db.commit()
        logger.exception("failed to persist upload artifact for item %s", item.id)
        raise HTTPException(status_code=500, detail="Failed to persist upload artifact") from exc

    item.metadata_ = {
        **(item.metadata_ or {}),
        **_build_upload_provenance(
            filename=filename,
            media_type=media_type,
            extension=extension,
            storage_path=storage_path,
        ),
    }
    await db.commit()
    return storage_path


@router.post("/media", response_model=IngestResponse, status_code=202, dependencies=[Depends(verify_capture_write_auth)])
async def ingest_media(
    request_body: IngestMediaRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Ingest any audio/video URL supported by yt-dlp (YouTube, podcasts, Vimeo, etc.)."""
    if not request_body.url.startswith("http"):
        raise HTTPException(status_code=422, detail="Invalid URL")

    webhook_url = validate_webhook_url(request_body.webhook_url) if request_body.webhook_url else None
    retry_payload = build_retry_payload(
        task_name="process_media",
        task_kwargs={
            "url": request_body.url,
            "model": request_body.model,
        },
    )
    item, job = await _create_item_and_job(
        db, "media", title=request_body.url, source_url=request_body.url,
        tenant_id=request.state.tenant_id,
        webhook_url=webhook_url, signing_key=request.state.key_hash if webhook_url else None,
        payload=retry_payload,
    )
    enqueued = await _enqueue_ingest_job(
        request=request,
        db=db,
        job=job,
        item=item,
        task_name="process_media",
        task_kwargs={
            "url": request_body.url,
            "tenant_id": request.state.tenant_id,
            "model": request_body.model,
        },
    )
    if not enqueued:
        raise HTTPException(status_code=503, detail="Ingest enqueue failed; job marked failed for retry")
    await _record_extension_capture_audit(request=request, route="media", job=job, item=item)
    return IngestResponse(job_id=job.id, status="queued")


@router.post(
    "/youtube",
    response_model=IngestResponse,
    status_code=202,
    include_in_schema=False,
    dependencies=[Depends(verify_capture_write_auth)],
)
async def ingest_youtube(
    request_body: IngestMediaRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Deprecated alias for /media."""
    return await ingest_media(request_body, request, db)


@router.post("/webpage", response_model=IngestResponse, status_code=202, dependencies=[Depends(verify_capture_write_auth)])
async def ingest_webpage(
    request_body: IngestWebpageRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    if not request_body.url.startswith("http"):
        raise HTTPException(status_code=422, detail="Invalid URL")

    webhook_url = validate_webhook_url(request_body.webhook_url) if request_body.webhook_url else None
    retry_payload = build_retry_payload(
        task_name="process_webpage",
        task_kwargs={
            "url": request_body.url,
            "model": request_body.model,
        },
    )
    item, job = await _create_item_and_job(
        db, "webpage", title=request_body.url, source_url=request_body.url,
        tenant_id=request.state.tenant_id,
        webhook_url=webhook_url, signing_key=request.state.key_hash if webhook_url else None,
        payload=retry_payload,
    )
    enqueued = await _enqueue_ingest_job(
        request=request,
        db=db,
        job=job,
        item=item,
        task_name="process_webpage",
        task_kwargs={
            "url": request_body.url,
            "tenant_id": request.state.tenant_id,
            "model": request_body.model,
        },
    )
    if not enqueued:
        raise HTTPException(status_code=503, detail="Ingest enqueue failed; job marked failed for retry")
    await _record_extension_capture_audit(request=request, route="webpage", job=job, item=item)
    return IngestResponse(job_id=job.id, status="queued")


def _extract_doc_from_path(path: str, filename: str) -> tuple[str, dict[str, Any]]:
    """Extract text and metadata from a document file synchronously.

    Uses pypdf for PDFs (fast), and dedicated extractors for other formats.
    Returns (extracted_text, metadata_dict).
    """
    ext = os.path.splitext(filename.lower())[1]

    if ext == ".pdf":
        import fitz  # pymupdf
        doc = fitz.open(path)
        meta: dict[str, Any] = {
            "page_count": len(doc),
            "file_size_bytes": os.path.getsize(path),
        }
        info = doc.metadata or {}
        if info.get("title"):
            meta["doc_title"] = info["title"]
        if info.get("author"):
            meta["doc_author"] = info["author"]
            meta["author"] = info["author"]
        pages = [doc[i].get_text() for i in range(len(doc))]
        doc.close()
        extracted = "\n\n".join(p for p in pages if p.strip())
        meta["word_count"] = len(extracted.split())
        return extracted, meta

    elif ext == ".docx":
        from app.utils.doc_extract import extract_docx
        return extract_docx(path)

    elif ext == ".xlsx":
        from app.utils.doc_extract import extract_xlsx
        return extract_xlsx(path)

    elif ext in (".md", ".txt"):
        from app.utils.doc_extract import extract_text_file
        return extract_text_file(path)

    raise ValueError(f"Unsupported file extension: {ext}")



@router.post("/doc", response_model=IngestResponse, status_code=202, dependencies=[Depends(verify_api_key)])
async def ingest_doc(
    request: Request,
    db: AsyncSession = Depends(get_db),
    file: UploadFile = File(...),
    webhook_url: str | None = Form(None),
    model: str | None = Form(None),
):
    """Upload a document (.pdf, .docx, .xlsx, .md, .txt) for text extraction and ingestion."""
    filename = file.filename or ""
    ext = os.path.splitext(filename.lower())[1]
    if ext not in _ALLOWED_DOC_EXTS:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported file type. Allowed: {', '.join(sorted(_ALLOWED_DOC_EXTS))}",
        )

    validated_webhook_url = validate_webhook_url(webhook_url) if webhook_url else None
    tmp_path = await _stream_to_tmp(file, suffix=ext, size_limit=_DOC_SIZE_LIMIT)

    try:
        # Extract text inline (before creating the job) so there are no orphaned jobs
        # if extraction fails or times out.
        loop = asyncio.get_event_loop()
        try:
            extracted_text, doc_metadata = await asyncio.wait_for(
                loop.run_in_executor(None, _extract_doc_from_path, tmp_path, filename),
                timeout=_DOC_EXTRACTION_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=504,
                detail=f"Document extraction timed out after {int(_DOC_EXTRACTION_TIMEOUT)}s. "
                       "Try a smaller or less complex file.",
            )

        if not extracted_text.strip():
            raise HTTPException(status_code=422, detail="No text could be extracted from the document")

        # Dedup: content hash scoped to tenant
        content_hash = compute_content_hash(extracted_text)
        existing_id = await db.scalar(
            select(Item.id)
            .where(Item.content_hash == content_hash)
            .where(Item.tenant_id == request.state.tenant_id)
            .where(Item.status != "failed")
            .where(Item.status != "deleted")
            .where(Item.deleted_at.is_(None))
            .limit(1)
        )
        if existing_id:
            raise HTTPException(
                status_code=409,
                detail=f"Duplicate document — already ingested as item {existing_id}",
            )

        # Use embedded doc title if present, fall back to filename
        title = doc_metadata.get("doc_title") or filename or "Uploaded document"
        cleaned_model = model if model and model.strip() else None
        retry_payload = build_retry_payload(
            task_name="process_doc",
            task_kwargs={
                "extracted_text": extracted_text,
                "doc_metadata": doc_metadata,
                "model": cleaned_model,
            },
        )
        item, job = await _create_item_and_job(
            db, "doc", title=title, tenant_id=request.state.tenant_id,
            webhook_url=validated_webhook_url,
            signing_key=request.state.key_hash if validated_webhook_url else None,
            payload=retry_payload,
            metadata=_build_upload_provenance(
                filename=filename,
                media_type=file.content_type,
                extension=ext or None,
            ),
        )
        await _attach_persisted_upload_artifact(
            db=db,
            item=item,
            job=job,
            tmp_path=tmp_path,
            tenant_id=request.state.tenant_id,
            filename=filename,
            media_type=file.content_type,
            extension=ext or None,
        )

        enqueued = await _enqueue_ingest_job(
            request=request,
            db=db,
            job=job,
            item=item,
            task_name="process_doc",
            task_kwargs={
                "extracted_text": extracted_text,
                "doc_metadata": doc_metadata,
                "tenant_id": request.state.tenant_id,
                "model": cleaned_model,
            },
        )
        if not enqueued:
            raise HTTPException(status_code=503, detail="Ingest enqueue failed; job marked failed for retry")
        return IngestResponse(job_id=job.id, status="queued")
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


@router.post("/pdf", response_model=IngestResponse, status_code=202, include_in_schema=False, dependencies=[Depends(verify_api_key)])
async def ingest_pdf(
    request: Request,
    db: AsyncSession = Depends(get_db),
    file: UploadFile = File(...),
    webhook_url: str | None = Form(None),
):
    """Deprecated alias for /doc — accepts PDFs only for backward compatibility."""
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=422, detail="File must be a PDF")
    return await ingest_doc(request, db, file, webhook_url)


@router.post("/image", response_model=IngestResponse, status_code=202, dependencies=[Depends(verify_api_key)])
async def ingest_image(
    request: Request,
    db: AsyncSession = Depends(get_db),
    file: UploadFile = File(...),
    webhook_url: str | None = Form(None),
):
    """Upload an image (.jpg, .jpeg, .png, .gif, .webp) for vision analysis and ingestion."""
    filename = file.filename or ""
    ext = os.path.splitext(filename.lower())[1]
    if ext not in _ALLOWED_IMAGE_EXTS:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported image type. Allowed: {', '.join(sorted(_ALLOWED_IMAGE_EXTS))}",
        )

    validated_webhook_url = validate_webhook_url(webhook_url) if webhook_url else None
    media_type = _IMAGE_MEDIA_TYPES.get(ext, "image/jpeg")
    tmp_path = await _stream_to_tmp(file, suffix=ext, size_limit=_IMAGE_SIZE_LIMIT)

    try:
        with open(tmp_path, "rb") as f:
            image_bytes = f.read()

        # Dedup by raw byte hash scoped to source_type="image".
        byte_hash = image_bytes_hash(image_bytes)
        existing_id = await db.scalar(
            select(Item.id)
            .where(Item.content_hash == byte_hash)
            .where(Item.source_type == "image")
            .where(Item.tenant_id == request.state.tenant_id)
            .where(Item.status != "failed")
            .where(Item.status != "deleted")
            .where(Item.deleted_at.is_(None))
            .limit(1)
        )
        if existing_id:
            raise HTTPException(
                status_code=409,
                detail=f"Duplicate image — already ingested as item {existing_id}",
            )

        image_metadata = {
            "filename": filename,
            "media_type": media_type,
            **build_image_analysis_metadata(
                description=None,
                filename=filename,
                media_type=media_type,
                extension=ext or None,
                image_bytes=image_bytes,
                byte_hash=byte_hash,
                status="queued",
            ),
        }
        retry_payload = build_retry_payload(
            task_name="process_image",
            task_kwargs={
                "image_metadata": image_metadata,
            },
        )
        item, job = await _create_item_and_job(
            db, "image", title=filename or "Uploaded image", tenant_id=request.state.tenant_id,
            webhook_url=validated_webhook_url,
            signing_key=request.state.key_hash if validated_webhook_url else None,
            payload=retry_payload,
            metadata={
                **_build_upload_provenance(
                    filename=filename,
                    media_type=media_type,
                    extension=ext or None,
                ),
                **image_metadata,
            },
        )
        storage_path = await _attach_persisted_upload_artifact(
            db=db,
            item=item,
            job=job,
            tmp_path=tmp_path,
            tenant_id=request.state.tenant_id,
            filename=filename,
            media_type=media_type,
            extension=ext or None,
        )
        image_metadata = {
            **image_metadata,
            **build_image_analysis_metadata(
                description=None,
                filename=filename,
                media_type=media_type,
                extension=ext or None,
                image_bytes=image_bytes,
                byte_hash=byte_hash,
                artifact_storage_path=storage_path,
                status="queued",
            ),
        }
        item.metadata_ = {**(item.metadata_ or {}), **image_metadata}
        job.payload = build_retry_payload(
            task_name="process_image",
            task_kwargs={
                "image_metadata": image_metadata,
            },
        )

        # Store byte hash for future dedup
        item.content_hash = byte_hash
        await db.commit()

        enqueued = await _enqueue_ingest_job(
            request=request,
            db=db,
            job=job,
            item=item,
            task_name="process_image",
            task_kwargs={
                "image_metadata": image_metadata,
                "tenant_id": request.state.tenant_id,
            },
        )
        if not enqueued:
            raise HTTPException(status_code=503, detail="Ingest enqueue failed; job marked failed for retry")
        return IngestResponse(job_id=job.id, status="queued")
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


@router.post("/note", response_model=IngestResponse, status_code=202, dependencies=[Depends(verify_capture_write_auth)])
async def ingest_note(
    request_body: IngestNoteRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    webhook_url = validate_webhook_url(request_body.webhook_url) if request_body.webhook_url else None
    retry_payload = build_retry_payload(
        task_name="process_note",
        task_kwargs={
            "title": request_body.title,
            "content": request_body.content,
            "tags": request_body.tags or None,
            "model": request_body.model,
        },
    )
    item, job = await _create_item_and_job(
        db, "note", title=request_body.title, tenant_id=request.state.tenant_id,
        webhook_url=webhook_url, signing_key=request.state.key_hash if webhook_url else None,
        payload=retry_payload,
    )
    enqueued = await _enqueue_ingest_job(
        request=request,
        db=db,
        job=job,
        item=item,
        task_name="process_note",
        task_kwargs={
            "title": request_body.title,
            "content": request_body.content,
            "tags": request_body.tags or None,
            "tenant_id": request.state.tenant_id,
            "model": request_body.model,
        },
    )
    if not enqueued:
        raise HTTPException(status_code=503, detail="Ingest enqueue failed; job marked failed for retry")
    await _record_extension_capture_audit(request=request, route="note", job=job, item=item)
    return IngestResponse(job_id=job.id, status="queued")


def _build_batch_task_payload(
    entry: BatchIngestItem,
    *,
    tenant_id: str,
) -> tuple[str, str, str, dict[str, Any]]:
    source_type, task_name = _BATCH_TYPE_MAP[entry.type]
    title = entry.title or entry.url or "Untitled"
    task_kwargs: dict[str, Any] = {
        "tenant_id": tenant_id,
        "model": entry.model,
    }
    if entry.type in ("youtube", "media", "webpage"):
        if not entry.url:
            raise HTTPException(status_code=422, detail=f"url required for type {entry.type}")
        if not entry.url.startswith("http"):
            raise HTTPException(status_code=422, detail="Invalid URL")
        task_kwargs["url"] = entry.url
        return source_type, task_name, title, task_kwargs

    if not entry.content:
        raise HTTPException(status_code=422, detail="content required for note type")

    task_kwargs["title"] = title
    task_kwargs["content"] = entry.content
    return source_type, task_name, title, task_kwargs


@router.post("/batch", response_model=BatchIngestResponse, status_code=202, dependencies=[Depends(verify_api_key)])
async def ingest_batch(
    body: BatchIngestRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    webhook_url = validate_webhook_url(body.webhook_url) if body.webhook_url else None
    signing_key = request.state.key_hash if webhook_url else None
    results = []
    prepared_entries: list[tuple[BatchIngestItem, str, str, str, dict[str, Any]]] = []
    for entry in body.items:
        # Validate every entry before creating any rows so an invalid payload cannot leave partial side effects.
        prepared_entries.append(
            (entry, *_build_batch_task_payload(entry, tenant_id=request.state.tenant_id))
        )
    for entry, source_type, task_name, title, task_kwargs in prepared_entries:
        item, job = await _create_item_and_job(
            db, source_type, title=title, source_url=entry.url,
            tenant_id=request.state.tenant_id,
            webhook_url=webhook_url, signing_key=signing_key,
            payload=build_retry_payload(task_name=task_name, task_kwargs=task_kwargs),
        )
        enqueued = await _enqueue_ingest_job(
            request=request,
            db=db,
            job=job,
            item=item,
            task_name=task_name,
            task_kwargs=task_kwargs,
        )
        results.append(
            BatchIngestResult(
                job_id=job.id,
                item_id=item.id,
                status="queued" if enqueued else "failed",
            )
        )

    return BatchIngestResponse(results=results, total=len(results))
