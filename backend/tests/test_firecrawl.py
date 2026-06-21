import asyncio

import httpx
import pytest

from app.pipelines import webpage as webpage_module
from app.pipelines.webpage import WebpagePipeline
from app.services.firecrawl import (
    FirecrawlConfig,
    FirecrawlScrapeError,
    scrape_with_firecrawl,
)


def test_firecrawl_self_hosted_uses_configured_base_url_and_optional_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_post(url: str, *, headers: dict, json: dict, timeout: float) -> httpx.Response:
        seen.update({"url": url, "headers": headers, "json": json, "timeout": timeout})
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            request=request,
            json={
                "success": True,
                "data": {
                    "markdown": "# Captured\n\nFirecrawl body",
                    "html": "<h1>Captured</h1>",
                    "metadata": {
                        "title": "Captured",
                        "sourceURL": "https://example.test/article",
                        "statusCode": 200,
                    },
                },
            },
        )

    monkeypatch.setattr("app.services.firecrawl.httpx.post", fake_post)

    html, markdown, metadata = scrape_with_firecrawl(
        "https://example.test/article",
        FirecrawlConfig(
            provider="firecrawl-self-hosted",
            base_url="https://firecrawl.internal.example/v2/",
            timeout_seconds=12,
        ),
    )

    assert seen["url"] == "https://firecrawl.internal.example/v2/scrape"
    assert seen["headers"] == {"Content-Type": "application/json"}
    assert seen["json"] == {
        "url": "https://example.test/article",
        "formats": ["markdown", "html"],
        "onlyMainContent": True,
    }
    assert seen["timeout"] == 12
    assert html == "<h1>Captured</h1>"
    assert markdown == "# Captured\n\nFirecrawl body"
    assert metadata["content_source"] == "firecrawl"
    assert metadata["firecrawl_provider"] == "firecrawl-self-hosted"
    assert metadata["title"] == "Captured"
    assert metadata["source_url"] == "https://example.test/article"


def test_firecrawl_cloud_adds_bearer_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_headers: dict[str, str] = {}

    def fake_post(_url: str, *, headers: dict, json: dict, timeout: float) -> httpx.Response:
        seen_headers.update(headers)
        request = httpx.Request("POST", _url)
        return httpx.Response(200, request=request, json={"success": True, "data": {"markdown": "Cloud body"}})

    monkeypatch.setattr("app.services.firecrawl.httpx.post", fake_post)

    scrape_with_firecrawl(
        "https://example.test/article",
        FirecrawlConfig(
            provider="firecrawl-cloud",
            base_url="https://api.firecrawl.dev/v2",
            api_key="fc-secret",
        ),
    )

    assert seen_headers["Authorization"] == "Bearer fc-secret"


def test_firecrawl_surfaces_http_error_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(_url: str, *, headers: dict, json: dict, timeout: float) -> httpx.Response:
        request = httpx.Request("POST", "https://firecrawl.internal.example/v2/scrape")
        return httpx.Response(429, json={"error": "rate limited"}, request=request)

    monkeypatch.setattr("app.services.firecrawl.httpx.post", fake_post)

    with pytest.raises(FirecrawlScrapeError, match="HTTP 429: rate limited"):
        scrape_with_firecrawl(
            "https://example.test/article",
            FirecrawlConfig(provider="firecrawl-self-hosted", base_url="https://firecrawl.internal.example/v2"),
        )


def test_webpage_pipeline_uses_firecrawl_before_local_article_scraper(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_firecrawl(url: str, config: FirecrawlConfig):
        assert url == "https://example.test/article"
        assert config.provider == "firecrawl-self-hosted"
        return "<h1>Article</h1>", "Article body from Firecrawl", {"title": "Article"}

    def fail_scrape(_url: str):
        raise AssertionError("local scraper should not run when Firecrawl succeeds")

    monkeypatch.setattr(webpage_module, "scrape_with_firecrawl", fake_firecrawl)
    monkeypatch.setattr(WebpagePipeline, "_scrape", staticmethod(fail_scrape))

    pipeline = WebpagePipeline(
        db=None,
        embedder=None,
        llm=None,
        firecrawl_config=FirecrawlConfig(
            provider="firecrawl-self-hosted",
            base_url="https://firecrawl.internal.example/v2",
        ),
    )
    text, metadata = asyncio.run(pipeline.extract("https://example.test/article"))

    assert text == "Article body from Firecrawl"
    assert metadata["title"] == "Article"
    assert metadata["domain"] == "example.test"
    assert metadata["estimated_read_time_minutes"] == 1
