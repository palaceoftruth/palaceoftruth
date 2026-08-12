import zipfile
from pathlib import Path

import pytest

from app.utils.file_type import (
    FileTypeError,
    MAX_OOXML_ENTRIES,
    matches_extension,
    safe_media_type,
    verify_file_type,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 16
PDF_BYTES = b"%PDF-1.7\n1 0 obj\n"


def _docx(path: Path, member: str) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(member, "<x/>")
    return path


def test_verify_accepts_matching_content(tmp_path: Path) -> None:
    pdf = tmp_path / "brief.pdf"
    pdf.write_bytes(PDF_BYTES)
    verify_file_type(str(pdf), ".pdf")

    png = tmp_path / "shot.png"
    png.write_bytes(PNG_BYTES)
    verify_file_type(str(png), ".png")

    text = tmp_path / "note.md"
    text.write_text("# heading\nwith unicode: é\n", encoding="utf-8")
    verify_file_type(str(text), ".md")


def test_verify_rejects_a_renamed_executable(tmp_path: Path) -> None:
    payload = tmp_path / "invoice.pdf"
    payload.write_bytes(b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 64)

    with pytest.raises(FileTypeError, match="does not match"):
        verify_file_type(str(payload), ".pdf")


def test_verify_rejects_html_disguised_as_an_image(tmp_path: Path) -> None:
    payload = tmp_path / "logo.png"
    payload.write_bytes(b"<html><script>alert(1)</script></html>")

    with pytest.raises(FileTypeError, match="does not match"):
        verify_file_type(str(payload), ".png")


def test_verify_rejects_binary_content_for_a_text_extension(tmp_path: Path) -> None:
    payload = tmp_path / "notes.txt"
    payload.write_bytes(b"text\x00\x01\x02binary")

    with pytest.raises(FileTypeError, match="does not match"):
        verify_file_type(str(payload), ".txt")


def test_verify_rejects_an_empty_file(tmp_path: Path) -> None:
    payload = tmp_path / "empty.pdf"
    payload.write_bytes(b"")

    with pytest.raises(FileTypeError, match="empty"):
        verify_file_type(str(payload), ".pdf")


def test_verify_requires_the_right_ooxml_package(tmp_path: Path) -> None:
    """A Word package must not be routed into the spreadsheet parser."""

    docx = _docx(tmp_path / "report.docx", "word/document.xml")
    verify_file_type(str(docx), ".docx")

    mislabelled = _docx(tmp_path / "report.xlsx", "word/document.xml")
    with pytest.raises(FileTypeError, match="does not match"):
        verify_file_type(str(mislabelled), ".xlsx")


def test_verify_rejects_a_plain_zip_named_as_an_office_file(tmp_path: Path) -> None:
    archive_path = tmp_path / "data.xlsx"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("payload.sh", "#!/bin/sh\n")

    with pytest.raises(FileTypeError, match="does not match"):
        verify_file_type(str(archive_path), ".xlsx")


def test_verify_rejects_ooxml_with_excessive_central_directory_entries(tmp_path: Path) -> None:
    archive_path = tmp_path / "bomb.docx"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("word/document.xml", "<document/>")
        for index in range(MAX_OOXML_ENTRIES):
            archive.writestr(f"padding/{index}", "")

    with pytest.raises(FileTypeError, match="too many archive entries"):
        verify_file_type(str(archive_path), ".docx")


def test_verify_rejects_an_extension_outside_the_allowlist(tmp_path: Path) -> None:
    payload = tmp_path / "run.svg"
    payload.write_bytes(b"<svg/>")

    with pytest.raises(FileTypeError, match="Unsupported file extension"):
        verify_file_type(str(payload), ".svg")


def test_safe_media_type_never_returns_an_active_type() -> None:
    assert safe_media_type(".png") == "image/png"
    assert safe_media_type(".md").startswith("text/plain")
    assert safe_media_type(".svg") == "application/octet-stream"
    assert safe_media_type(None) == "application/octet-stream"


def test_matches_extension_tolerates_a_split_utf8_character() -> None:
    head = ("a" * 4090).encode("utf-8") + "é".encode("utf-8")[:1]
    assert matches_extension(head, ".txt")
