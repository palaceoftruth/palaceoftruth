import uuid
from typing import Literal

from pydantic import BaseModel, Field

RetrievalModality = Literal["text", "image_description", "ocr_text", "image_native"]
RetrievalSupportLevel = Literal["strong", "weak", "unknown"]
RetrievalTrustClass = Literal[
    "raw_source",
    "curated_memory",
    "generated_synthesis",
    "broad_fallback",
    "stale_context",
    "low_support_generated",
]
RetrievalSourceSupportState = Literal["direct_source", "source_backed", "partial_source", "unsupported", "unknown"]
RetrievalFreshnessClass = Literal["fresh", "dated", "stale", "undated"]
RetrievalDerivedRawClass = Literal["raw", "curated", "derived", "fallback"]


class RetrievalProvenance(BaseModel):
    modality: RetrievalModality
    candidate_source: str
    support_level: RetrievalSupportLevel = "unknown"
    source_url: str | None = None
    source_label: str | None = None
    source_item_id: uuid.UUID | None = None
    source_span: dict | None = None
    original_artifact_url: str | None = None
    original_artifact_label: str | None = None
    media_type: str | None = None
    model: str | None = None
    provider: str | None = None
    confidence: float | None = None
    byte_hash: str | None = None
    notes: list[str] = Field(default_factory=list)


class RetrievalTrustMetadata(BaseModel):
    trust_class: RetrievalTrustClass
    source_support_state: RetrievalSourceSupportState
    freshness: RetrievalFreshnessClass
    derived_raw_classification: RetrievalDerivedRawClass
