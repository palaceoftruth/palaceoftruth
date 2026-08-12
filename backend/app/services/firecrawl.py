from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from app.ingest_sanitize import sanitize_summary, sanitize_title
from app.utils.outbound_http import OutboundUrlError, Resolver, validate_public_http_url


class FirecrawlScrapeError(RuntimeError):
    """Raised when Firecrawl cannot return usable scrape content."""


MAX_FIRECRAWL_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_FIRECRAWL_MARKDOWN_CHARS = 5_000_000
MAX_FIRECRAWL_HTML_CHARS = 8_000_000


@dataclass(frozen=True)
class FirecrawlConfig:
    provider: str
    base_url: str
    api_key: str = ""
    timeout_seconds: float = 60.0
    only_main_content: bool = True

    @property
    def enabled(self) -> bool:
        return self.provider in {"firecrawl-cloud", "firecrawl-self-hosted"}

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_key.strip())

    @property
    def scrape_endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/scrape"


def firecrawl_config_from_settings(settings: Any) -> FirecrawlConfig:
    return FirecrawlConfig(
        provider=str(settings.webpage_scraper_provider),
        base_url=str(settings.firecrawl_base_url),
        api_key=str(settings.firecrawl_api_key or ""),
        timeout_seconds=float(settings.firecrawl_timeout_seconds),
        only_main_content=bool(settings.firecrawl_only_main_content),
    )


def scrape_with_firecrawl(
    url: str,
    config: FirecrawlConfig,
    *,
    resolver: Resolver | None = None,
    client: httpx.Client | None = None,
) -> tuple[str | None, str, dict[str, Any]]:
    if not config.enabled:
        raise FirecrawlScrapeError("Firecrawl scraping is not enabled")

    # Firecrawl fetches this URL itself, so the caller's earlier validation is
    # not enough: re-resolve immediately before handing the URL over and fail
    # closed on any non-public address. Firecrawl still resolves independently,
    # which is why the workload egress policy remains load-bearing here.
    try:
        url = validate_public_http_url(url, resolver=resolver)
    except OutboundUrlError as exc:
        raise FirecrawlScrapeError(f"Firecrawl scrape target is not a permitted URL: {exc}") from exc

    headers = {"Content-Type": "application/json"}
    if config.auth_enabled:
        headers["Authorization"] = f"Bearer {config.api_key.strip()}"

    payload = {
        "url": url,
        "formats": ["markdown", "html"],
        "onlyMainContent": config.only_main_content,
    }
    owns_client = client is None
    request_client = client or httpx.Client(timeout=config.timeout_seconds)
    try:
        with request_client.stream(
            "POST",
            config.scrape_endpoint,
            headers=headers,
            json=payload,
            timeout=config.timeout_seconds,
        ) as response:
            declared_length = response.headers.get("Content-Length")
            if declared_length is not None:
                try:
                    if int(declared_length) > MAX_FIRECRAWL_RESPONSE_BYTES:
                        raise FirecrawlScrapeError("Firecrawl scrape response exceeded the size limit")
                except ValueError:
                    pass
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > MAX_FIRECRAWL_RESPONSE_BYTES:
                    raise FirecrawlScrapeError("Firecrawl scrape response exceeded the size limit")
            status_code = response.status_code
            reason_phrase = response.reason_phrase
        if status_code >= 400:
            error_response = httpx.Response(
                status_code,
                content=bytes(content),
                request=httpx.Request("POST", config.scrape_endpoint),
            )
            detail = _response_error_detail(error_response)
            if detail == "unknown Firecrawl error":
                detail = reason_phrase
            raise FirecrawlScrapeError(
                f"Firecrawl scrape failed with HTTP {status_code}: {detail}"
            )
    except FirecrawlScrapeError:
        raise
    except httpx.TimeoutException as exc:
        raise FirecrawlScrapeError(f"Firecrawl scrape timed out after {config.timeout_seconds:g}s") from exc
    except httpx.HTTPError as exc:
        raise FirecrawlScrapeError(f"Firecrawl scrape request failed: {exc}") from exc
    finally:
        if owns_client:
            request_client.close()

    try:
        body = httpx.Response(200, content=bytes(content)).json()
    except ValueError as exc:
        raise FirecrawlScrapeError("Firecrawl scrape returned non-JSON response") from exc

    if body.get("success") is False:
        raise FirecrawlScrapeError(f"Firecrawl scrape failed: {_payload_error_detail(body)}")

    data = body.get("data")
    if not isinstance(data, dict):
        raise FirecrawlScrapeError("Firecrawl scrape response did not include data")

    markdown = data.get("markdown")
    if not isinstance(markdown, str) or not markdown.strip():
        raise FirecrawlScrapeError("Firecrawl scrape response did not include markdown content")
    if len(markdown) > MAX_FIRECRAWL_MARKDOWN_CHARS:
        raise FirecrawlScrapeError("Firecrawl markdown exceeded the size limit")

    metadata = _metadata_from_response(data, config=config)
    html = data.get("html") if isinstance(data.get("html"), str) else None
    if html is not None and len(html) > MAX_FIRECRAWL_HTML_CHARS:
        raise FirecrawlScrapeError("Firecrawl HTML exceeded the size limit")
    return html, markdown.strip(), metadata


def _metadata_from_response(data: dict[str, Any], *, config: FirecrawlConfig) -> dict[str, Any]:
    raw_metadata = data.get("metadata")
    source_metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    metadata: dict[str, Any] = {
        "content_source": "firecrawl",
        "firecrawl_provider": config.provider,
        "scraped_with": "firecrawl",
    }
    for firecrawl_key, palace_key in (
        ("title", "title"),
        ("author", "author"),
        ("description", "description"),
        ("language", "language"),
        ("sourceURL", "source_url"),
        ("url", "canonical_url"),
        ("statusCode", "http_status_code"),
        ("contentType", "content_type"),
    ):
        value = source_metadata.get(firecrawl_key)
        if value not in (None, ""):
            metadata[palace_key] = (
                sanitize_summary(value)
                if palace_key == "description"
                else sanitize_title(value)
                if palace_key in {"title", "author"}
                else value
            )
    published_at = source_metadata.get("publishedTime") or source_metadata.get("date")
    if published_at:
        metadata["date"] = str(published_at)
        metadata["published_at"] = str(published_at)
    warning = data.get("warning")
    if isinstance(warning, str) and warning.strip():
        metadata["firecrawl_warning"] = warning[:500]
    source_url = metadata.get("source_url") or metadata.get("canonical_url")
    if isinstance(source_url, str):
        try:
            metadata["domain"] = urlparse(source_url).netloc
        except Exception:
            pass
    return metadata


def _response_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        text = response.text.strip()
        return text[:300] if text else response.reason_phrase
    return _payload_error_detail(payload)


def _payload_error_detail(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("error", "message", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:300]
        nested = payload.get("data")
        if isinstance(nested, dict):
            metadata = nested.get("metadata")
            if isinstance(metadata, dict):
                value = metadata.get("error")
                if isinstance(value, str) and value.strip():
                    return value.strip()[:300]
    return "unknown Firecrawl error"
