import uuid
from typing import Literal

from pydantic import BaseModel, Field

RetrievalModality = Literal["text", "image_description", "ocr_text", "image_native"]
RetrievalSupportLevel = Literal["strong", "weak", "unknown"]


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
