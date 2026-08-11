"""Bounded conditional HTTP fetches for watched source resources.

This deliberately owns only network observation.  The worker is responsible for
the separate, transactional activation of changed content into an Item and its
append-only SourceRecord version.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from app.utils.outbound_http import (
    OutboundUrlError,
    stream_public_http_async,
    validate_public_http_url,
)
from app.utils.safe_xml import MAX_XML_DOCUMENT_BYTES

# One shared ceiling for every document this service fetches or parses, so a
# legitimate-looking external host cannot return an unbounded body.
MAX_RESOURCE_BODY_BYTES = MAX_XML_DOCUMENT_BYTES


@dataclass(frozen=True)
class HttpRefreshResult:
    """A secret-free, serializable result of one conditional document GET."""

    outcome: str
    status_code: int | None
    final_url: str | None = None
    body: bytes | None = None
    etag: str | None = None
    last_modified: str | None = None
    failure_reason: str | None = None
    retry_after_seconds: int | None = None
    redirect_url: str | None = None


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> int | None:
    """Parse a positive Retry-After value without trusting unbounded delays."""

    if not value:
        return None
    try:
        seconds = int(value)
    except ValueError:
        try:
            target = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if target.tzinfo is None:
            return None
        seconds = int((target - (now or datetime.now(timezone.utc))).total_seconds())
    return max(0, seconds)


async def fetch_http_resource(
    url: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    timeout_seconds: float = 30.0,
    client: httpx.AsyncClient | None = None,
    trusted_exact_hosts: tuple[str, ...] = (),
    max_body_bytes: int = MAX_RESOURCE_BODY_BYTES,
) -> HttpRefreshResult:
    """GET a resource with validators; never fall back to a HEAD request."""

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if max_body_bytes <= 0:
        raise ValueError("max_body_bytes must be positive")

    headers = {"User-Agent": "PalaceOfTruthSourceRefresh/1.0 (+https://palace.sarvent.cloud)"}
    if etag:
        headers["If-None-Match"] = etag
    elif last_modified:
        headers["If-Modified-Since"] = last_modified

    owns_client = client is None
    # Redirects are followed by the worker one hop at a time so each target
    # receives its own robots and source-identity check before it is fetched.
    request_client = client or httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False)
    try:
        # Stream rather than buffer: the body is read one chunk at a time and
        # abandoned past the ceiling, so a host that returns an endless or
        # multi-gigabyte response cannot exhaust the worker's memory.
        if owns_client:
            stream = stream_public_http_async(
                request_client,
                "GET",
                url,
                headers=headers,
                follow_redirects=False,
                trusted_exact_hosts=trusted_exact_hosts,
            )
        else:
            safe_url = validate_public_http_url(
                url,
                resolve=False,
                trusted_exact_hosts=trusted_exact_hosts,
            )
            stream = request_client.stream(
                "GET", safe_url, headers=headers, follow_redirects=False
            )

        async with stream as response:
            status_code = response.status_code
            response_headers = response.headers
            declared_length = _declared_content_length(response_headers)
            if declared_length is not None and declared_length > max_body_bytes:
                # Reject before reading a single byte when the host tells us.
                return HttpRefreshResult(
                    "failure",
                    status_code,
                    failure_reason="body_too_large",
                )
            body = await _read_capped_body(response, max_body_bytes) if _wants_body(status_code) else None
            if body is _TOO_LARGE:
                return HttpRefreshResult("failure", status_code, failure_reason="body_too_large")
    except OutboundUrlError as exc:
        return HttpRefreshResult("failure", None, failure_reason=f"unsafe_url:{exc}")
    except httpx.TimeoutException:
        return HttpRefreshResult("failure", None, failure_reason="timeout")
    except httpx.RequestError as exc:
        return HttpRefreshResult("failure", None, failure_reason=f"transport:{exc.__class__.__name__}")
    finally:
        if owns_client:
            await request_client.aclose()

    # A pinned request's transport URL contains the selected IP. Preserve the
    # canonical caller URL in observations and redirect resolution.
    final_url = validate_public_http_url(
        url,
        resolve=False,
        trusted_exact_hosts=trusted_exact_hosts,
    )
    if 300 <= status_code < 400 and response_headers.get("Location"):
        return HttpRefreshResult(
            "redirect",
            status_code,
            final_url=final_url,
            redirect_url=str(httpx.URL(final_url).join(response_headers["Location"])),
        )
    if status_code == 304:
        return HttpRefreshResult(
            "not_modified",
            304,
            final_url=final_url,
            etag=response_headers.get("ETag") or etag,
            last_modified=response_headers.get("Last-Modified") or last_modified,
        )
    if status_code == 404:
        # The worker requires a repeated observation before tombstoning a
        # resource; one transient 404 only enters bounded retry/backoff.
        return HttpRefreshResult("not_found", 404, final_url=final_url, failure_reason="http_404")
    if status_code == 410:
        return HttpRefreshResult("gone", status_code, final_url=final_url, failure_reason=f"http_{status_code}")
    if status_code < 200 or status_code >= 300:
        return HttpRefreshResult(
            "failure",
            status_code,
            final_url=final_url,
            failure_reason=f"http_{status_code}",
            retry_after_seconds=parse_retry_after(response_headers.get("Retry-After")),
        )
    return HttpRefreshResult(
        "success",
        status_code,
        final_url=final_url,
        body=body,
        etag=response_headers.get("ETag"),
        last_modified=response_headers.get("Last-Modified"),
    )


# Sentinel distinguishing "read stopped at the cap" from "no body was wanted".
_TOO_LARGE = object()


def _wants_body(status_code: int) -> bool:
    """Only a successful response contributes content to a source version."""

    return 200 <= status_code < 300 and status_code != 204


def _declared_content_length(headers: httpx.Headers) -> int | None:
    try:
        return int(headers["Content-Length"])
    except (KeyError, TypeError, ValueError):
        return None


async def _read_capped_body(response: httpx.Response, max_body_bytes: int):
    """Read the body, abandoning it as soon as it crosses the ceiling."""

    content = bytearray()
    async for chunk in response.aiter_bytes():
        content.extend(chunk)
        if len(content) > max_body_bytes:
            return _TOO_LARGE
    return bytes(content)
