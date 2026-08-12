"""One bounded, entity-free entry point for every untrusted XML document.

Parsing untrusted XML is a trust boundary in the same way that outbound HTTP
is.  Keeping the size cap and the declaration rejection in a single helper
means a newly added parser inherits the guard instead of re-deriving it.
"""

from __future__ import annotations

from xml.etree.ElementTree import Element

import defusedxml.ElementTree as DefusedElementTree
from defusedxml.common import DefusedXmlException

# Kept small and auditable on purpose: every XML document this service accepts
# is a feed, sitemap, or OPML outline, none of which need more than this.
MAX_XML_DOCUMENT_BYTES = 1_048_576


class UnsafeXmlError(ValueError):
    """Raised when an untrusted XML document violates the parsing guard."""


def checked_xml_bytes(body: bytes | str, *, max_bytes: int = MAX_XML_DOCUMENT_BYTES) -> bytes:
    """Return the document bytes only if they are safe to hand to a parser."""

    encoded = body.encode("utf-8") if isinstance(body, str) else bytes(body)
    if len(encoded) > max_bytes:
        raise UnsafeXmlError("XML document exceeds the maximum size")
    return encoded


def parse_safe_xml(
    body: bytes | str,
    *,
    max_bytes: int = MAX_XML_DOCUMENT_BYTES,
) -> Element:
    """Parse an untrusted XML document behind the size and declaration guard.

    ``defusedxml`` decodes the XML declaration before checking forbidden
    constructs, so UTF-16 and other supported encodings cannot bypass a raw
    UTF-8 substring check.
    """

    checked = checked_xml_bytes(body, max_bytes=max_bytes)
    try:
        return DefusedElementTree.fromstring(checked, forbid_dtd=True)
    except DefusedXmlException as exc:
        construct = "doctype" if type(exc).__name__ == "DTDForbidden" else "construct"
        raise UnsafeXmlError(f"XML document uses a forbidden {construct}: {exc}") from exc
    except DefusedElementTree.ParseError as exc:
        raise UnsafeXmlError(f"XML document is not valid XML: {exc}") from exc
