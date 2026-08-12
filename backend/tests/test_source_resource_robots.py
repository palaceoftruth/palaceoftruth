import httpx
import pytest
import socket

from app.services.source_resource_robots import evaluate_robots, robots_url


def _resolver(address: str = "93.184.216.34"):
    def resolve(_host: str, _port, *, type: int):
        assert type == socket.SOCK_STREAM
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]

    return resolve


def test_robots_url_keeps_only_origin() -> None:
    assert robots_url("https://example.test/path?q=1") == "https://example.test/robots.txt"


@pytest.mark.asyncio
async def test_disallowed_robots_denies_document_fetch() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="User-agent: PalaceOfTruthSourceRefresh\nDisallow: /private")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await evaluate_robots(
            "https://example.test/private/document", client=client, resolver=_resolver()
        )

    assert result.allowed is False
    assert result.decision == "robots_disallowed"


@pytest.mark.asyncio
async def test_missing_robots_is_explicitly_allowed() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await evaluate_robots(
            "https://example.test/document", client=client, resolver=_resolver()
        )

    assert result.allowed is True
    assert result.decision == "robots_missing"


@pytest.mark.asyncio
async def test_private_literal_robots_requires_an_exact_trusted_host() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        blocked = await evaluate_robots(
            "http://private.test/source", client=client, resolver=_resolver("10.42.0.31")
        )
        allowed = await evaluate_robots(
            "http://10.42.0.31/source",
            client=client,
            resolver=_resolver("10.42.0.31"),
            trusted_exact_hosts=("10.42.0.31",),
        )

    assert blocked == type(blocked)(False, "robots_unsafe_url")
    assert allowed == type(allowed)(True, "robots_missing")
