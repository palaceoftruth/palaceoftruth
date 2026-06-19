import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


def _validate_model_not_blank(v: str | None) -> str | None:
    if v is not None and v.strip() == "":
        raise ValueError("model must not be blank")
    return v


class IngestMediaRequest(BaseModel):
    url: str
    webhook_url: str | None = None
    model: str | None = None

    @field_validator("model")
    @classmethod
    def model_not_blank(cls, v: str | None) -> str | None:
        return _validate_model_not_blank(v)


# Backward-compat alias
IngestYouTubeRequest = IngestMediaRequest


class IngestWebpageRequest(BaseModel):
    url: str
    webhook_url: str | None = None
    model: str | None = None

    @field_validator("model")
    @classmethod
    def model_not_blank(cls, v: str | None) -> str | None:
        return _validate_model_not_blank(v)


class IngestNoteRequest(BaseModel):
    title: str
    content: str
    tags: list[str] = []
    webhook_url: str | None = None
    model: str | None = None

    @field_validator("model")
    @classmethod
    def model_not_blank(cls, v: str | None) -> str | None:
        return _validate_model_not_blank(v)


class IngestResponse(BaseModel):
    job_id: uuid.UUID
    status: str = "queued"


BrowserCaptureKind = Literal["selection_note", "media", "social_post", "webpage"]
BrowserCaptureRoute = Literal["media", "webpage", "note"]


class BrowserImageCandidate(BaseModel):
    url: str
    source_post_url: str | None = None
    alt_text: str | None = None
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    role: str | None = None
    order: int | None = Field(default=None, ge=0)


class BrowserCaptureRequest(BaseModel):
    url: str | None = None
    page_title: str | None = None
    selection_text: str | None = None
    tags: list[str] = Field(default_factory=list)
    detected_kind: BrowserCaptureKind | None = None
    image_candidates: list[BrowserImageCandidate] = Field(default_factory=list, max_length=4)
    extension_metadata: dict[str, Any] = Field(default_factory=dict)
    browser_extension_version: str | None = None
    webhook_url: str | None = None
    model: str | None = None

    @field_validator("model")
    @classmethod
    def model_not_blank(cls, v: str | None) -> str | None:
        return _validate_model_not_blank(v)


class BrowserCaptureResponse(BaseModel):
    job_id: uuid.UUID | None = None
    item_id: uuid.UUID
    status: str = "queued"
    kind: BrowserCaptureKind
    route: BrowserCaptureRoute
    source_url: str | None = None
    duplicate_of: uuid.UUID | None = None
    web_save_id: uuid.UUID | None = None


class BatchIngestItem(BaseModel):
    type: Literal["youtube", "media", "webpage", "note"]
    url: str | None = None
    content: str | None = None
    title: str | None = None
    model: str | None = None

    @field_validator("model")
    @classmethod
    def model_not_blank(cls, v: str | None) -> str | None:
        return _validate_model_not_blank(v)


class BatchIngestRequest(BaseModel):
    items: list[BatchIngestItem] = Field(..., max_length=100)
    webhook_url: str | None = None


class BatchIngestResult(BaseModel):
    job_id: uuid.UUID
    item_id: uuid.UUID
    status: str


class BatchIngestResponse(BaseModel):
    results: list[BatchIngestResult]
    total: int
