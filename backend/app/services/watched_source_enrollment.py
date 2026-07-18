"""Safe, bounded promotion of saved webpages into source resources.

This module deliberately does not fetch URLs or schedule refreshes.  It turns a
selected WebSave into a ``manual`` SourceResource so a later operator policy can
enable watching without re-discovering historical browser captures.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from app.models.item import Item
from app.models.web_save import WebSave
from app.services.source_resources import canonical_http_identity, normalize_http_url


@dataclass(frozen=True)
class EnrollmentCandidate:
    web_save_id: str
    tenant_id: str
    original_url: str
    canonical_url: str
    canonical_identity: str
    domain: str


def candidate_from_web_save(web_save: WebSave, item: Item) -> tuple[EnrollmentCandidate | None, str | None]:
    """Return an eligible candidate or a stable, non-sensitive exclusion reason."""

    if web_save.archived_at is not None:
        return None, "archived_web_save"
    if item.deleted_at is not None or item.status == "deleted":
        return None, "deleted_item"
    if web_save.capture_kind != "webpage" or item.source_type != "webpage":
        return None, "not_webpage"
    raw_url = web_save.normalized_url or item.source_url or web_save.original_url
    try:
        canonical_url = normalize_http_url(raw_url)
        original_url = normalize_http_url(web_save.original_url)
    except ValueError:
        return None, "invalid_http_url"
    domain = urlsplit(canonical_url).hostname or "unknown"
    return EnrollmentCandidate(
        web_save_id=str(web_save.id),
        tenant_id=web_save.tenant_id,
        original_url=original_url,
        canonical_url=canonical_url,
        canonical_identity=canonical_http_identity(canonical_url),
        domain=domain,
    ), None
