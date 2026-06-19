import uuid
from typing import Literal

from pydantic import BaseModel, field_validator

from app.schemas.artifact_citation import ArtifactCitation
from app.schemas.retrieval_provenance import RetrievalProvenance


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str

    @field_validator("content")
    @classmethod
    def content_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("content must not be blank")
        return value


class UsageInfo(BaseModel):
    input_tokens: int | None
    output_tokens: int | None


class ChatSource(BaseModel):
    item_id: uuid.UUID
    title: str
    source_type: str
    chunk_text: str
    score: float
    chunk_index: int = 0
    total_chunks: int | None = None
    source_url: str | None = None
    artifact_citation: ArtifactCitation | None = None
    retrieval_provenance: RetrievalProvenance | None = None


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    model: str | None = None
    conversation_id: uuid.UUID | None = None

    @field_validator("messages")
    @classmethod
    def messages_must_not_be_empty(cls, value: list[ChatMessage]) -> list[ChatMessage]:
        if not value:
            raise ValueError("messages must not be empty")
        return value

    @field_validator("model")
    @classmethod
    def model_not_blank(cls, v: str | None) -> str | None:
        if v is not None and v.strip() == "":
            raise ValueError("model must not be blank")
        return v.strip() if v is not None else None


class ChatResponse(BaseModel):
    response: str
    sources: list[ChatSource]
    usage: UsageInfo | None = None
