import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import generate_webhook_signing_key, verify_capture_write_auth
from app.config import settings
from app.database import get_db
from app.models.item import Item
from app.models.job import Job
from app.models.web_save import WebSave
from app.schemas.ingest import (
    BrowserCaptureRequest,
    BrowserCaptureResponse,
    BrowserImageCandidate,
    BrowserImageUploadResponse,
)
from app.utils.job_payloads import build_retry_payload
from app.utils.outbound_http import (
    OutboundUrlError,
    validate_public_http_url_async,
)
from app.utils.webhook import validate_webhook_url
from app.api.ingest import (
    _create_item_and_job,
    _enqueue_ingest_job,
    _record_extension_capture_audit,
)
from app.services.bundle import BundleValidationError, persist_upload_artifact_bytes
from app.services.image_analysis import build_image_analysis_metadata
from app.services.image_candidates import (
    DownloadedImageCandidate,
    HTTP_TIMEOUT,
    IMAGE_SIZE_LIMIT,
    MAX_IMAGE_CANDIDATES,
    ImageCandidateError,
    UploadedImage,
    download_image_candidate,
    inspect_uploaded_image,
    normalize_uploaded_image_origin,
    validate_candidate_relationship,
)

router = APIRouter(prefix="/capture", tags=["capture"])
logger = logging.getLogger(__name__)

_MEDIA_EXTENSIONS = frozenset(
    {
        ".aac",
        ".aiff",
        ".flac",
        ".m4a",
        ".m4v",
        ".mov",
        ".mp3",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".oga",
        ".ogg",
        ".ogv",
        ".wav",
        ".webm",
    }
)


_SOCIAL_HOST_SUFFIXES = (
    "x.com",
    "twitter.com",
    "bsky.app",
    "threads.net",
    "reddit.com",
    "linkedin.com",
)

_DownloadedImageCandidate = DownloadedImageCandidate


def _normalize_http_url(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=422, detail="Invalid URL")
    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path or "",
            "",
            parsed.query or "",
            parsed.fragment or "",
        )
    )


def _source_domain(normalized_url: str | None) -> str | None:
    if normalized_url is None:
        return None
    return urlparse(normalized_url).hostname


def _clean_tags(tags: list[str]) -> list[str]:
    cleaned = []
    seen = set()
    for tag in tags:
        value = tag.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        cleaned.append(value)
    return cleaned


def _host_matches(hostname: str, suffix: str) -> bool:
    return hostname == suffix or hostname.endswith(f".{suffix}")


def _validate_candidate_relationship(
    *,
    candidate: BrowserImageCandidate,
    normalized_url: str,
    resolved_kind: str,
) -> str:
    try:
        return validate_candidate_relationship(
            candidate=candidate,
            source_url=normalized_url,
            resolved_kind=resolved_kind,
        )
    except ImageCandidateError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


async def _download_image_candidate(
    *,
    client: httpx.AsyncClient,
    candidate: BrowserImageCandidate,
    normalized_candidate_url: str,
    source_url: str,
) -> _DownloadedImageCandidate:
    try:
        return await download_image_candidate(
            client=client,
            candidate=candidate,
            normalized_candidate_url=normalized_candidate_url,
            source_url=source_url,
        )
    except ImageCandidateError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


async def download_browser_image_for_proxy(*, image_url: str, source_url: str) -> _DownloadedImageCandidate:
    """Fetch a stored browser image through the capture SSRF controls."""
    candidate = BrowserImageCandidate(url=image_url, source_post_url=source_url)
    normalized_url = _validate_candidate_relationship(
        candidate=candidate,
        normalized_url=source_url,
        resolved_kind="social_post",
    )
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, trust_env=False) as client:
        return await _download_image_candidate(
            client=client,
            candidate=candidate,
            normalized_candidate_url=normalized_url,
            source_url=source_url,
        )


async def _validate_and_download_image_candidates(
    *,
    body: BrowserCaptureRequest,
    normalized_url: str | None,
    resolved_kind: str,
) -> list[_DownloadedImageCandidate]:
    if not body.image_candidates:
        return []
    if normalized_url is None:
        raise HTTPException(status_code=422, detail="url is required for image_candidates")
    normalized_candidate_urls = [
        _validate_candidate_relationship(
            candidate=candidate,
            normalized_url=normalized_url,
            resolved_kind=resolved_kind,
        )
        for candidate in body.image_candidates
    ]
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, trust_env=False) as client:
        return [
            await _download_image_candidate(
                client=client,
                candidate=candidate,
                normalized_candidate_url=normalized_candidate_url,
                source_url=normalized_url,
            )
            for candidate, normalized_candidate_url in zip(body.image_candidates, normalized_candidate_urls, strict=True)
        ]


def _is_media_url(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.lower()
    if any(path.endswith(extension) for extension in _MEDIA_EXTENSIONS):
        return True
    if parsed.hostname == "youtu.be":
        return True
    if parsed.hostname and _host_matches(parsed.hostname, "youtube.com"):
        return parsed.path.startswith("/watch") or parsed.path.startswith("/shorts/")
    return False


def _is_social_url(url: str) -> bool:
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    if any(_host_matches(hostname, suffix) for suffix in _SOCIAL_HOST_SUFFIXES):
        return True
    parts = [part for part in parsed.path.split("/") if part]
    return len(parts) == 2 and parts[0].startswith("@") and parts[1].isdigit()


def _resolve_capture_kind(body: BrowserCaptureRequest, normalized_url: str | None) -> str:
    if body.selection_text and body.selection_text.strip():
        return "selection_note"
    if normalized_url is None:
        raise HTTPException(status_code=422, detail="url is required unless selection_text is present")
    if _is_media_url(normalized_url):
        return "media"
    if _is_social_url(normalized_url):
        return "social_post"
    return "webpage"


def _capture_metadata(
    *,
    body: BrowserCaptureRequest,
    normalized_url: str | None,
    resolved_kind: str,
    route: str,
    tags: list[str],
    extension_version: str | None,
) -> dict[str, Any]:
    selection = body.selection_text.strip() if body.selection_text else None
    metadata: dict[str, Any] = {
        "browser_capture": {
            "source_url": normalized_url,
            "source_title": body.page_title.strip() if body.page_title else None,
            "capture_kind": resolved_kind,
            "client_detected_kind": body.detected_kind,
            "route": route,
            "browser_extension_version": extension_version,
            "tags": tags,
            "extension_metadata": body.extension_metadata,
        }
    }
    if selection is not None:
        metadata["browser_capture"]["captured_selection"] = {
            "char_count": len(selection),
            "summary": selection[:240],
        }
    return metadata


@dataclass(frozen=True)
class _CapturedImage:
    """One image that is ready to become a child item of a browser capture.

    Bytes reach Palace two ways: Palace downloads a candidate URL itself, or
    the extension uploads what the browser already decoded. Both converge here,
    but their provenance differs, so the source-specific fields stay in
    ``provenance`` and ``artifact_source`` records which route produced them.
    """

    content: bytes
    media_type: str
    extension: str
    byte_hash: str
    byte_size: int
    order: int | None
    alt_text: str | None
    role: str | None
    width: int | None
    height: int | None
    # Collapses repeats inside one request. A download keys on its verified
    # final URL; an upload can only key on the URL the client claimed.
    dedupe_key: str
    artifact_source: str
    provenance: dict[str, Any]


def _captured_image_from_download(
    *,
    downloaded: _DownloadedImageCandidate,
    source_post_url: str,
) -> _CapturedImage:
    candidate = downloaded.candidate
    return _CapturedImage(
        content=downloaded.content,
        media_type=downloaded.media_type,
        extension=downloaded.extension,
        byte_hash=downloaded.byte_hash,
        byte_size=downloaded.byte_size,
        order=candidate.order,
        alt_text=candidate.alt_text,
        role=candidate.role,
        width=candidate.width,
        height=candidate.height,
        dedupe_key=downloaded.final_url,
        artifact_source="browser_image_candidate",
        provenance={
            "source_post_url": source_post_url,
            "candidate_url": downloaded.normalized_url,
            "final_url": downloaded.final_url,
        },
    )


def _captured_image_from_upload(
    *,
    uploaded: UploadedImage,
    source_page_url: str | None,
    source_image_url: str | None,
    byte_origin: str,
    alt_text: str | None,
    role: str | None,
    width: int | None,
    height: int | None,
    order: int | None,
) -> _CapturedImage:
    return _CapturedImage(
        content=uploaded.content,
        media_type=uploaded.media_type,
        extension=uploaded.extension,
        byte_hash=uploaded.byte_hash,
        byte_size=uploaded.byte_size,
        order=order,
        alt_text=alt_text,
        role=role,
        width=width,
        height=height,
        dedupe_key=source_image_url or uploaded.byte_hash,
        artifact_source="browser_image_upload",
        provenance={
            "source_post_url": source_page_url,
            "source_image_url": source_image_url,
            "byte_origin": byte_origin,
            # Palace never fetched these bytes. Anything downstream that treats
            # a URL as evidence must not treat this one that way.
            "client_asserted_source": True,
        },
    )


async def _create_browser_image_items(
    db: AsyncSession,
    *,
    parent_item: Item,
    tenant_id: str,
    normalized_url: str,
    downloaded_candidates: list[_DownloadedImageCandidate],
) -> tuple[list[dict[str, Any]], list[Item], list[Job]]:
    return await _persist_captured_images(
        db,
        parent_item=parent_item,
        tenant_id=tenant_id,
        images=[
            _captured_image_from_download(downloaded=downloaded, source_post_url=normalized_url)
            for downloaded in downloaded_candidates
        ],
    )


async def _persist_captured_images(
    db: AsyncSession,
    *,
    parent_item: Item,
    tenant_id: str,
    images: list[_CapturedImage],
) -> tuple[list[dict[str, Any]], list[Item], list[Job]]:
    linked_candidates: list[dict[str, Any]] = []
    child_items: list[Item] = []
    child_jobs: list[Job] = []
    artifact_paths: list[Path] = []
    seen_candidate_keys: set[tuple[str, str]] = set()
    try:
        # Keep the parent item, job, and web save in the outer transaction. A
        # failed child artifact then rolls back only the child rows, so the
        # caller can persist truthful failure and retry state.
        async with db.begin_nested():
            for index, image in enumerate(images):
                candidate_key = (image.byte_hash, image.dedupe_key)
                if candidate_key in seen_candidate_keys:
                    continue
                seen_candidate_keys.add(candidate_key)
                order = image.order if image.order is not None else index
                title = (
                    image.alt_text.strip()
                    if image.alt_text and image.alt_text.strip()
                    else f"Image from {parent_item.title}"
                )
                filename = f"browser-image{image.extension}"
                image_metadata = {
                    "filename": filename,
                    "media_type": image.media_type,
                    **build_image_analysis_metadata(
                        filename=filename,
                        media_type=image.media_type,
                        extension=image.extension,
                        image_bytes=image.content,
                        byte_hash=image.byte_hash,
                        status="queued",
                    ),
                }
                image_metadata["image_analysis"]["artifact"]["source"] = image.artifact_source
                child_item = Item(
                    source_type="image_candidate",
                    source_url=None,
                    title=title,
                    # Keep the captured evidence visible while its analysis
                    # job runs independently.
                    status="captured",
                    tenant_id=tenant_id,
                    # The byte hash is provenance metadata. Image candidates
                    # are not deduped against user-uploaded ``image`` items.
                    content_hash=None,
                    metadata_={
                        "browser_capture_image": {
                            "source": image.artifact_source,
                            "status": "captured_not_processed",
                            "parent_item_id": str(parent_item.id),
                            **image.provenance,
                            "media_type": image.media_type,
                            "byte_hash": image.byte_hash,
                            "byte_size": image.byte_size,
                            "order": order,
                            "alt_text": image.alt_text,
                            "role": image.role,
                            "dimensions": {
                                "width": image.width,
                                "height": image.height,
                            },
                        },
                        **image_metadata,
                    },
                )
                db.add(child_item)
                await db.flush()
                storage_path = persist_upload_artifact_bytes(
                    image.content,
                    tenant_id=tenant_id,
                    item_id=child_item.id,
                    extension=image.extension,
                )
                artifact_paths.append(Path(storage_path))
                browser_image = dict(child_item.metadata_["browser_capture_image"])
                browser_image["artifact"] = {
                    "filename": f"{child_item.id}{image.extension}",
                    "media_type": image.media_type,
                    "storage_path": storage_path,
                }
                child_item.metadata_ = {"browser_capture_image": browser_image}
                child_item.metadata_.update(image_metadata)
                image_metadata = {
                    **image_metadata,
                    "filename": f"{child_item.id}{image.extension}",
                    "image_analysis": {
                        **image_metadata["image_analysis"],
                        "artifact": {
                            **image_metadata["image_analysis"]["artifact"],
                            "filename": f"{child_item.id}{image.extension}",
                            "storage_path": storage_path,
                        },
                    },
                }
                child_item.metadata_["image_analysis"] = image_metadata["image_analysis"]
                child_item.metadata_["filename"] = f"{child_item.id}{image.extension}"
                child_job = Job(
                    item_id=child_item.id,
                    job_type="image",
                    status="queued",
                    progress=0,
                    tenant_id=tenant_id,
                    payload=build_retry_payload(
                        task_name="process_image",
                        task_kwargs={"image_metadata": image_metadata},
                    ),
                )
                db.add(child_job)
                child_items.append(child_item)
                child_jobs.append(child_job)
                linked_candidates.append(
                    _linked_captured_image_metadata(
                        item_id=child_item.id,
                        image=image,
                        order=order,
                    )
                )
    except (OSError, BundleValidationError):
        for artifact_path in artifact_paths:
            artifact_path.unlink(missing_ok=True)
        raise
    if linked_candidates:
        parent_item.metadata_ = {
            **(parent_item.metadata_ or {}),
            "browser_capture": {
                **((parent_item.metadata_ or {}).get("browser_capture") or {}),
                "image_candidates": [
                    *((parent_item.metadata_ or {}).get("browser_capture") or {}).get("image_candidates", []),
                    *linked_candidates,
                ],
            },
        }
        await db.commit()
    return linked_candidates, child_items, child_jobs


def _linked_captured_image_metadata(
    *,
    item_id: Any,
    image: _CapturedImage,
    order: int,
) -> dict[str, Any]:
    linked: dict[str, Any] = {"item_id": str(item_id)}
    if image.artifact_source == "browser_image_candidate":
        linked["candidate_url"] = image.provenance["candidate_url"]
        linked["final_url"] = image.provenance["final_url"]
    else:
        linked["source"] = image.artifact_source
        linked["source_image_url"] = image.provenance["source_image_url"]
        linked["byte_origin"] = image.provenance["byte_origin"]
    linked["media_type"] = image.media_type
    linked["byte_hash"] = image.byte_hash
    linked["byte_size"] = image.byte_size
    linked["order"] = order
    return linked


async def _get_active_web_save(
    db: AsyncSession,
    *,
    tenant_id: str,
    normalized_url: str | None,
) -> WebSave | None:
    if normalized_url is None:
        return None
    result = await db.execute(
        select(WebSave)
        .join(Item, Item.id == WebSave.item_id)
        .where(WebSave.tenant_id == tenant_id)
        .where(WebSave.normalized_url == normalized_url)
        .where(WebSave.archived_at.is_(None))
        .where(Item.deleted_at.is_(None))
        .where(Item.status != "deleted")
    )
    return result.scalars().first()


async def _archive_inactive_web_saves(
    db: AsyncSession,
    *,
    tenant_id: str,
    normalized_url: str | None,
) -> None:
    if normalized_url is None:
        return
    result = await db.execute(
        select(WebSave, Item)
        .outerjoin(Item, Item.id == WebSave.item_id)
        .where(WebSave.tenant_id == tenant_id)
        .where(WebSave.normalized_url == normalized_url)
        .where(WebSave.archived_at.is_(None))
    )
    archived_at = datetime.now(timezone.utc)
    changed = False
    for web_save, item in result.all():
        if item is None or item.deleted_at is not None or item.status == "deleted":
            web_save.archived_at = archived_at
            changed = True
    if changed:
        await db.commit()


def _link_source_web_save(metadata: dict[str, Any], web_save: WebSave | None) -> None:
    if web_save is None:
        return
    metadata["browser_capture"]["source_web_save_id"] = str(web_save.id)
    metadata["browser_capture"]["source_item_id"] = str(web_save.item_id)


async def _create_web_save(
    db: AsyncSession,
    *,
    tenant_id: str,
    item_id: Any,
    body: BrowserCaptureRequest,
    normalized_url: str,
    resolved_kind: str,
    tags: list[str],
    extension_version: str | None,
) -> tuple[WebSave, bool]:
    web_save = WebSave(
        tenant_id=tenant_id,
        item_id=item_id,
        original_url=body.url.strip() if body.url else normalized_url,
        normalized_url=normalized_url,
        source_title=body.page_title.strip() if body.page_title and body.page_title.strip() else None,
        source_domain=_source_domain(normalized_url),
        capture_kind=resolved_kind,
        user_tags=tags,
        extension_version=extension_version,
        metadata_={
            "browser_capture": {
                "client_detected_kind": body.detected_kind,
                "extension_metadata": body.extension_metadata,
                "preview_media": None,
            }
        },
    )
    db.add(web_save)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        existing = await _get_active_web_save(
            db,
            tenant_id=tenant_id,
            normalized_url=normalized_url,
        )
        if existing is not None:
            return existing, True
        raise HTTPException(status_code=409, detail="Web save already exists") from exc
    await db.refresh(web_save)
    return web_save, False


@router.post(
    "/browser",
    response_model=BrowserCaptureResponse,
    status_code=202,
    dependencies=[Depends(verify_capture_write_auth)],
)
async def capture_browser(
    body: BrowserCaptureRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BrowserCaptureResponse:
    normalized_url = _normalize_http_url(body.url)
    if normalized_url is not None:
        try:
            normalized_url = await validate_public_http_url_async(normalized_url)
        except OutboundUrlError as exc:
            raise HTTPException(status_code=422, detail=f"Invalid capture URL: {exc}") from exc
    resolved_kind = _resolve_capture_kind(body, normalized_url)
    route = "note" if resolved_kind == "selection_note" else "media" if resolved_kind == "media" else "webpage"
    tags = _clean_tags(body.tags)
    extension_version = (
        body.browser_extension_version
        or request.headers.get("X-Palace-Extension-Version")
        or None
    )
    metadata = _capture_metadata(
        body=body,
        normalized_url=normalized_url,
        resolved_kind=resolved_kind,
        route=route,
        tags=tags,
        extension_version=extension_version,
    )
    existing_web_save = await _get_active_web_save(
        db,
        tenant_id=request.state.tenant_id,
        normalized_url=normalized_url,
    )
    if resolved_kind != "selection_note" and existing_web_save is not None:
        return BrowserCaptureResponse(
            job_id=None,
            item_id=existing_web_save.item_id,
            status="duplicate",
            kind=resolved_kind,
            route=route,
            source_url=normalized_url,
            duplicate_of=existing_web_save.item_id,
            web_save_id=existing_web_save.id,
        )
    downloaded_image_candidates = await _validate_and_download_image_candidates(
        body=body,
        normalized_url=normalized_url,
        resolved_kind=resolved_kind,
    )
    if resolved_kind == "selection_note":
        _link_source_web_save(metadata, existing_web_save)
    elif resolved_kind != "selection_note":
        await _archive_inactive_web_saves(
            db,
            tenant_id=request.state.tenant_id,
            normalized_url=normalized_url,
        )

    webhook_url = validate_webhook_url(body.webhook_url) if body.webhook_url else None
    signing_key = generate_webhook_signing_key() if webhook_url else None
    title = body.page_title.strip() if body.page_title and body.page_title.strip() else normalized_url or "Browser selection"

    if route == "note":
        content = (body.selection_text or "").strip()
        if not content:
            raise HTTPException(status_code=422, detail="selection_text is required for selection_note")
        task_name = "process_note"
        source_type = "note"
        source_url = None
        task_kwargs: dict[str, Any] = {
            "title": title,
            "content": content,
            "tags": tags or None,
            "tenant_id": request.state.tenant_id,
            "model": body.model,
        }
    elif route == "media":
        task_name = "process_media"
        source_type = "media"
        source_url = normalized_url
        task_kwargs = {
            "url": normalized_url,
            "tenant_id": request.state.tenant_id,
            "model": body.model,
        }
    else:
        task_name = "process_webpage"
        source_type = "webpage"
        source_url = normalized_url
        task_kwargs = {
            "url": normalized_url,
            "tenant_id": request.state.tenant_id,
            "model": body.model,
        }

    retry_kwargs = {key: value for key, value in task_kwargs.items() if key != "tenant_id"}
    item, job = await _create_item_and_job(
        db,
        source_type,
        title=title,
        source_url=source_url,
        tenant_id=request.state.tenant_id,
        webhook_url=webhook_url,
        signing_key=signing_key,
        payload=build_retry_payload(task_name=task_name, task_kwargs=retry_kwargs),
        metadata=metadata,
        tags=tags,
    )
    web_save = None
    if resolved_kind != "selection_note" and normalized_url is not None:
        web_save, raced_duplicate = await _create_web_save(
            db,
            tenant_id=request.state.tenant_id,
            item_id=item.id,
            body=body,
            normalized_url=normalized_url,
            resolved_kind=resolved_kind,
            tags=tags,
            extension_version=extension_version,
        )
        if raced_duplicate:
            return BrowserCaptureResponse(
                job_id=None,
                item_id=web_save.item_id,
                status="duplicate",
                kind=resolved_kind,
                route=route,
                source_url=normalized_url,
                duplicate_of=web_save.item_id,
                web_save_id=web_save.id,
            )
    linked_image_candidates = []
    linked_image_items: list[Item] = []
    linked_image_jobs: list[Job] = []
    if downloaded_image_candidates and normalized_url is not None:
        try:
            linked_image_candidates, linked_image_items, linked_image_jobs = await _create_browser_image_items(
                db,
                parent_item=item,
                tenant_id=request.state.tenant_id,
                normalized_url=normalized_url,
                downloaded_candidates=downloaded_image_candidates,
            )
        except (OSError, BundleValidationError) as exc:
            item.status = "failed"
            job.status = "failed"
            job.error_message = f"Failed to persist browser image artifact: {exc}"
            job.completed_at = datetime.now(timezone.utc)
            if web_save is not None:
                web_save.archived_at = datetime.now(timezone.utc)
            await db.commit()
            raise HTTPException(
                status_code=500,
                detail="Failed to persist browser image artifact; capture can be retried",
            ) from exc
        if web_save is not None:
            web_save.metadata_ = {
                **(web_save.metadata_ or {}),
                "browser_capture": {
                    **((web_save.metadata_ or {}).get("browser_capture") or {}),
                    "preview_media": linked_image_candidates,
                },
            }
            await db.commit()

    enqueued = await _enqueue_ingest_job(
        request=request,
        db=db,
        job=job,
        item=item,
        task_name=task_name,
        task_kwargs=task_kwargs,
    )
    if not enqueued:
        if web_save is not None:
            web_save.archived_at = datetime.now(timezone.utc)
            await db.commit()
        raise HTTPException(status_code=503, detail="Capture enqueue failed; job marked failed for retry")

    # Image candidates are independent child work. A failed child enqueue must
    # not turn a valid parent capture into a failed request, and its durable
    # artifact/citation remains available for retry.
    for linked_image_item, linked_image_job in zip(
        linked_image_items, linked_image_jobs, strict=True
    ):
        child_enqueued = await _enqueue_ingest_job(
            request=request,
            db=db,
            job=linked_image_job,
            item=linked_image_item,
            task_name="process_image",
            task_kwargs={
                "image_metadata": linked_image_item.metadata_,
                "tenant_id": request.state.tenant_id,
            },
        )
        if not child_enqueued:
            # _enqueue_ingest_job persisted only this child/job's failure.
            # Keep processing sibling candidates and report the parent success.
            child_metadata = dict(linked_image_item.metadata_ or {})
            browser_image = dict(child_metadata.get("browser_capture_image") or {})
            browser_image["status"] = "enqueue_failed"
            browser_image["enqueue_error"] = {
                "classification": "retryable",
                "code": "enqueue_failed",
                "message": "worker queue unavailable",
                "attempted_at": datetime.now(timezone.utc).isoformat(),
            }
            analysis = dict(child_metadata.get("image_analysis") or {})
            analysis["status"] = "failed"
            vision = dict(analysis.get("vision") or {})
            vision["error"] = {
                "message": "worker queue unavailable",
                "retryable": True,
                "code": "enqueue_failed",
            }
            analysis["vision"] = vision
            child_metadata["browser_capture_image"] = browser_image
            child_metadata["image_analysis"] = analysis
            linked_image_item.metadata_ = child_metadata
            await db.commit()
            logger.warning("image candidate enqueue failed for %s; parent continues", linked_image_item.id)

    await _record_extension_capture_audit(request=request, route=route, job=job, item=item)
    return BrowserCaptureResponse(
        job_id=job.id,
        item_id=item.id,
        status="queued",
        kind=resolved_kind,
        route=route,
        source_url=normalized_url,
        web_save_id=web_save.id if web_save else None,
    )


async def _read_bounded_upload(file: UploadFile, *, limit: int) -> bytes:
    """Read an upload into memory, refusing anything past the limit.

    ``UploadFile.read()`` with no argument would let a client choose how much
    of this process's memory to take, so the read is chunked and checked.
    """

    buffer = bytearray()
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > limit:
            raise HTTPException(status_code=413, detail="uploaded image is too large")
    return bytes(buffer)


def _parse_tag_form_value(value: str | None) -> list[str]:
    return _clean_tags((value or "").split(","))


async def _active_web_save_for_item(
    db: AsyncSession,
    *,
    tenant_id: str,
    item_id: Any,
) -> WebSave | None:
    result = await db.execute(
        select(WebSave)
        .where(WebSave.tenant_id == tenant_id)
        .where(WebSave.item_id == item_id)
        .where(WebSave.archived_at.is_(None))
    )
    return result.scalars().first()


async def _attach_uploaded_image(
    db: AsyncSession,
    *,
    request: Request,
    tenant_id: str,
    parent_item_id: uuid.UUID,
    uploaded: UploadedImage,
    byte_origin: str,
    normalized_page_url: str | None,
    normalized_image_url: str | None,
    alt_text: str | None,
    role: str | None,
    width: int | None,
    height: int | None,
    order: int | None,
) -> BrowserImageUploadResponse:
    result = await db.execute(
        select(Item)
        .where(Item.id == parent_item_id)
        .where(Item.tenant_id == tenant_id)
        .where(Item.deleted_at.is_(None))
    )
    parent_item = result.scalars().first()
    if parent_item is None or parent_item.status == "deleted":
        raise HTTPException(status_code=404, detail="Capture item not found")

    browser_capture = (parent_item.metadata_ or {}).get("browser_capture")
    if not isinstance(browser_capture, dict):
        raise HTTPException(status_code=422, detail="Item is not a browser capture")

    existing_linked = [
        linked for linked in (browser_capture.get("image_candidates") or []) if isinstance(linked, dict)
    ]
    # Uploading the same bytes twice is a retry, not a second image. Answer it
    # with the child that already holds them instead of storing a copy.
    for linked in existing_linked:
        if linked.get("byte_hash") == uploaded.byte_hash and linked.get("item_id"):
            existing_child_id = uuid.UUID(str(linked["item_id"]))
            return BrowserImageUploadResponse(
                job_id=None,
                item_id=existing_child_id,
                status="duplicate",
                parent_item_id=parent_item.id,
                media_type=uploaded.media_type,
                byte_hash=uploaded.byte_hash,
                byte_size=uploaded.byte_size,
                byte_origin=byte_origin,
                duplicate_of=existing_child_id,
            )
    if len(existing_linked) >= MAX_IMAGE_CANDIDATES:
        raise HTTPException(
            status_code=422,
            detail=f"Capture already holds the maximum of {MAX_IMAGE_CANDIDATES} images",
        )

    image = _captured_image_from_upload(
        uploaded=uploaded,
        source_page_url=normalized_page_url or browser_capture.get("source_url"),
        source_image_url=normalized_image_url,
        byte_origin=byte_origin,
        alt_text=alt_text,
        role=role,
        width=width,
        height=height,
        order=order if order is not None else len(existing_linked),
    )
    try:
        linked_images, child_items, child_jobs = await _persist_captured_images(
            db,
            parent_item=parent_item,
            tenant_id=tenant_id,
            images=[image],
        )
    except (OSError, BundleValidationError) as exc:
        logger.exception("failed to persist uploaded image for capture %s", parent_item_id)
        raise HTTPException(
            status_code=500,
            detail="Failed to persist browser image artifact; upload can be retried",
        ) from exc

    child_item, child_job = child_items[0], child_jobs[0]
    web_save = await _active_web_save_for_item(db, tenant_id=tenant_id, item_id=parent_item.id)
    if web_save is not None:
        web_save_capture = (web_save.metadata_ or {}).get("browser_capture") or {}
        preview_media = [
            *(web_save_capture.get("preview_media") or []),
            *linked_images,
        ]
        web_save.metadata_ = {
            **(web_save.metadata_ or {}),
            "browser_capture": {**web_save_capture, "preview_media": preview_media},
        }
        await db.commit()

    enqueued = await _enqueue_ingest_job(
        request=request,
        db=db,
        job=child_job,
        item=child_item,
        task_name="process_image",
        task_kwargs={
            "image_metadata": child_item.metadata_,
            "tenant_id": tenant_id,
        },
    )
    if not enqueued:
        raise HTTPException(status_code=503, detail="Image enqueue failed; job marked failed for retry")

    await _record_extension_capture_audit(request=request, route="image", job=child_job, item=child_item)
    return BrowserImageUploadResponse(
        job_id=child_job.id,
        item_id=child_item.id,
        status="queued",
        parent_item_id=parent_item.id,
        media_type=uploaded.media_type,
        byte_hash=uploaded.byte_hash,
        byte_size=uploaded.byte_size,
        byte_origin=byte_origin,
        web_save_id=web_save.id if web_save is not None else None,
    )


async def _create_standalone_image_capture(
    db: AsyncSession,
    *,
    request: Request,
    tenant_id: str,
    uploaded: UploadedImage,
    byte_origin: str,
    normalized_page_url: str | None,
    normalized_image_url: str | None,
    page_title: str | None,
    alt_text: str | None,
    role: str | None,
    width: int | None,
    height: int | None,
    order: int | None,
    tags: list[str],
    extension_version: str | None,
) -> BrowserImageUploadResponse:
    # The URL is recorded, never fetched, so an intranet or otherwise private
    # address is allowed here on purpose: those are exactly the images Palace
    # cannot download for itself.
    save_url = normalized_image_url or normalized_page_url
    if save_url is None:
        raise HTTPException(
            status_code=422,
            detail="image_url or source_url is required for a standalone image capture",
        )

    existing_id = await db.scalar(
        select(Item.id)
        .where(Item.content_hash == uploaded.byte_hash)
        .where(Item.source_type == "image")
        .where(Item.tenant_id == tenant_id)
        .where(Item.status != "failed")
        .where(Item.status != "deleted")
        .where(Item.deleted_at.is_(None))
        .limit(1)
    )
    if existing_id:
        return BrowserImageUploadResponse(
            job_id=None,
            item_id=existing_id,
            status="duplicate",
            media_type=uploaded.media_type,
            byte_hash=uploaded.byte_hash,
            byte_size=uploaded.byte_size,
            byte_origin=byte_origin,
            duplicate_of=existing_id,
        )

    existing_web_save = await _get_active_web_save(db, tenant_id=tenant_id, normalized_url=save_url)
    if existing_web_save is not None:
        return BrowserImageUploadResponse(
            job_id=None,
            item_id=existing_web_save.item_id,
            status="duplicate",
            media_type=uploaded.media_type,
            byte_hash=uploaded.byte_hash,
            byte_size=uploaded.byte_size,
            byte_origin=byte_origin,
            duplicate_of=existing_web_save.item_id,
            web_save_id=existing_web_save.id,
        )
    await _archive_inactive_web_saves(db, tenant_id=tenant_id, normalized_url=save_url)

    title = (
        (page_title or "").strip()
        or (alt_text or "").strip()
        or save_url
    )
    filename = f"browser-image{uploaded.extension}"
    image_metadata = {
        "filename": filename,
        "media_type": uploaded.media_type,
        **build_image_analysis_metadata(
            filename=filename,
            media_type=uploaded.media_type,
            extension=uploaded.extension,
            image_bytes=uploaded.content,
            byte_hash=uploaded.byte_hash,
            status="queued",
        ),
    }
    image_metadata["image_analysis"]["artifact"]["source"] = "browser_image_upload"
    metadata: dict[str, Any] = {
        "browser_capture": {
            "source_url": save_url,
            "source_title": (page_title or "").strip() or None,
            "capture_kind": "image",
            "client_detected_kind": "image",
            "route": "image",
            "browser_extension_version": extension_version,
            "tags": tags,
            "extension_metadata": {},
        },
        "browser_capture_image": {
            "source": "browser_image_upload",
            "status": "captured_not_processed",
            "parent_item_id": None,
            "source_post_url": normalized_page_url,
            "source_image_url": normalized_image_url,
            "byte_origin": byte_origin,
            # Palace never fetched these bytes, so the URL above is what the
            # client said, not what Palace verified.
            "client_asserted_source": True,
            "media_type": uploaded.media_type,
            "byte_hash": uploaded.byte_hash,
            "byte_size": uploaded.byte_size,
            "order": order if order is not None else 0,
            "alt_text": alt_text,
            "role": role,
            "dimensions": {"width": width, "height": height},
        },
        **image_metadata,
    }

    item, job = await _create_item_and_job(
        db,
        "image",
        title=title,
        source_url=normalized_image_url,
        tenant_id=tenant_id,
        payload=build_retry_payload(
            task_name="process_image",
            task_kwargs={"image_metadata": image_metadata},
        ),
        metadata=metadata,
        tags=tags,
    )

    try:
        storage_path = persist_upload_artifact_bytes(
            uploaded.content,
            tenant_id=tenant_id,
            item_id=item.id,
            extension=uploaded.extension,
        )
    except (OSError, BundleValidationError) as exc:
        item.status = "failed"
        job.status = "failed"
        job.error_message = f"Failed to persist uploaded image artifact: {exc}"
        job.completed_at = datetime.now(timezone.utc)
        await db.commit()
        logger.exception("failed to persist uploaded image artifact for item %s", item.id)
        raise HTTPException(
            status_code=500,
            detail="Failed to persist browser image artifact; upload can be retried",
        ) from exc

    stored_filename = f"{item.id}{uploaded.extension}"
    image_metadata = {
        "filename": stored_filename,
        "media_type": uploaded.media_type,
        **build_image_analysis_metadata(
            filename=stored_filename,
            media_type=uploaded.media_type,
            extension=uploaded.extension,
            image_bytes=uploaded.content,
            byte_hash=uploaded.byte_hash,
            artifact_storage_path=storage_path,
            status="queued",
        ),
    }
    image_metadata["image_analysis"]["artifact"]["source"] = "browser_image_upload"
    capture_image = {
        **metadata["browser_capture_image"],
        "artifact": {
            "filename": stored_filename,
            "media_type": uploaded.media_type,
            "storage_path": storage_path,
        },
    }
    item.metadata_ = {
        **(item.metadata_ or {}),
        "browser_capture_image": capture_image,
        **image_metadata,
    }
    item.content_hash = uploaded.byte_hash
    job.payload = build_retry_payload(
        task_name="process_image",
        task_kwargs={"image_metadata": image_metadata},
    )

    web_save = WebSave(
        tenant_id=tenant_id,
        item_id=item.id,
        original_url=save_url,
        normalized_url=save_url,
        source_title=(page_title or "").strip() or None,
        source_domain=_source_domain(save_url),
        capture_kind="image",
        user_tags=tags,
        extension_version=extension_version,
        metadata_={
            "browser_capture": {
                "client_detected_kind": "image",
                "extension_metadata": {},
                "preview_media": None,
            }
        },
    )
    db.add(web_save)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        existing = await _get_active_web_save(db, tenant_id=tenant_id, normalized_url=save_url)
        if existing is None:
            raise HTTPException(status_code=409, detail="Web save already exists") from exc
        return BrowserImageUploadResponse(
            job_id=None,
            item_id=existing.item_id,
            status="duplicate",
            media_type=uploaded.media_type,
            byte_hash=uploaded.byte_hash,
            byte_size=uploaded.byte_size,
            byte_origin=byte_origin,
            duplicate_of=existing.item_id,
            web_save_id=existing.id,
        )
    await db.refresh(web_save)

    enqueued = await _enqueue_ingest_job(
        request=request,
        db=db,
        job=job,
        item=item,
        task_name="process_image",
        task_kwargs={
            "image_metadata": image_metadata,
            "tenant_id": tenant_id,
        },
    )
    if not enqueued:
        web_save.archived_at = datetime.now(timezone.utc)
        await db.commit()
        raise HTTPException(status_code=503, detail="Image enqueue failed; job marked failed for retry")

    await _record_extension_capture_audit(request=request, route="image", job=job, item=item)
    return BrowserImageUploadResponse(
        job_id=job.id,
        item_id=item.id,
        status="queued",
        media_type=uploaded.media_type,
        byte_hash=uploaded.byte_hash,
        byte_size=uploaded.byte_size,
        byte_origin=byte_origin,
        web_save_id=web_save.id,
    )


@router.post(
    "/browser/images",
    response_model=BrowserImageUploadResponse,
    status_code=202,
    dependencies=[Depends(verify_capture_write_auth)],
)
async def capture_browser_image(
    request: Request,
    db: AsyncSession = Depends(get_db),
    file: UploadFile = File(...),
    item_id: uuid.UUID | None = Form(default=None),
    source_url: str | None = Form(default=None),
    image_url: str | None = Form(default=None),
    page_title: str | None = Form(default=None),
    alt_text: str | None = Form(default=None),
    width: int | None = Form(default=None, ge=1),
    height: int | None = Form(default=None, ge=1),
    role: str | None = Form(default=None),
    order: int | None = Form(default=None, ge=0),
    origin: str = Form(default="page_fetch"),
    tags: str | None = Form(default=None),
) -> BrowserImageUploadResponse:
    """Store image bytes the browser already holds.

    ``POST /capture/browser`` can only give Palace a URL to fetch, which fails
    for any image behind a session, a signed URL, or a `blob:`/`data:` source.
    The extension can read those bytes in the page it is already authenticated
    to, so this route accepts the bytes directly. With ``item_id`` the image is
    attached to that capture; without one it becomes its own image capture.
    """

    content = await _read_bounded_upload(file, limit=IMAGE_SIZE_LIMIT)
    try:
        uploaded = inspect_uploaded_image(content)
        byte_origin = normalize_uploaded_image_origin(origin)
    except ImageCandidateError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    normalized_page_url = _normalize_http_url(source_url)
    normalized_image_url = _normalize_http_url(image_url)
    tenant_id = request.state.tenant_id
    extension_version = request.headers.get("X-Palace-Extension-Version")

    if item_id is not None:
        return await _attach_uploaded_image(
            db,
            request=request,
            tenant_id=tenant_id,
            parent_item_id=item_id,
            uploaded=uploaded,
            byte_origin=byte_origin,
            normalized_page_url=normalized_page_url,
            normalized_image_url=normalized_image_url,
            alt_text=alt_text,
            role=role,
            width=width,
            height=height,
            order=order,
        )
    return await _create_standalone_image_capture(
        db,
        request=request,
        tenant_id=tenant_id,
        uploaded=uploaded,
        byte_origin=byte_origin,
        normalized_page_url=normalized_page_url,
        normalized_image_url=normalized_image_url,
        page_title=page_title,
        alt_text=alt_text,
        role=role,
        width=width,
        height=height,
        order=order,
        tags=_parse_tag_form_value(tags),
        extension_version=extension_version,
    )
