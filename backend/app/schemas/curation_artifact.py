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
    "needs_source",
    "reviewable",
    "promoted",
    "proposed",
    "approved",
    "rejected",
    "stale",
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


def _source_digests_cover_items(source_item_ids: list[str], source_digests: dict[str, str]) -> bool:
    return all(item_id in source_digests for item_id in source_item_ids)


class CandidateCurationArtifactCreate(BaseModel):
    artifact_kind: CandidateArtifactKind
    target_runtime: str = Field(min_length=1, max_length=80)
    target_surface: str = Field(min_length=1)
    status: Literal["draft", "needs_source", "reviewable", "proposed"] = "draft"
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
        if self.status in {"reviewable", "proposed"}:
            if not self.source_item_ids:
                raise ValueError("source_item_ids must include at least one evidence pointer")
            if not self.source_digests:
                raise ValueError("source_digests must include at least one stable digest")
            if self.status == "reviewable" and not _source_digests_cover_items(
                self.source_item_ids,
                self.source_digests,
            ):
                raise ValueError("source_digests must include a stable digest for each source_item_id")
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
    promotion_state: str = "draft"
    source_support_level: str = "no_source"
    advisory_generated_context: bool = True
    promoted_source_backed: bool = False
    supersedes_artifact_id: uuid.UUID | None
    superseded_by_artifact_id: uuid.UUID | None
    deprecated_reason: str | None
    created_at: datetime
    updated_at: datetime
    approved_at: datetime | None
    deprecated_at: datetime | None
    model_config = {"from_attributes": True, "populate_by_name": True}

    @model_validator(mode="after")
    def derive_promotion_labels(self) -> "CandidateCurationArtifactOut":
        metadata = self.metadata or {}
        if metadata.get("source_evidence_stale") is True or self.status in {"stale", "deprecated", "superseded"}:
            source_support_level = "stale"
        elif metadata.get("source_conflicts") is True:
            source_support_level = "conflicting"
        elif self.source_item_ids and not _source_digests_cover_items(self.source_item_ids, self.source_digests):
            source_support_level = "partial_source"
        elif len(self.source_item_ids) > 1:
            source_support_level = "multi_source"
        elif len(self.source_item_ids) == 1:
            source_support_level = "single_source"
        else:
            source_support_level = "no_source"

        legacy_state_map = {
            "proposed": "reviewable",
            "approved": "promoted",
            "deprecated": "stale",
            "superseded": "stale",
        }
        promotion_state = legacy_state_map.get(self.status, self.status)
        self.promotion_state = promotion_state
        self.source_support_level = source_support_level
        self.promoted_source_backed = (
            promotion_state == "promoted"
            and source_support_level in {"single_source", "multi_source"}
        )
        self.advisory_generated_context = not self.promoted_source_backed
        return self


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
