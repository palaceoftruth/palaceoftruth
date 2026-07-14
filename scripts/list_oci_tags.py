#!/usr/bin/env python3
"""List every tag in one OCI repository without dropping paginated results."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Protocol


NEXT_LINK_RE = re.compile(r'<([^>]+)>\s*;[^,]*\brel="?next"?', re.IGNORECASE)
HELM_SEMVER_TAG_RE = re.compile(
    r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:[+_][0-9A-Za-z.-]+)?$"
)


class Response(Protocol):
    headers: object

    def __enter__(self) -> "Response": ...

    def __exit__(self, *args: object) -> None: ...

    def read(self) -> bytes: ...


def _next_page_url(current_url: str, link_header: str | None) -> str | None:
    if not link_header:
        return None
    match = NEXT_LINK_RE.search(link_header)
    if not match:
        return None
    next_url = urllib.parse.urljoin(current_url, match.group(1))
    current = urllib.parse.urlparse(current_url)
    following = urllib.parse.urlparse(next_url)
    if (following.scheme, following.netloc) != (current.scheme, current.netloc):
        raise ValueError(f"OCI pagination changed origin: {next_url}")
    if following.path != current.path:
        raise ValueError(f"OCI pagination changed repository path: {next_url}")
    return next_url


def list_oci_tags(
    tags_url: str,
    bearer_token: str,
    *,
    opener: Callable[..., Response] = urllib.request.urlopen,
) -> list[str]:
    if not bearer_token:
        raise ValueError("OCI bearer token is required")

    tags: set[str] = set()
    seen_urls: set[str] = set()
    next_url: str | None = tags_url
    while next_url:
        if next_url in seen_urls:
            raise ValueError(f"OCI pagination repeated a page: {next_url}")
        seen_urls.add(next_url)

        request = urllib.request.Request(
            next_url,
            headers={"Authorization": f"Bearer {bearer_token}"},
        )
        with opener(request, timeout=30) as response:
            payload = json.loads(response.read())
            if not isinstance(payload, dict) or "tags" not in payload:
                raise ValueError("OCI registry returned an invalid tags payload")
            page_tags = payload["tags"] or []
            if not isinstance(page_tags, list) or not all(
                isinstance(tag, str) for tag in page_tags
            ):
                raise ValueError("OCI registry returned an invalid tags payload")
            tags.update(page_tags)
            link_header = response.headers.get("Link")
        next_url = _next_page_url(next_url, link_header)

    return sorted(tags)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--token-env", default="OCI_BEARER_TOKEN")
    parser.add_argument(
        "--semver-only",
        action="store_true",
        help="Print only tags that can represent Helm semantic chart versions.",
    )
    args = parser.parse_args()

    token = os.environ.get(args.token_env, "")
    repository = urllib.parse.quote(args.repository.strip("/"), safe="/")
    tags_url = f"https://{args.registry}/v2/{repository}/tags/list?n=100"
    try:
        tags = list_oci_tags(tags_url, token)
        if args.semver_only:
            ignored_tags = [tag for tag in tags if not HELM_SEMVER_TAG_RE.fullmatch(tag)]
            if ignored_tags:
                print(
                    "Ignoring non-SemVer OCI tags: " + ", ".join(ignored_tags),
                    file=sys.stderr,
                )
            tags = [tag for tag in tags if HELM_SEMVER_TAG_RE.fullmatch(tag)]
        for tag in tags:
            print(tag)
    except (ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
