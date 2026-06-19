import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


BUNDLE_VERSION = 1


class BundleSourceInstance(BaseModel):
    app: str = "palaceoftruth"
    tenant_id: str


class BundleEmbeddingMetadata(BaseModel):
    source_model: str
    rebuild_required: bool = True


class BundleManifest(BaseModel):
    bundle_version: int = BUNDLE_VERSION
    exported_at: datetime
    source_instance: BundleSourceInstance
    embedding: BundleEmbeddingMetadata
    items_file: str = "items.json"
    conversations_file: str = "conversations.json"
    artifacts_dir: str | None = None


class BundleUploadArtifactReference(BaseModel):
    source: str = "user_upload"
    filename: str
    media_type: str | None = None
    extension: str | None = None
    bundle_path: str | None = None
    storage_path: str | None = None


class BundleItemRecord(BaseModel):
    id: uuid.UUID
    source_type: str
    source_url: str | None = None
    title: str
    summary: str | None = None
    raw_content: str | None = None
    content_chunks: Any | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    upload_artifact: BundleUploadArtifactReference | None = None
    tags: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    content_hash: str | None = None
    created_at: datetime
    updated_at: datetime | None = None


class BundleConversationMessageRecord(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    created_at: datetime

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        if value not in {"user", "assistant"}:
            raise ValueError("role must be 'user' or 'assistant'")
        return value


class BundleConversationRecord(BaseModel):
    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime
    messages: list[BundleConversationMessageRecord] = Field(default_factory=list)


class BundlePayload(BaseModel):
    manifest: BundleManifest
    items: list[BundleItemRecord] = Field(default_factory=list)
    conversations: list[BundleConversationRecord] = Field(default_factory=list)


class AdminImportResponse(BaseModel):
    job_id: uuid.UUID
    tenant_id: str
    status: str


class AdminJobResponse(BaseModel):
    id: uuid.UUID
    tenant_id: str
    job_type: str
    status: str
    progress: int
    error_message: str | None
    created_at: datetime
    completed_at: datetime | None
    payload: dict[str, Any] | None = None

    model_config = {"from_attributes": True}
