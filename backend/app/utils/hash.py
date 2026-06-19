import hashlib
import re


def compute_content_hash(text: str) -> str:
    """SHA-256 of lowercased, whitespace-collapsed text.

    Used for content-level deduplication across all ingestion pipelines.
    Normalizing before hashing ensures the same article ingested via different
    URLs (or re-uploaded as a PDF) produces an identical fingerprint.
    """
    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    return hashlib.sha256(normalized.encode()).hexdigest()
