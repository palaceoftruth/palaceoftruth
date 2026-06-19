from pydantic import BaseModel, Field


class ArtifactCitation(BaseModel):
    kind: str
    thumbnail_url: str | None = None
    caption: str | None = None
    extracted_text: list[str] = Field(default_factory=list)
    source_url: str | None = None
    source_label: str | None = None
    original_artifact_url: str | None = None
    original_artifact_label: str | None = None
    filename: str | None = None
    media_type: str | None = None
    dimensions: dict[str, int | None] | None = None
    model: str | None = None
    provider: str | None = None
    confidence: float | None = None
    byte_hash: str | None = None
