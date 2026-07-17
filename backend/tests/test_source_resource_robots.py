import httpx
import pytest

from app.services.source_resource_robots import evaluate_robots, robots_url


def test_robots_url_keeps_only_origin() -> None:
    assert robots_url("https://example.test/path?q=1") == "https://example.test/robots.txt"


@pytest.mark.asyncio
async def test_disallowed_robots_denies_document_fetch() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="User-agent: PalaceOfTruthSourceRefresh\nDisallow: /private")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await evaluate_robots("https://example.test/private/document", client=client)

    assert result.allowed is False
    assert result.decision == "robots_disallowed"


@pytest.mark.asyncio
async def test_missing_robots_is_explicitly_allowed() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await evaluate_robots("https://example.test/document", client=client)

    assert result.allowed is True
    assert result.decision == "robots_missing"
