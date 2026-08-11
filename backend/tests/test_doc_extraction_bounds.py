"""Extraction of untrusted uploads must be bounded and actually stoppable."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import time
import zipfile

import pytest

from app.services.doc_extraction import (
    DocumentExtractionError,
    DocumentExtractionTimeout,
    DocumentTooComplexError,
    extract_document_bounded,
)
from app.utils.doc_extract import (
    TRUNCATION_MARKER,
    extract_document,
    extract_text_file,
    extract_xlsx,
)


def test_text_extraction_stops_at_the_output_ceiling(tmp_path) -> None:
    path = tmp_path / "big.txt"
    path.write_text("a" * 10_000, encoding="utf-8")

    text, metadata = extract_text_file(str(path), max_chars=1_000)

    assert text == "a" * 1_000 + TRUNCATION_MARKER
    assert metadata["extraction_truncated"] is True


def test_text_extraction_under_the_ceiling_is_untouched(tmp_path) -> None:
    path = tmp_path / "small.txt"
    path.write_text("hello world", encoding="utf-8")

    text, metadata = extract_text_file(str(path), max_chars=1_000)

    assert text == "hello world"
    assert "extraction_truncated" not in metadata


def test_xlsx_extraction_stops_reading_a_highly_amplified_sheet(tmp_path) -> None:
    """A small zip container must not be able to produce unbounded output."""

    openpyxl = pytest.importorskip("openpyxl")
    path = tmp_path / "bomb.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    for _ in range(2_000):
        sheet.append(["x" * 200])
    workbook.save(path)

    text, metadata = extract_xlsx(str(path), max_chars=5_000)

    assert len(text) <= 5_000 + len(TRUNCATION_MARKER)
    assert metadata["extraction_truncated"] is True


def test_unsupported_extension_is_rejected_before_any_parser_runs(tmp_path) -> None:
    path = tmp_path / "payload.bin"
    path.write_bytes(b"\x00\x01")

    with pytest.raises(ValueError, match="Unsupported file extension"):
        extract_document(str(path), "payload.bin")


def test_bounded_extraction_returns_text_and_metadata(tmp_path) -> None:
    path = tmp_path / "note.txt"
    path.write_text("bounded body", encoding="utf-8")

    text, metadata = asyncio.run(
        extract_document_bounded(str(path), "note.txt", timeout_seconds=30)
    )

    assert text == "bounded body"
    assert metadata == {}


def test_bounded_extraction_surfaces_a_parse_failure_as_an_input_error(tmp_path) -> None:
    path = tmp_path / "not-really.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "not a docx at all")

    with pytest.raises(DocumentExtractionError):
        asyncio.run(extract_document_bounded(str(path), "not-really.docx", timeout_seconds=60))


def test_bounded_extraction_reports_a_child_killed_by_its_resource_limit(tmp_path) -> None:
    """A child that blows its memory or CPU ceiling is reported, not hung on."""

    path = tmp_path / "bomb.txt"
    path.write_text("body", encoding="utf-8")

    with pytest.raises(DocumentTooComplexError):
        asyncio.run(
            extract_document_bounded(
                str(path),
                "bomb.txt",
                timeout_seconds=30,
                # Limits this tight stand in for a decompression bomb without
                # needing to build one: the child cannot even start up.
                cpu_limit_seconds=0,
                memory_limit_bytes=1024,
            )
        )


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX FIFOs")
def test_bounded_extraction_kills_a_child_that_will_never_finish(tmp_path) -> None:
    """The timeout must stop the work, not merely stop waiting for it."""

    # Opening a FIFO with no writer blocks forever, which is the property under
    # test: the old executor-thread version could never be stopped.
    path = tmp_path / "never.txt"
    os.mkfifo(path)

    started = time.monotonic()
    with pytest.raises(DocumentExtractionTimeout):
        asyncio.run(extract_document_bounded(str(path), "never.txt", timeout_seconds=2))
    elapsed = time.monotonic() - started

    assert elapsed < 30
    assert not _any_live_child()


def _any_live_child() -> bool:
    """True while any extraction child is still running after the timeout."""

    return any(process.is_alive() for process in multiprocessing.active_children())
