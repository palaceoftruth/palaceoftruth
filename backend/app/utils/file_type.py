"""Content-based file type checks for uploaded and served artifacts.

A file name extension and a multipart Content-Type header are both attacker
controlled. Everything that decides how a byte stream is parsed, or how a
browser is told to render it, is therefore derived from the bytes themselves.
"""

from __future__ import annotations

import zipfile

# Enough bytes for every signature checked here, and enough text for a useful
# UTF-8 decode test on a plain text upload.
SNIFF_BYTES = 4096


class FileTypeError(ValueError):
    """The bytes do not match the type the caller declared."""


# Extension -> the media type an artifact of that extension is served with.
# Nothing outside this map is ever returned as a media type.
SAFE_MEDIA_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".md": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

_FALLBACK_MEDIA_TYPE = "application/octet-stream"

# Extensions whose content is one of the OOXML zip containers. The required
# member prefix separates a Word package from an Excel package, so the
# extension cannot be used to route bytes into the wrong parser.
_OOXML_MEMBER_PREFIXES = {".docx": "word/", ".xlsx": "xl/"}

_MAGIC_PREFIXES: dict[str, tuple[bytes, ...]] = {
    ".pdf": (b"%PDF-",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".gif": (b"GIF87a", b"GIF89a"),
}

_TEXT_EXTS = frozenset({".md", ".txt"})


def safe_media_type(extension: str | None) -> str:
    """Map a verified extension to a media type that is safe to serve."""

    if not extension:
        return _FALLBACK_MEDIA_TYPE
    return SAFE_MEDIA_TYPES.get(extension.lower(), _FALLBACK_MEDIA_TYPE)


def matches_extension(head: bytes, extension: str) -> bool:
    """Test the leading bytes of a file against one declared extension.

    Only the signature is checked here. Zip containers need the archive
    directory as well, so use verify_file_type() for a file on disk.
    """

    ext = extension.lower()

    prefixes = _MAGIC_PREFIXES.get(ext)
    if prefixes is not None:
        return head.startswith(prefixes)

    if ext == ".webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"

    if ext in _OOXML_MEMBER_PREFIXES:
        # "PK\x03\x04" is a local file header; the empty and spanned variants
        # cannot hold an OOXML part.
        return head.startswith(b"PK\x03\x04")

    if ext in _TEXT_EXTS:
        if b"\x00" in head:
            return False
        try:
            # A truncated multi-byte character at the sniff boundary is not a
            # failure, so decode errors are only fatal for a complete head.
            head.decode("utf-8")
        except UnicodeDecodeError as exc:
            return exc.start >= len(head) - 4
        return True

    return False


def verify_file_type(path: str, extension: str) -> None:
    """Raise FileTypeError when a file on disk contradicts its extension."""

    ext = (extension or "").lower()
    if ext not in SAFE_MEDIA_TYPES:
        raise FileTypeError(f"Unsupported file extension: {extension!r}")

    try:
        with open(path, "rb") as handle:
            head = handle.read(SNIFF_BYTES)
    except OSError as exc:
        raise FileTypeError(f"The uploaded file could not be read: {exc}") from exc

    if not head:
        raise FileTypeError("The uploaded file is empty")

    if not matches_extension(head, ext):
        raise FileTypeError(f"File content does not match the {ext} extension")

    member_prefix = _OOXML_MEMBER_PREFIXES.get(ext)
    if member_prefix is not None:
        _verify_ooxml_package(path, ext, member_prefix)


def _verify_ooxml_package(path: str, extension: str, member_prefix: str) -> None:
    """Confirm a zip container really holds the expected Office package."""

    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
    except (OSError, zipfile.BadZipFile) as exc:
        raise FileTypeError(f"File is not a readable {extension} package: {exc}") from exc

    if not any(name.startswith(member_prefix) for name in names):
        raise FileTypeError(f"File content does not match the {extension} extension")
