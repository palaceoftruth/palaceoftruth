import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


CandidateArtifactKind = Literal[
    "candidate_skill",
    "candidate_routing_manifest",
    "candidate_prompt_guardrail",
]
CandidateArtifactStatus = Literal[
    "draft",
    "proposed",
    "approved",
    "rejected",
    "deprecated",
    "superseded",
]


def _not_blank(value: str, field_name: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} must not be blank")
    return stripped


def _clean_string_list(values: list[str], field_name: str) -> list[str]:
    cleaned: list[str] = []
    for value in values:
        stripped = _not_blank(value, field_name)
        if stripped not in cleaned:
            cleaned.append(stripped)
    return cleaned


class CandidateCurationArtifactCreate(BaseModel):
    artifact_kind: CandidateArtifactKind
    target_runtime: str = Field(min_length=1, max_length=80)
    target_surface: str = Field(min_length=1)
    status: Literal["draft", "proposed"] = "draft"
    source_item_ids: list[str] = Field(default_factory=list)
    source_digests: dict[str, str] = Field(default_factory=dict)
    candidate_body: str = Field(min_length=1)
    privacy_review: dict[str, Any] = Field(default_factory=dict)
    eval_summary: dict[str, Any] = Field(default_factory=dict)
    approval: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    supersedes_artifact_id: uuid.UUID | None = None

    @field_validator("target_runtime", "target_surface", "candidate_body")
    @classmethod
    def required_strings_not_blank(cls, value: str, info) -> str:
        return _not_blank(value, info.field_name)

    @field_validator("source_item_ids")
    @classmethod
    def source_item_ids_not_blank(cls, value: list[str]) -> list[str]:
        return _clean_string_list(value, "source_item_ids")

    @field_validator("source_digests")
    @classmethod
    def source_digests_not_blank(cls, value: dict[str, str]) -> dict[str, str]:
        return {
            _not_blank(key, "source_digests key"): _not_blank(digest, "source_digests value")
            for key, digest in value.items()
        }

    @model_validator(mode="after")
    def validate_source_evidence(self) -> "CandidateCurationArtifactCreate":
        if not self.source_item_ids:
            raise ValueError("source_item_ids must include at least one evidence pointer")
        if not self.source_digests:
            raise ValueError("source_digests must include at least one stable digest")
        if not self.privacy_review:
            raise ValueError("privacy_review is required")
        return self


class CandidateCurationArtifactUpdate(BaseModel):
    status: str | None = None
    source_item_ids: list[str] | None = None
    source_digests: dict[str, str] | None = None
    privacy_review: dict[str, Any] | None = None
    eval_summary: dict[str, Any] | None = None
    approval: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    superseded_by_artifact_id: uuid.UUID | None = None
    deprecated_reason: str | None = None

    @field_validator("source_item_ids")
    @classmethod
    def optional_source_item_ids_not_blank(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = _clean_string_list(value, "source_item_ids")
        if not cleaned:
            raise ValueError("source_item_ids must include at least one evidence pointer")
        return cleaned

    @field_validator("status")
    @classmethod
    def status_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _not_blank(value, "status")

    @field_validator("source_digests")
    @classmethod
    def optional_source_digests_not_blank(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        cleaned = {
            _not_blank(key, "source_digests key"): _not_blank(digest, "source_digests value")
            for key, digest in value.items()
        }
        if not cleaned:
            raise ValueError("source_digests must include at least one stable digest")
        return cleaned

    @field_validator("deprecated_reason")
    @classmethod
    def deprecated_reason_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _not_blank(value, "deprecated_reason")


class CandidateCurationArtifactOut(BaseModel):
    id: uuid.UUID
    tenant_id: str
    artifact_kind: str
    target_runtime: str
    target_surface: str
    status: str
    source_item_ids: list[str]
    source_digests: dict[str, str]
    candidate_body: str
    privacy_review: dict[str, Any]
    eval_summary: dict[str, Any]
    approval: dict[str, Any]
    metadata: dict[str, Any] = Field(default={}, validation_alias="metadata_")
    supersedes_artifact_id: uuid.UUID | None
    superseded_by_artifact_id: uuid.UUID | None
    deprecated_reason: str | None
    created_at: datetime
    updated_at: datetime
    approved_at: datetime | None
    deprecated_at: datetime | None
    model_config = {"from_attributes": True, "populate_by_name": True}


class CandidateCurationArtifactListResponse(BaseModel):
    artifacts: list[CandidateCurationArtifactOut]
    total: int


class CandidatePromotionHandoffOut(BaseModel):
    artifact_id: uuid.UUID
    target_runtime: str
    target_surface: str
    promotion_target: str
    source_item_ids: list[str]
    source_digests: dict[str, str]
    approval: dict[str, Any]
    gate_evidence: dict[str, Any]
    rollback_or_deprecation_notes: list[str]
    rendered_handoff: str
