from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, TypedDict
from urllib.parse import unquote, urlparse

_DATE_KEY_PRECEDENCE = (
    ("memory_entry.created_at", "memory_metadata", "high"),
    ("memory_contract.created_at", "memory_metadata", "high"),
    ("frontmatter.date", "frontmatter", "high"),
    ("frontmatter.published", "frontmatter", "high"),
    ("frontmatter.published_at", "frontmatter", "high"),
    ("source_date", "metadata", "high"),
    ("event_date", "metadata", "high"),
    ("published_at", "published_metadata", "high"),
    ("published", "published_metadata", "medium"),
)
_FILENAME_DATE_RE = re.compile(
    r"(?<!\d)(?P<year>20\d{2}|19\d{2})[-_/]?(?P<month>0[1-9]|1[0-2])[-_/]?(?P<day>0[1-9]|[12]\d|3[01])(?!\d)"
)


@dataclass(frozen=True)
class EffectiveDate:
    value: datetime
    source: str
    quality: str


def _nested_value(metadata: dict[str, Any], path: str) -> Any:
    current: Any = metadata
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(candidate)
            except (TypeError, ValueError, IndexError, OverflowError):
                return None
    else:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_filename_date(source_url: str | None, metadata: dict[str, Any]) -> datetime | None:
    candidates = [
        metadata.get("sync_relative_path"),
        metadata.get("upload_artifact", {}).get("filename")
        if isinstance(metadata.get("upload_artifact"), dict)
        else None,
        source_url,
    ]
    for raw_candidate in candidates:
        if not raw_candidate:
            continue
        parsed = urlparse(str(raw_candidate))
        path = unquote(parsed.path or str(raw_candidate))
        name = Path(path).name or path
        match = _FILENAME_DATE_RE.search(name)
        if not match:
            continue
        try:
            return datetime(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
                tzinfo=timezone.utc,
            )
        except ValueError:
            continue
    return None


def resolve_effective_date(
    *,
    metadata: dict[str, Any] | None,
    source_url: str | None = None,
    fallback_created_at: datetime | None = None,
) -> EffectiveDate | None:
    """Choose the content/event date used for temporal retrieval ranking."""
    metadata = metadata or {}
    for key, source, quality in _DATE_KEY_PRECEDENCE:
        parsed = _parse_datetime(_nested_value(metadata, key))
        if parsed is not None:
            return EffectiveDate(parsed, source, quality)

    filename_date = _parse_filename_date(source_url, metadata)
    if filename_date is not None:
        return EffectiveDate(filename_date, "source_filename", "medium")

    parsed_fallback = _parse_datetime(fallback_created_at)
    if parsed_fallback is not None:
        return EffectiveDate(parsed_fallback, "created_at_fallback", "low")
    return None


def apply_effective_date(
    item: Any,
    *,
    metadata: dict[str, Any] | None = None,
    fallback_created_at: datetime | None = None,
) -> None:
    effective = resolve_effective_date(
        metadata=metadata if metadata is not None else getattr(item, "metadata_", None),
        source_url=getattr(item, "source_url", None),
        fallback_created_at=fallback_created_at or getattr(item, "created_at", None),
    )
    if effective is None:
        return
    item.effective_date = effective.value
    item.effective_date_source = effective.source
    item.effective_date_quality = effective.quality


class SourceDateEvidence(TypedDict, total=False):
    published_at: str
    event_at: str
    captured_at: str


def source_date_evidence(metadata: dict[str, Any] | None) -> SourceDateEvidence:
    """Return explicit source dates without ranking fallbacks or invented timezone.

    A diary filename, generic frontmatter.date, memory creation and effective_date
    do not establish publication/capture/event dates. Preserve date-only precision.
    """
    if not isinstance(metadata, dict):
        return {}
    result: SourceDateEvidence = {}
    for label, paths in (
        ("published_at", ("published_at", "frontmatter.published_at", "frontmatter.published")),
        ("event_at", ("event_at", "event_date")),
        ("captured_at", ("captured_at",)),
    ):
        for path in paths:
            value = _nested_value(metadata, path)
            if isinstance(value, datetime):
                value = value.isoformat()
            if not isinstance(value, str) or len(value) > 40 or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))?", value
            ):
                continue
            try:
                datetime.fromisoformat(value)
            except ValueError:
                continue
            result[label] = value
            break
    return result
