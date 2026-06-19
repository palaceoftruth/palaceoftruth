import hashlib
import ipaddress
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

from fastapi import APIRouter, Depends, HTTPException, Request
import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_capture_write_auth
from app.database import get_db
from app.models.item import Item
from app.models.web_save import WebSave
from app.schemas.ingest import BrowserCaptureRequest, BrowserCaptureResponse, BrowserImageCandidate
from app.utils.job_payloads import build_retry_payload
from app.utils.webhook import validate_webhook_url
from app.api.ingest import (
    _create_item_and_job,
    _enqueue_ingest_job,
    _record_extension_capture_audit,
)

router = APIRouter(prefix="/capture", tags=["capture"])

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

_SOCIAL_IMAGE_HOST_SUFFIXES: dict[str, tuple[str, ...]] = {
    "x.com": ("pbs.twimg.com", "video.twimg.com"),
    "twitter.com": ("pbs.twimg.com", "video.twimg.com"),
    "bsky.app": ("cdn.bsky.app",),
    "threads.net": ("cdninstagram.com", "fbcdn.net"),
    "reddit.com": (
        "i.redd.it",
        "preview.redd.it",
        "external-preview.redd.it",
        "v.redd.it",
        "redditmedia.com",
    ),
    "linkedin.com": ("media.licdn.com", "licdn.com"),
}

_CANDIDATE_IMAGE_SIZE_LIMIT = 8 * 1024 * 1024
_CANDIDATE_REDIRECT_LIMIT = 3
_CANDIDATE_HTTP_TIMEOUT = 10.0
_IMAGE_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})


@dataclass(frozen=True)
class _DownloadedImageCandidate:
    candidate: BrowserImageCandidate
    normalized_url: str
    final_url: str
    media_type: str
    byte_hash: str
    byte_size: int


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


def _normalize_candidate_url(value: str | None, *, detail: str) -> str:
    normalized = _normalize_http_url(value)
    if normalized is None:
        raise HTTPException(status_code=422, detail=detail)
    return normalized


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


def _hostname_is_private(hostname: str) -> bool:
    try:
        addresses = [ipaddress.ip_address(hostname)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise HTTPException(status_code=422, detail="image candidate host could not be resolved") from exc
        addresses = []
        for info in infos:
            try:
                addresses.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue
    if not addresses:
        raise HTTPException(status_code=422, detail="image candidate host could not be resolved")
    return any(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        for address in addresses
    )


def _assert_public_candidate_host(normalized_url: str) -> str:
    hostname = urlparse(normalized_url).hostname
    if not hostname:
        raise HTTPException(status_code=422, detail="image candidate host is required")
    if _hostname_is_private(hostname):
        raise HTTPException(status_code=422, detail="image candidate host is not allowed")
    return hostname


def _allowed_image_host_suffixes(source_url: str) -> tuple[str, ...]:
    source_host = urlparse(source_url).hostname or ""
    for source_suffix, image_suffixes in _SOCIAL_IMAGE_HOST_SUFFIXES.items():
        if _host_matches(source_host, source_suffix):
            return image_suffixes
    return ()


def _assert_allowed_image_host(*, image_url: str, source_url: str) -> None:
    image_host = _assert_public_candidate_host(image_url)
    allowed_suffixes = _allowed_image_host_suffixes(source_url)
    if not allowed_suffixes or not any(_host_matches(image_host, suffix) for suffix in allowed_suffixes):
        raise HTTPException(status_code=422, detail="image candidate host is not allowed for source post")


def _validate_candidate_relationship(
    *,
    candidate: BrowserImageCandidate,
    normalized_url: str,
    resolved_kind: str,
) -> str:
    if resolved_kind != "social_post":
        raise HTTPException(status_code=422, detail="image_candidates are only supported for social_post captures")
    normalized_candidate_url = _normalize_candidate_url(candidate.url, detail="Invalid image candidate URL")
    _assert_allowed_image_host(image_url=normalized_candidate_url, source_url=normalized_url)
    if candidate.source_post_url is not None:
        source_post_url = _normalize_candidate_url(
            candidate.source_post_url,
            detail="Invalid image candidate source_post_url",
        )
        if source_post_url != normalized_url:
            raise HTTPException(status_code=422, detail="image candidate source_post_url must match capture url")
    return normalized_candidate_url


async def _download_image_candidate(
    *,
    client: httpx.AsyncClient,
    candidate: BrowserImageCandidate,
    normalized_candidate_url: str,
    source_url: str,
) -> _DownloadedImageCandidate:
    current_url = normalized_candidate_url
    for _redirect in range(_CANDIDATE_REDIRECT_LIMIT + 1):
        _assert_allowed_image_host(image_url=current_url, source_url=source_url)
        try:
            response_context = client.stream("GET", current_url, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=422, detail="image candidate could not be downloaded") from exc

        try:
            async with response_context as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise HTTPException(status_code=422, detail="image candidate redirect missing location")
                    current_url = _normalize_candidate_url(
                        urljoin(current_url, location),
                        detail="Invalid image candidate redirect URL",
                    )
                    continue

                if response.status_code >= 400:
                    raise HTTPException(status_code=422, detail="image candidate returned an error")
                final_url = str(response.url)
                _assert_allowed_image_host(image_url=final_url, source_url=source_url)
                media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if media_type not in _IMAGE_MEDIA_TYPES:
                    raise HTTPException(status_code=422, detail="image candidate content type is not allowed")
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared_size = int(content_length)
                    except ValueError as exc:
                        raise HTTPException(status_code=422, detail="image candidate content length is invalid") from exc
                    if declared_size > _CANDIDATE_IMAGE_SIZE_LIMIT:
                        raise HTTPException(status_code=413, detail="image candidate is too large")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > _CANDIDATE_IMAGE_SIZE_LIMIT:
                        raise HTTPException(status_code=413, detail="image candidate is too large")
                image_bytes = bytes(content)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=422, detail="image candidate could not be downloaded") from exc
        byte_hash = hashlib.sha256(image_bytes).hexdigest()
        return _DownloadedImageCandidate(
            candidate=candidate,
            normalized_url=normalized_candidate_url,
            final_url=final_url,
            media_type=media_type,
            byte_hash=byte_hash,
            byte_size=len(image_bytes),
        )
    raise HTTPException(status_code=422, detail="image candidate redirected too many times")


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
    async with httpx.AsyncClient(timeout=_CANDIDATE_HTTP_TIMEOUT) as client:
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


async def _create_browser_image_items(
    db: AsyncSession,
    *,
    parent_item: Item,
    tenant_id: str,
    normalized_url: str,
    downloaded_candidates: list[_DownloadedImageCandidate],
) -> tuple[list[dict[str, Any]], list[Item]]:
    linked_candidates: list[dict[str, Any]] = []
    child_items: list[Item] = []
    seen_candidate_keys: set[tuple[str, str]] = set()
    for index, downloaded in enumerate(downloaded_candidates):
        candidate_key = (downloaded.byte_hash, downloaded.final_url)
        if candidate_key in seen_candidate_keys:
            continue
        seen_candidate_keys.add(candidate_key)
        candidate = downloaded.candidate
        order = candidate.order if candidate.order is not None else index
        title = (
            candidate.alt_text.strip()
            if candidate.alt_text and candidate.alt_text.strip()
            else f"Image from {parent_item.title}"
        )
        child_item = Item(
            source_type="image_candidate",
            source_url=None,
            title=title,
            status="captured",
            tenant_id=tenant_id,
            content_hash=None,
            metadata_={
                "browser_capture_image": {
                    "source": "browser_image_candidate",
                    "status": "captured_not_processed",
                    "parent_item_id": str(parent_item.id),
                    "source_post_url": normalized_url,
                    "candidate_url": downloaded.normalized_url,
                    "final_url": downloaded.final_url,
                    "media_type": downloaded.media_type,
                    "byte_hash": downloaded.byte_hash,
                    "byte_size": downloaded.byte_size,
                    "order": order,
                    "alt_text": candidate.alt_text,
                    "role": candidate.role,
                    "dimensions": {
                        "width": candidate.width,
                        "height": candidate.height,
                    },
                }
            },
        )
        db.add(child_item)
        await db.flush()
        child_items.append(child_item)
        linked_candidates.append(
            _linked_image_candidate_metadata(
                item_id=child_item.id,
                downloaded=downloaded,
                order=order,
            )
        )
    if linked_candidates:
        parent_item.metadata_ = {
            **(parent_item.metadata_ or {}),
            "browser_capture": {
                **((parent_item.metadata_ or {}).get("browser_capture") or {}),
                "image_candidates": linked_candidates,
            },
        }
        await db.commit()
    return linked_candidates, child_items


def _linked_image_candidate_metadata(
    *,
    item_id: Any,
    downloaded: _DownloadedImageCandidate,
    order: int,
) -> dict[str, Any]:
    return {
        "item_id": str(item_id),
        "candidate_url": downloaded.normalized_url,
        "final_url": downloaded.final_url,
        "media_type": downloaded.media_type,
        "byte_hash": downloaded.byte_hash,
        "byte_size": downloaded.byte_size,
        "order": order,
    }


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
    signing_key = request.state.key_hash if webhook_url else None
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
    if downloaded_image_candidates and normalized_url is not None:
        linked_image_candidates, linked_image_items = await _create_browser_image_items(
            db,
            parent_item=item,
            tenant_id=request.state.tenant_id,
            normalized_url=normalized_url,
            downloaded_candidates=downloaded_image_candidates,
        )
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
        for linked_image_item in linked_image_items:
            linked_image_item.status = "failed"
        await db.commit()
        raise HTTPException(status_code=503, detail="Capture enqueue failed; job marked failed for retry")

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
