"""Validation and bounded fetching for untrusted outbound HTTP targets."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

Resolver = Callable[..., Iterable[tuple]]

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_DEFAULT_MAX_BYTES = 10 * 1024 * 1024
_DEFAULT_USER_AGENT = "PalaceOfTruth/1.0 (+https://palace.sarvent.cloud)"


class OutboundUrlError(ValueError):
    """Raised when an untrusted URL is not safe for outbound access."""


@dataclass(frozen=True)
class ValidatedHttpTarget:
    """A validated URL plus the exact address selected for the connection."""

    url: str
    host: str
    address: ipaddress.IPv4Address | ipaddress.IPv6Address


def _normalized_http_url(url: str) -> tuple[str, str]:
    if not isinstance(url, str) or not url or url != url.strip():
        raise OutboundUrlError("URL must be a non-empty string without surrounding whitespace")
    if any(character.isspace() or ord(character) < 0x20 for character in url):
        raise OutboundUrlError("URL must not contain whitespace or control characters")

    try:
        parsed = urlsplit(url)
        port = parsed.port
        host = parsed.hostname
    except ValueError as exc:
        raise OutboundUrlError("URL is malformed") from exc

    if parsed.scheme.lower() not in {"http", "https"}:
        raise OutboundUrlError("URL must use http or https")
    if not parsed.netloc or not host:
        raise OutboundUrlError("URL must include a valid host")
    if parsed.username is not None or parsed.password is not None:
        raise OutboundUrlError("URL must not include credentials")

    normalized_host = host.lower().rstrip(".")
    if not normalized_host or normalized_host in {"localhost", "localhost.localdomain"}:
        raise OutboundUrlError("URL must not target an internal host")
    try:
        ascii_host = normalized_host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise OutboundUrlError("URL host is malformed") from exc

    # Rebuild the authority from parsed components. This rejects ambiguous
    # backslash/userinfo forms instead of relying on downstream client parsing.
    display_host = f"[{ascii_host}]" if ":" in ascii_host else ascii_host
    authority = f"{display_host}:{port}" if port is not None else display_host
    normalized = urlunsplit(
        (parsed.scheme.lower(), authority, parsed.path or "/", parsed.query, parsed.fragment)
    )
    return normalized, ascii_host


def _resolved_addresses(
    host: str,
    *,
    resolver: Resolver | None = None,
) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        try:
            answers = (resolver or socket.getaddrinfo)(host, None, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise OutboundUrlError("URL host could not be resolved") from exc
        addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
        for answer in answers:
            try:
                addresses.add(ipaddress.ip_address(answer[4][0]))
            except (IndexError, TypeError, ValueError):
                continue
        if not addresses:
            raise OutboundUrlError("URL host did not resolve to an IP address")
        return addresses
    return {literal}


def validate_public_http_url(
    url: str,
    *,
    resolve: bool = True,
    resolver: Resolver | None = None,
) -> str:
    """Return a normalized URL only when every resolved address is globally routable."""

    normalized, host = _normalized_http_url(url)
    if not resolve:
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            return normalized
        addresses = {literal}
    else:
        addresses = _resolved_addresses(host, resolver=resolver)

    if any(not address.is_global for address in addresses):
        raise OutboundUrlError("URL must not target private, loopback, link-local, or reserved addresses")
    return normalized


def parse_http_url(url: str) -> str:
    """Strictly parse an HTTP URL without applying public-network policy."""

    normalized, _host = _normalized_http_url(url)
    return normalized


def resolve_public_http_target(
    url: str,
    *,
    resolver: Resolver | None = None,
) -> ValidatedHttpTarget:
    """Resolve once and select a public address that callers can connect to directly."""

    normalized, host = _normalized_http_url(url)
    addresses = _resolved_addresses(host, resolver=resolver)
    if any(not address.is_global for address in addresses):
        raise OutboundUrlError("URL must not target private, loopback, link-local, or reserved addresses")
    # Stable selection makes behavior predictable while still failing closed if
    # even one mixed DNS answer points at a non-public destination.
    address = sorted(addresses, key=lambda item: (item.version, int(item)))[0]
    return ValidatedHttpTarget(normalized, host, address)


async def validate_public_http_url_async(url: str, *, resolve: bool = True) -> str:
    """Asynchronously resolve and validate an outbound URL."""

    return await asyncio.to_thread(validate_public_http_url, url, resolve=resolve)


def _pinned_request_parts(target: ValidatedHttpTarget) -> tuple[str, str, dict[str, str]]:
    parsed = urlsplit(target.url)
    address_host = f"[{target.address}]" if target.address.version == 6 else str(target.address)
    port = f":{parsed.port}" if parsed.port is not None else ""
    connect_url = urlunsplit(
        (parsed.scheme, f"{address_host}{port}", parsed.path, parsed.query, "")
    )
    default_port = 443 if parsed.scheme == "https" else 80
    original_host = f"[{target.host}]" if ":" in target.host else target.host
    host_header = (
        original_host
        if parsed.port in (None, default_port)
        else f"{original_host}:{parsed.port}"
    )
    return connect_url, host_header, {"sni_hostname": target.host}


async def request_public_http_async(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    **kwargs,
) -> httpx.Response:
    """Issue one DNS-pinned request while preserving HTTP Host and TLS SNI."""

    target = await asyncio.to_thread(resolve_public_http_target, url)
    connect_url, host_header, extensions = _pinned_request_parts(target)
    headers = httpx.Headers(kwargs.pop("headers", None))
    headers["Host"] = host_header
    return await client.request(
        method,
        connect_url,
        headers=headers,
        extensions=extensions,
        **kwargs,
    )


@asynccontextmanager
async def stream_public_http_async(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    **kwargs,
):
    """Stream one DNS-pinned response while preserving HTTP Host and TLS SNI."""

    target = await asyncio.to_thread(resolve_public_http_target, url)
    connect_url, host_header, extensions = _pinned_request_parts(target)
    headers = httpx.Headers(kwargs.pop("headers", None))
    headers["Host"] = host_header
    async with client.stream(
        method,
        connect_url,
        headers=headers,
        extensions=extensions,
        **kwargs,
    ) as response:
        yield response


def fetch_public_http_bytes(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout_seconds: float = 30.0,
    max_redirects: int = 5,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    raise_for_status: bool = True,
    client: httpx.Client | None = None,
) -> tuple[bytes, httpx.Response]:
    """Fetch bytes while validating the initial target and every redirect hop."""

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    owns_client = client is None
    request_client = client or httpx.Client(
        timeout=timeout_seconds,
        follow_redirects=False,
        # Pinned IP origins must never pool a TLS connection across hostnames
        # that happen to resolve to the same address.
        limits=httpx.Limits(max_keepalive_connections=0),
    )
    current_url = url
    request_headers = {"User-Agent": _DEFAULT_USER_AGENT, **(headers or {})}
    try:
        for hop in range(max_redirects + 1):
            if owns_client:
                target = resolve_public_http_target(current_url)
                request_url, host_header, extensions = _pinned_request_parts(target)
                current_url = target.url
                hop_headers = {**request_headers, "Host": host_header}
            else:
                # Supplying a client is an explicit test/transport injection
                # point. Keep parsing strict without bypassing MockTransport.
                current_url = validate_public_http_url(current_url, resolve=False)
                request_url = current_url
                hop_headers = request_headers
                extensions = None
            with request_client.stream(
                "GET",
                request_url,
                headers=hop_headers,
                follow_redirects=False,
                extensions=extensions,
            ) as response:
                if response.status_code in _REDIRECT_STATUSES and response.headers.get("Location"):
                    if hop >= max_redirects:
                        raise OutboundUrlError("Too many redirects")
                    current_url = urljoin(current_url, response.headers["Location"])
                    continue
                if raise_for_status:
                    response.raise_for_status()
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > max_bytes:
                        raise OutboundUrlError("Outbound response exceeded the size limit")
                return bytes(content), response
        raise OutboundUrlError("Too many redirects")
    finally:
        if owns_client:
            request_client.close()
