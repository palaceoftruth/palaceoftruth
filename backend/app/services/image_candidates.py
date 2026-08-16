"""Bounded, provenance-preserving downloads for browser image candidates."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse, urlsplit, urlunparse

import httpx

from app.schemas.ingest import BrowserImageCandidate
from app.utils.file_type import SNIFF_BYTES, matches_extension
from app.utils.outbound_http import (
    OutboundUrlError,
    stream_public_http_async,
    validate_public_http_url,
    validate_public_http_url_async,
)

MAX_IMAGE_CANDIDATES = 4
IMAGE_SIZE_LIMIT = 8 * 1024 * 1024
REDIRECT_LIMIT = 3
HTTP_TIMEOUT = 10.0
IMAGE_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

SOCIAL_IMAGE_HOST_SUFFIXES: dict[str, tuple[str, ...]] = {
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


class ImageCandidateError(ValueError):
    """A candidate failed validation or bounded download."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 422,
        code: str = "invalid_candidate",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class DownloadedImageCandidate:
    candidate: BrowserImageCandidate
    normalized_url: str
    final_url: str
    media_type: str
    extension: str
    content: bytes
    byte_size: int
    byte_hash: str


def normalize_http_url(value: str | None, *, detail: str = "Invalid URL") -> str:
    if value is None or not value.strip():
        raise ImageCandidateError(detail)
    raw = value.strip()
    try:
        parsed = urlsplit(raw)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise ValueError
        if parsed.username is not None or parsed.password is not None:
            raise ImageCandidateError("URL must not include credentials")
        # Reject control/whitespace characters before handing the URL to a
        # parser/client. Public URL validation also rejects these forms, but
        # doing it here gives candidate metadata deterministic normalization.
        if any(character.isspace() or ord(character) < 0x20 for character in raw):
            raise ValueError
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
    except ImageCandidateError:
        raise
    except (TypeError, ValueError) as exc:
        raise ImageCandidateError(detail) from exc


def host_matches(hostname: str, suffix: str) -> bool:
    return hostname == suffix or hostname.endswith(f".{suffix}")


def allowed_image_host_suffixes(source_url: str) -> tuple[str, ...]:
    source_host = urlparse(source_url).hostname or ""
    for source_suffix, image_suffixes in SOCIAL_IMAGE_HOST_SUFFIXES.items():
        if host_matches(source_host, source_suffix):
            return image_suffixes
    return ()


def validate_candidate_relationship(
    *, candidate: BrowserImageCandidate, source_url: str, resolved_kind: str
) -> str:
    if resolved_kind != "social_post":
        raise ImageCandidateError("image_candidates are only supported for social_post captures")
    normalized_source = normalize_http_url(source_url, detail="Invalid capture URL")
    normalized_candidate = normalize_http_url(candidate.url, detail="Invalid image candidate URL")
    try:
        safe_url = validate_public_http_url(normalized_candidate, resolve=False)
    except OutboundUrlError as exc:
        raise ImageCandidateError("image candidate host is not allowed") from exc
    candidate_host = urlparse(safe_url).hostname or ""
    suffixes = allowed_image_host_suffixes(normalized_source)
    if not suffixes or not any(host_matches(candidate_host, suffix) for suffix in suffixes):
        raise ImageCandidateError("image candidate host is not allowed for source post")
    if candidate.source_post_url is not None:
        candidate_source = normalize_http_url(
            candidate.source_post_url, detail="Invalid image candidate source_post_url"
        )
        if candidate_source != normalized_source:
            raise ImageCandidateError("image candidate source_post_url must match capture url")
    return normalized_candidate


async def download_image_candidate(
    *,
    client: httpx.AsyncClient,
    candidate: BrowserImageCandidate,
    normalized_candidate_url: str,
    source_url: str,
) -> DownloadedImageCandidate:
    """Download one candidate while validating every DNS and redirect hop."""

    current_url = normalized_candidate_url
    for _redirect in range(REDIRECT_LIMIT + 1):
        try:
            safe_url = await validate_public_http_url_async(current_url)
        except OutboundUrlError as exc:
            raise ImageCandidateError("image candidate host is not allowed") from exc
        image_host = urlparse(safe_url).hostname or ""
        suffixes = allowed_image_host_suffixes(source_url)
        if not suffixes or not any(host_matches(image_host, suffix) for suffix in suffixes):
            raise ImageCandidateError("image candidate host is not allowed for source post")
        try:
            response_context = stream_public_http_async(
                client, "GET", safe_url, follow_redirects=False
            )
            async with response_context as response:
                if response.is_redirect:
                    if _redirect >= REDIRECT_LIMIT:
                        raise ImageCandidateError("image candidate redirected too many times")
                    location = response.headers.get("location")
                    if not location:
                        raise ImageCandidateError("image candidate redirect missing location")
                    current_url = normalize_http_url(
                        urljoin(safe_url, location), detail="Invalid image candidate redirect URL"
                    )
                    continue
                if response.status_code >= 400:
                    if response.status_code == 429:
                        raise ImageCandidateError(
                            "image candidate host rate limited the request",
                            code="rate_limited",
                            retryable=True,
                        )
                    if response.status_code >= 500:
                        raise ImageCandidateError(
                            "image candidate host returned a server error",
                            code="server_error",
                            retryable=True,
                        )
                    raise ImageCandidateError("image candidate returned an error")
                media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if media_type not in IMAGE_MEDIA_TYPES:
                    raise ImageCandidateError("image candidate content type is not allowed")
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared_size = int(content_length)
                    except ValueError as exc:
                        raise ImageCandidateError("image candidate content length is invalid") from exc
                    if declared_size < 0:
                        raise ImageCandidateError("image candidate content length is invalid")
                    if declared_size < 0:
                        raise ImageCandidateError("image candidate content length is invalid")
                    if declared_size > IMAGE_SIZE_LIMIT:
                        raise ImageCandidateError("image candidate is too large", status_code=413)
                content_buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    content_buffer.extend(chunk)
                    if len(content_buffer) > IMAGE_SIZE_LIMIT:
                        raise ImageCandidateError("image candidate is too large", status_code=413)
                content = bytes(content_buffer)
                final_url = safe_url
                # Validate the final URL after reading the response too. This
                # makes the returned provenance safe even with custom transports.
                await validate_public_http_url_async(final_url)
        except ImageCandidateError:
            raise
        except httpx.HTTPError as exc:
            raise ImageCandidateError(
                "image candidate could not be downloaded",
                code="network_error",
                retryable=True,
            ) from exc
        except OutboundUrlError as exc:
            raise ImageCandidateError("image candidate URL is not allowed") from exc
        extension = IMAGE_EXTENSIONS[media_type]
        if not matches_extension(content[:SNIFF_BYTES], extension):
            raise ImageCandidateError("image candidate bytes do not match content type")
        return DownloadedImageCandidate(
            candidate=candidate,
            normalized_url=normalized_candidate_url,
            final_url=final_url,
            media_type=media_type,
            extension=extension,
            content=content,
            byte_size=len(content),
            byte_hash=hashlib.sha256(content).hexdigest(),
        )
    raise ImageCandidateError("image candidate redirected too many times")
