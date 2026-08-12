"""Untrusted ingestion strings must be normalised on write (L-15)."""

import pytest

from app.ingest_sanitize import (
    MAX_FILENAME_LENGTH,
    MAX_TITLE_LENGTH,
    sanitize_embed_html,
    sanitize_filename,
    sanitize_summary,
    sanitize_title,
)


@pytest.mark.parametrize(
    "payload",
    [
        '<blockquote>hi</blockquote><script>alert(1)</script>',
        '<blockquote onclick="alert(1)">hi</blockquote>',
        '<blockquote>hi</blockquote><iframe src="https://evil.test"></iframe>',
        '<blockquote style="background:url(javascript:alert(1))">hi</blockquote>',
    ],
)
def test_embed_html_keeps_the_quote_and_drops_everything_active(payload: str) -> None:
    cleaned = sanitize_embed_html(payload)

    assert "hi" in cleaned
    for banned in ("script", "iframe", "onclick", "style"):
        assert banned not in cleaned.lower()


def test_embed_html_rejects_dangerous_link_schemes() -> None:
    cleaned = sanitize_embed_html(
        '<blockquote><a href="javascript:alert(1)">a</a>'
        '<a href="https://x.com/p">b</a></blockquote>'
    )

    assert "javascript:" not in cleaned
    assert "https://x.com/p" in cleaned
    # Outbound links must not hand the opener window to the target.
    assert "noopener" in cleaned


def test_embed_html_ignores_non_string_provider_values() -> None:
    assert sanitize_embed_html(None) == ""
    assert sanitize_embed_html(12) == ""
    assert sanitize_embed_html("   ") == ""


def test_title_is_flattened_to_text() -> None:
    assert sanitize_title("<b>Big</b> news") == "Big news"
    assert sanitize_title("Line\r\nbreak\ttab") == "Line break tab"


def test_title_does_not_leave_a_tag_behind_an_entity() -> None:
    """`&lt;script&gt;` decodes back into markup for any consumer that unescapes."""
    for payload in (
        "&lt;script&gt;alert(1)&lt;/script&gt;Report",
        "&amp;lt;script&amp;gt;alert(1)&amp;lt;/script&amp;gt;Report",
    ):
        cleaned = sanitize_title(payload)

        assert "<" not in cleaned
        assert "script" not in cleaned.lower()
        assert "Report" in cleaned


def test_title_and_summary_are_length_capped() -> None:
    assert len(sanitize_title("a" * 5000)) == MAX_TITLE_LENGTH
    assert len(sanitize_summary("a" * 50_000)) == 20_000


def test_title_of_a_missing_value_is_empty() -> None:
    assert sanitize_title(None) == ""
    assert sanitize_title("") == ""


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("report.pdf", "report.pdf"),
        ("../../etc/passwd", "passwd"),
        (r"C:\Users\me\notes.docx", "notes.docx"),
        ("  spaced  name.txt  ", "spaced name.txt"),
        ("..", ""),
        ("", ""),
    ],
)
def test_filename_is_reduced_to_a_safe_basename(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_filename_drops_bidirectional_override_characters() -> None:
    # U+202E makes "invoice\u202Efdp.exe" display as "invoice exe.pdf".
    cleaned = sanitize_filename("invoice\u202efdp.exe")

    assert "\u202e" not in cleaned
    assert cleaned == "invoicefdp.exe"


def test_filename_is_length_capped() -> None:
    cleaned = sanitize_filename("a" * 1000 + ".pdf")
    assert len(cleaned) == MAX_FILENAME_LENGTH
    assert cleaned.endswith(".pdf")
