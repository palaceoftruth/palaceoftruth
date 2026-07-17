import httpx
import pytest

from app.services.source_resource_fetch import fetch_http_resource, parse_retry_after


@pytest.mark.asyncio
async def test_conditional_get_prefers_etag_and_maps_304_without_head() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(304, headers={"ETag": '"v2"'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch_http_resource(
            "https://example.test/document",
            etag='"v1"',
            last_modified="ignored",
            client=client,
        )

    assert result.outcome == "not_modified"
    assert result.status_code == 304
    assert result.etag == '"v2"'
    assert seen[0].method == "GET"
    assert seen[0].headers["if-none-match"] == '"v1"'
    assert "if-modified-since" not in seen[0].headers


@pytest.mark.asyncio
async def test_conditional_get_uses_last_modified_when_no_etag() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["if-modified-since"] == "Wed, 01 Jan 2025 00:00:00 GMT"
        return httpx.Response(200, content=b"changed", headers={"Last-Modified": "Thu, 02 Jan 2025 00:00:00 GMT"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch_http_resource(
            "https://example.test/document",
            last_modified="Wed, 01 Jan 2025 00:00:00 GMT",
            client=client,
        )

    assert result.outcome == "success"
    assert result.body == b"changed"
    assert result.last_modified == "Thu, 02 Jan 2025 00:00:00 GMT"


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "outcome"), [(404, "gone"), (410, "gone"), (429, "failure"), (503, "failure")])
async def test_non_success_responses_preserve_a_typed_outcome(status: int, outcome: str) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch_http_resource("https://example.test/document", client=client)

    assert result.outcome == outcome
    assert result.failure_reason == f"http_{status}"


@pytest.mark.asyncio
async def test_retry_after_is_preserved_for_bounded_worker_backoff() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "120"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch_http_resource("https://example.test/document", client=client)

    assert result.retry_after_seconds == 120
    assert parse_retry_after("not-a-date") is None
