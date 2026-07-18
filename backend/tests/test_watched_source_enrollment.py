import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.models.item import Item
from app.models.web_save import WebSave
from app.services.source_resources import decide_alias
from app.services.watched_source_enrollment import candidate_from_web_save
from scripts import enroll_watched_sources


def _row(*, original_url: str = "https://example.com/story", normalized_url: str | None = None, archived: bool = False, source_type: str = "webpage"):
    item = Item(id=uuid.uuid4(), tenant_id="tenant-a", source_type=source_type, source_url=original_url, title="Story", status="ready")
    save = WebSave(id=uuid.uuid4(), tenant_id="tenant-a", item_id=item.id, original_url=original_url, normalized_url=normalized_url or original_url, capture_kind="webpage", archived_at=datetime.now(timezone.utc) if archived else None)
    return save, item


def test_candidate_is_normalized_without_reading_content() -> None:
    save, item = _row(original_url="HTTPS://Example.COM:443/story#fragment")
    candidate, reason = candidate_from_web_save(save, item)
    assert reason is None
    assert candidate is not None
    assert candidate.canonical_url == "https://example.com/story"
    assert candidate.domain == "example.com"


def test_candidate_excludes_archived_or_non_webpage_records() -> None:
    save, item = _row(archived=True)
    assert candidate_from_web_save(save, item) == (None, "archived_web_save")
    save, item = _row(source_type="note")
    assert candidate_from_web_save(save, item) == (None, "not_webpage")


def test_candidate_rejects_invalid_urls() -> None:
    save, item = _row(original_url="file:///private/source")
    assert candidate_from_web_save(save, item) == (None, "invalid_http_url")


def test_cursor_round_trip_is_opaque_and_rejects_invalid_input() -> None:
    cursor = enroll_watched_sources._encode_cursor(
        SimpleNamespace(id=uuid.UUID("00000000-0000-0000-0000-000000000010"), saved_at=datetime(2026, 7, 18, tzinfo=timezone.utc))
    )
    saved_at, web_save_id = enroll_watched_sources._decode_cursor(cursor)
    assert saved_at == datetime(2026, 7, 18, tzinfo=timezone.utc)
    assert web_save_id == uuid.UUID("00000000-0000-0000-0000-000000000010")
    with pytest.raises(ValueError, match="valid enrollment cursor"):
        enroll_watched_sources._decode_cursor("not-a-cursor")


@pytest.mark.asyncio
async def test_dry_run_report_is_aggregate_only_and_bounded_by_host() -> None:
    first_save, first_item = _row()
    second_save, second_item = _row(original_url="https://example.com/another")
    archived_save, archived_item = _row(archived=True)
    args = SimpleNamespace(tenant_id="tenant-a", per_host_limit=1, write=False)
    report = await enroll_watched_sources.enroll(args, [(first_save, first_item), (second_save, second_item), (archived_save, archived_item)], "opaque-next")
    assert report["mode"] == "dry_run"
    assert report["source_type"] == {"webpage": 3}
    assert report["candidate_policy"]["eligible_webpage"] == 2
    assert report["exclusion_reason"] == {"archived_web_save": 1, "per_host_limit": 1}
    assert report["domain"] == {"example.com": 2}
    assert report["selected_domain"] == {"example.com": 1}
    assert report["next_cursor"] == "opaque-next"
    assert str(first_save.id) not in str(report)


def test_cross_origin_original_alias_remains_a_conflict() -> None:
    save, item = _row(original_url="https://other.example/story", normalized_url="https://example.com/story")
    candidate, reason = candidate_from_web_save(save, item)
    assert reason is None
    assert candidate is not None
    assert candidate.original_url == "https://other.example/story"
    assert candidate.canonical_url == "https://example.com/story"
    alias = decide_alias(canonical_url=candidate.canonical_url, observed_url=candidate.original_url, signal="submitted")
    assert alias.decision == "conflict"
    assert alias.reason == "cross_origin_signal"
