"""Pure, bounded discovery helpers for watched HTTP sources.

Discovery is intentionally separate from fetching and enrollment: callers must
provide the already-fetched document and an explicit host allowlist.  This keeps
the first canary incapable of crawling the web, subscribing to WebSub, or
creating a resource merely because a document advertised a link.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from io import BytesIO
from typing import Iterable, Literal
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

import feedparser

from app.services.source_resources import normalize_http_url


SourceClass = Literal["webpage", "feed", "sitemap"]


@dataclass(frozen=True)
class SourceClassPolicy:
    """Safe default interval and adaptive bounds for an enrolled source class."""

    refresh_slo: timedelta
    minimum_interval: timedelta
    maximum_interval: timedelta


SOURCE_CLASS_POLICIES: dict[SourceClass, SourceClassPolicy] = {
    "webpage": SourceClassPolicy(timedelta(hours=24), timedelta(hours=1), timedelta(days=7)),
    "feed": SourceClassPolicy(timedelta(minutes=30), timedelta(minutes=5), timedelta(hours=6)),
    "sitemap": SourceClassPolicy(timedelta(hours=12), timedelta(hours=1), timedelta(days=3)),
}

# Parsing is a canary boundary as much as outbound fetching is. Keep a single
# document below a small, auditable limit before handing it to either parser.
MAX_DISCOVERY_DOCUMENT_BYTES = 1_048_576


@dataclass(frozen=True)
class DiscoveryCandidate:
    """A candidate that still requires explicit enrollment before use."""

    url: str
    source_class: SourceClass
    provenance: Literal["feed_entry", "sitemap_url"]


@dataclass(frozen=True)
class DiscoveryResult:
    candidates: tuple[DiscoveryCandidate, ...]
    advertised_websub_hubs: tuple[str, ...]
    rejected: int


def _allowed_hosts(hosts: Iterable[str]) -> set[str]:
    normalized = {host.strip().lower() for host in hosts if host.strip()}
    if not normalized:
        raise ValueError("allowed_hosts must not be empty")
    return normalized


def _checked_body(body: bytes | str) -> bytes:
    encoded = body.encode("utf-8") if isinstance(body, str) else body
    if len(encoded) > MAX_DISCOVERY_DOCUMENT_BYTES:
        raise ValueError("discovery document exceeds the maximum size")
    # The standard library parser does not fetch external entities, but reject
    # declarations outright so this safety property is explicit and testable.
    if b"<!DOCTYPE" in encoded.upper() or b"<!ENTITY" in encoded.upper():
        raise ValueError("discovery XML declarations are not allowed")
    return encoded


def _allowed_url(raw_url: str, *, base_url: str, allowed_hosts: set[str]) -> str | None:
    try:
        candidate = normalize_http_url(urljoin(base_url, raw_url))
    except ValueError:
        return None
    if (urlsplit(candidate).hostname or "").lower() not in allowed_hosts:
        return None
    return candidate


def _append_candidate(
    candidates: list[DiscoveryCandidate],
    seen: set[str],
    *,
    url: str | None,
    base_url: str,
    allowed_hosts: set[str],
    source_class: SourceClass,
    provenance: Literal["feed_entry", "sitemap_url"],
    max_candidates: int,
) -> bool:
    if url is None or len(candidates) >= max_candidates:
        return False
    normalized = _allowed_url(url, base_url=base_url, allowed_hosts=allowed_hosts)
    if normalized is None or normalized in seen:
        return False
    seen.add(normalized)
    candidates.append(DiscoveryCandidate(normalized, source_class, provenance))
    return True


def discover_feed_candidates(
    *,
    feed_url: str,
    body: bytes | str,
    allowed_hosts: Iterable[str],
    max_candidates: int = 50,
) -> DiscoveryResult:
    """Extract allowlisted feed-entry links without any network side effect.

    WebSub hubs are returned only as evidence.  The canary never subscribes to
    them, which avoids opening callbacks or granting a third party a trigger.
    """

    if max_candidates <= 0:
        raise ValueError("max_candidates must be positive")
    host_set = _allowed_hosts(allowed_hosts)
    normalized_feed = _allowed_url(feed_url, base_url=feed_url, allowed_hosts=host_set)
    if normalized_feed is None:
        raise ValueError("feed_url must be an allowed HTTP URL")

    parsed = feedparser.parse(_checked_body(body))
    candidates: list[DiscoveryCandidate] = []
    seen: set[str] = set()
    rejected = 0
    for entry in parsed.entries:
        link = entry.get("link")
        if not _append_candidate(
            candidates, seen, url=link, base_url=normalized_feed, allowed_hosts=host_set,
            source_class="webpage", provenance="feed_entry", max_candidates=max_candidates,
        ):
            rejected += 1

    hubs: list[str] = []
    for link in parsed.feed.get("links", []):
        if link.get("rel") == "hub":
            hub = _allowed_url(link.get("href", ""), base_url=normalized_feed, allowed_hosts=host_set)
            if hub is not None and hub not in hubs:
                hubs.append(hub)
            elif hub is None:
                rejected += 1
    return DiscoveryResult(tuple(candidates), tuple(hubs), rejected)


def discover_sitemap_candidates(
    *,
    sitemap_url: str,
    body: bytes | str,
    allowed_hosts: Iterable[str],
    max_candidates: int = 50,
) -> DiscoveryResult:
    """Extract URL-set entries from a supplied sitemap with strict host/cap bounds."""

    if max_candidates <= 0:
        raise ValueError("max_candidates must be positive")
    host_set = _allowed_hosts(allowed_hosts)
    normalized_sitemap = _allowed_url(sitemap_url, base_url=sitemap_url, allowed_hosts=host_set)
    if normalized_sitemap is None:
        raise ValueError("sitemap_url must be an allowed HTTP URL")
    stream = BytesIO(_checked_body(body))

    candidates: list[DiscoveryCandidate] = []
    seen: set[str] = set()
    rejected = 0
    try:
        elements = ElementTree.iterparse(stream, events=("end",))
        for _, element in elements:
            if element.tag.rsplit("}", 1)[-1] != "loc":
                continue
            accepted = _append_candidate(
                candidates, seen, url=element.text, base_url=normalized_sitemap, allowed_hosts=host_set,
                source_class="webpage", provenance="sitemap_url", max_candidates=max_candidates,
            )
            if not accepted:
                rejected += 1
            if len(candidates) == max_candidates:
                # Do not continue walking an attacker-controlled sitemap once
                # the caller's explicit canary cap is satisfied.
                break
            element.clear()
    except ElementTree.ParseError as exc:
        raise ValueError("sitemap is not valid XML") from exc
    return DiscoveryResult(tuple(candidates), (), rejected)
