"""Text extraction utilities for document types supported by /ingest/doc."""
import logging

logger = logging.getLogger(__name__)


def extract_docx(path: str) -> tuple[str, dict]:
    """Extract text from a .docx file.

    Returns (text, metadata) where metadata contains core_properties if available.
    """
    from docx import Document  # python-docx

    doc = Document(path)
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    text = "\n\n".join(paragraphs)

    metadata: dict = {}
    props = doc.core_properties
    if props.title:
        metadata["doc_title"] = props.title
    if props.author:
        metadata["doc_author"] = props.author

    return text, metadata


def extract_xlsx(path: str) -> tuple[str, dict]:
    """Extract text from all sheets of an .xlsx file, filtering empty rows.

    Returns (text, metadata) where text is a concatenation of all sheets as
    tab-separated rows, and metadata contains sheet names.
    """
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    parts: list[str] = []
    sheet_names: list[str] = []

    for sheet in wb.worksheets:
        sheet_names.append(sheet.title)
        rows: list[str] = []
        for row in sheet.iter_rows(values_only=True):
            cells = [str(c) if c is not None else "" for c in row]
            # Skip rows that are entirely empty
            if any(c.strip() for c in cells):
                rows.append("\t".join(cells))
        if rows:
            parts.append(f"[Sheet: {sheet.title}]\n" + "\n".join(rows))

    wb.close()
    text = "\n\n".join(parts)
    metadata = {"sheets": sheet_names}
    return text, metadata


def extract_text_file(path: str) -> tuple[str, dict]:
    """Read a plain-text file (.md, .txt) with UTF-8/latin-1 fallback."""
    for encoding in ("utf-8", "latin-1"):
        try:
            with open(path, encoding=encoding) as fh:
                text = fh.read()
            return text, {}
        except UnicodeDecodeError:
            continue
    logger.warning("extract_text_file: could not decode %s with any encoding", path)
    return "", {}
