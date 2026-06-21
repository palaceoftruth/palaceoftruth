from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx


class FirecrawlScrapeError(RuntimeError):
    """Raised when Firecrawl cannot return usable scrape content."""


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


def scrape_with_firecrawl(url: str, config: FirecrawlConfig) -> tuple[str | None, str, dict[str, Any]]:
    if not config.enabled:
        raise FirecrawlScrapeError("Firecrawl scraping is not enabled")

    headers = {"Content-Type": "application/json"}
    if config.auth_enabled:
        headers["Authorization"] = f"Bearer {config.api_key.strip()}"

    payload = {
        "url": url,
        "formats": ["markdown", "html"],
        "onlyMainContent": config.only_main_content,
    }
    try:
        response = httpx.post(
            config.scrape_endpoint,
            headers=headers,
            json=payload,
            timeout=config.timeout_seconds,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = _response_error_detail(exc.response)
        raise FirecrawlScrapeError(
            f"Firecrawl scrape failed with HTTP {exc.response.status_code}: {detail}"
        ) from exc
    except httpx.TimeoutException as exc:
        raise FirecrawlScrapeError(f"Firecrawl scrape timed out after {config.timeout_seconds:g}s") from exc
    except httpx.HTTPError as exc:
        raise FirecrawlScrapeError(f"Firecrawl scrape request failed: {exc}") from exc

    try:
        body = response.json()
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

    metadata = _metadata_from_response(data, config=config)
    html = data.get("html") if isinstance(data.get("html"), str) else None
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
            metadata[palace_key] = value
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
