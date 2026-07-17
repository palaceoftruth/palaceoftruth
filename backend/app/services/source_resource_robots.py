"""Robots policy checks for the bounded watched-resource fetcher."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx


@dataclass(frozen=True)
class RobotsDecision:
    allowed: bool
    decision: str


def robots_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, "/robots.txt", "", ""))


async def evaluate_robots(
    url: str,
    *,
    user_agent: str = "PalaceOfTruthSourceRefresh",
    timeout_seconds: float = 10.0,
    client: httpx.AsyncClient | None = None,
) -> RobotsDecision:
    """Fail closed when robots cannot be checked; absent robots allows crawling."""

    owns_client = client is None
    request_client = client or httpx.AsyncClient(timeout=timeout_seconds)
    try:
        response = await request_client.get(robots_url(url), headers={"User-Agent": user_agent})
    except httpx.RequestError:
        return RobotsDecision(False, "robots_unavailable")
    finally:
        if owns_client:
            await request_client.aclose()
    if response.status_code == 404:
        return RobotsDecision(True, "robots_missing")
    if response.status_code < 200 or response.status_code >= 300:
        return RobotsDecision(False, f"robots_http_{response.status_code}")
    parser = RobotFileParser()
    parser.parse(response.text.splitlines())
    return RobotsDecision(parser.can_fetch(user_agent, url), "robots_allowed" if parser.can_fetch(user_agent, url) else "robots_disallowed")
