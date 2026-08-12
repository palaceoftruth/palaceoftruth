from __future__ import annotations

import socket

import httpx
import pytest

from app.utils.outbound_http import (
    OutboundUrlError,
    ValidatedHttpTarget,
    fetch_public_http_bytes,
    request_public_http_async,
    validate_public_http_url,
)


def _resolver(*addresses: str):
    def resolve(_host: str, _port, *, type: int):
        assert type == socket.SOCK_STREAM
        return [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (address, 0, 0, 0) if ":" in address else (address, 0),
            )
            for address in addresses
        ]

    return resolve


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://10.1.2.3/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[fd00::1]/",
        "http://[::1]/",
    ],
)
def test_rejects_non_public_ip_literals(url: str) -> None:
    with pytest.raises(OutboundUrlError):
        validate_public_http_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://user:password@example.com/",
        "https://example.com\\@127.0.0.1/",
        " https://example.com/",
        "https://example.com:invalid/",
        "https:///missing-host",
    ],
)
def test_rejects_malformed_or_ambiguous_urls(url: str) -> None:
    with pytest.raises(OutboundUrlError):
        validate_public_http_url(url, resolver=_resolver("93.184.216.34"))


def test_rejects_mixed_public_and_private_dns_answers() -> None:
    with pytest.raises(OutboundUrlError):
        validate_public_http_url(
            "https://mixed.example/resource",
            resolver=_resolver("93.184.216.34", "10.0.0.8"),
        )


def test_private_dns_requires_an_exact_operator_trusted_host() -> None:
    resolver = _resolver("10.42.0.31")

    with pytest.raises(OutboundUrlError):
        validate_public_http_url(
            "http://palace-source-canary.palace-sarvent.svc.cluster.local/",
            resolver=resolver,
        )
    with pytest.raises(OutboundUrlError):
        validate_public_http_url(
            "http://other.palace-sarvent.svc.cluster.local/",
            resolver=resolver,
            trusted_exact_hosts=(
                "palace-source-canary.palace-sarvent.svc.cluster.local",
            ),
        )

    assert validate_public_http_url(
        "http://palace-source-canary.palace-sarvent.svc.cluster.local/",
        resolver=resolver,
        trusted_exact_hosts=(
            "palace-source-canary.palace-sarvent.svc.cluster.local",
        ),
    ) == "http://palace-source-canary.palace-sarvent.svc.cluster.local/"


def test_accepts_only_global_dns_answers() -> None:
    assert (
        validate_public_http_url(
            "HTTPS://PUBLIC.EXAMPLE/path?x=1",
            resolver=_resolver("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"),
        )
        == "https://public.example/path?x=1"
    )


def test_redirect_to_private_address_is_rejected_before_second_request() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/secret"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OutboundUrlError):
            fetch_public_http_bytes(
                "https://public.example/start",
                client=client,
                resolver=_resolver("93.184.216.34"),
            )

    assert seen == ["https://93.184.216.34/start"]


def test_injected_client_cannot_bypass_private_hostname_resolution() -> None:
    def fail_request(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("private hostname must be rejected before transport")

    with httpx.Client(transport=httpx.MockTransport(fail_request)) as client:
        with pytest.raises(OutboundUrlError, match="private"):
            fetch_public_http_bytes(
                "https://internal.example/secret",
                client=client,
                resolver=_resolver("10.0.0.8"),
            )


@pytest.mark.asyncio
async def test_async_request_pins_ip_and_preserves_host_and_sni(monkeypatch) -> None:
    import app.utils.outbound_http as outbound_http

    monkeypatch.setattr(
        outbound_http,
        "resolve_public_http_target",
        lambda _url, **_kwargs: ValidatedHttpTarget(
            "https://public.example/hook",
            "public.example",
            __import__("ipaddress").ip_address("93.184.216.34"),
        ),
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await request_public_http_async(
            client,
            "POST",
            "https://public.example/hook",
            content=b"{}",
        )

    assert response.status_code == 204
    assert str(seen[0].url) == "https://93.184.216.34/hook"
    assert seen[0].headers["Host"] == "public.example"
    assert seen[0].extensions["sni_hostname"] == "public.example"


@pytest.mark.asyncio
async def test_async_request_formats_ipv6_literal_host_header(monkeypatch) -> None:
    import app.utils.outbound_http as outbound_http

    public_ipv6 = "2606:2800:220:1:248:1893:25c8:1946"
    monkeypatch.setattr(
        outbound_http,
        "resolve_public_http_target",
        lambda _url, **_kwargs: ValidatedHttpTarget(
            f"https://[{public_ipv6}]:8443/resource",
            public_ipv6,
            __import__("ipaddress").ip_address(public_ipv6),
        ),
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await request_public_http_async(
            client,
            "GET",
            f"https://[{public_ipv6}]:8443/resource",
        )

    assert seen[0].headers["Host"] == f"[{public_ipv6}]:8443"
