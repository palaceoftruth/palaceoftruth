from datetime import datetime, timezone
from types import SimpleNamespace

from app.services.item_dates import apply_effective_date, resolve_effective_date


def test_resolve_effective_date_prefers_memory_metadata_created_at() -> None:
    effective = resolve_effective_date(
        metadata={
            "memory_entry": {"created_at": "2026-05-08T12:00:00Z"},
            "published": "2026-05-07T12:00:00Z",
        },
        source_url=None,
    )

    assert effective is not None
    assert effective.value == datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    assert effective.source == "memory_metadata"
    assert effective.quality == "high"


def test_resolve_effective_date_uses_published_metadata_then_filename() -> None:
    published = resolve_effective_date(
        metadata={"published": "Tue, 05 May 2026 16:59:13 +0000"},
        source_url="https://example.com/archive/2026-04-01-note.md",
    )
    filename = resolve_effective_date(
        metadata={},
        source_url="https://example.com/archive/2026-04-01-note.md",
    )

    assert published is not None
    assert published.value == datetime(2026, 5, 5, 16, 59, 13, tzinfo=timezone.utc)
    assert published.source == "published_metadata"
    assert filename is not None
    assert filename.value == datetime(2026, 4, 1, tzinfo=timezone.utc)
    assert filename.source == "source_filename"
    assert filename.quality == "medium"


def test_apply_effective_date_records_source_and_quality() -> None:
    item = SimpleNamespace(
        source_url=None,
        metadata_={"frontmatter": {"date": "2026-05-01"}},
        created_at=datetime(2026, 5, 9, tzinfo=timezone.utc),
    )

    apply_effective_date(item)

    assert item.effective_date == datetime(2026, 5, 1, tzinfo=timezone.utc)
    assert item.effective_date_source == "frontmatter"
    assert item.effective_date_quality == "high"
