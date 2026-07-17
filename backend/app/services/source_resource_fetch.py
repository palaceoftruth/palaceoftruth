"""Bounded conditional HTTP fetches for watched source resources.

This deliberately owns only network observation.  The worker is responsible for
the separate, transactional activation of changed content into an Item and its
append-only SourceRecord version.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx


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


async def fetch_http_resource(
    url: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    timeout_seconds: float = 30.0,
    client: httpx.AsyncClient | None = None,
) -> HttpRefreshResult:
    """GET a resource with validators; never fall back to a HEAD request."""

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    headers = {"User-Agent": "PalaceOfTruthSourceRefresh/1.0 (+https://palace.sarvent.cloud)"}
    if etag:
        headers["If-None-Match"] = etag
    elif last_modified:
        headers["If-Modified-Since"] = last_modified

    owns_client = client is None
    request_client = client or httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True)
    try:
        response = await request_client.get(url, headers=headers)
    except httpx.TimeoutException:
        return HttpRefreshResult("failure", None, failure_reason="timeout")
    except httpx.RequestError as exc:
        return HttpRefreshResult("failure", None, failure_reason=f"transport:{exc.__class__.__name__}")
    finally:
        if owns_client:
            await request_client.aclose()

    final_url = str(response.url)
    response_headers = response.headers
    if response.status_code == 304:
        return HttpRefreshResult(
            "not_modified",
            304,
            final_url=final_url,
            etag=response_headers.get("ETag") or etag,
            last_modified=response_headers.get("Last-Modified") or last_modified,
        )
    if response.status_code in {404, 410}:
        return HttpRefreshResult("gone", response.status_code, final_url=final_url, failure_reason=f"http_{response.status_code}")
    if response.status_code < 200 or response.status_code >= 300:
        return HttpRefreshResult("failure", response.status_code, final_url=final_url, failure_reason=f"http_{response.status_code}")
    return HttpRefreshResult(
        "success",
        response.status_code,
        final_url=final_url,
        body=response.content,
        etag=response_headers.get("ETag"),
        last_modified=response_headers.get("Last-Modified"),
    )
