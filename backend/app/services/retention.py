from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.curation_artifact import CandidateCurationArtifactCreate
from app.schemas.memory import MemoryEntryFactKind, MemoryEntryRequest, MemoryScope
from app.services.curation_artifacts import create_candidate_curation_artifact
from app.services.codex_memory_privacy import redact_codex_memory_preview, scan_codex_memory_privacy
from app.services.llm import LLMService
from app.services.memory import MemoryArtifactAcceptanceResult, accept_canonical_memory_entry
from app.services.memory_admission import evaluate_memory_write_admission
from app.services.memory_telemetry import record_retention_extraction
from app.services.semantic_scope_profiles import SemanticScopeProfileService

logger = logging.getLogger(__name__)

RetentionExtractionMode = Literal["raw_write", "extracted_write", "reflection_candidate"]

_DEFAULT_RETAIN_MISSION = (
    "Retain durable facts, decisions, task state, operational lessons, and source-backed context. "
    "Do not retain greetings, banter, transient debug noise, or sensitive raw secret values."
)
_DEFAULT_REFLECT_MISSION = (
    "Propose concise observation memories only when the supplied source memories support them. "
    "Keep conflicts visible for operator review. Do not promote conclusions or hide raw evidence."
)
_MAX_EXTRACTION_INPUT_CHARS = 8000
_SECRET_PLACEHOLDER = "[redacted]"
_SECRET_TAG_PLACEHOLDER = "redacted"


class RetentionExtractedEntry(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    body: str = Field(min_length=1)
    summary: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    fact_kind: MemoryEntryFactKind
    tags: list[str] = Field(default_factory=list)

    @field_validator("title", "body", "summary")
    @classmethod
    def strings_are_clean(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("value must not be blank")
        return cleaned

    @field_validator("tags")
    @classmethod
    def tags_are_clean(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        cleaned: list[str] = []
        for tag in value:
            normalized = tag.strip().lower()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            cleaned.append(normalized)
        return cleaned[:12]


class RetentionExtractionOutput(BaseModel):
    entries: list[RetentionExtractedEntry] = Field(default_factory=list, max_length=10)


@dataclass(frozen=True)
class RetentionWriteResult:
    mode: RetentionExtractionMode
    created_count: int
    rejected_count: int
    skipped_count: int
    acceptance_results: list[MemoryArtifactAcceptanceResult]
    candidate_artifact_ids: list[str]
    extraction_confidences: list[float]
    retain_mission: str


class RetentionService:
    """Mission-steered retention extraction built on the canonical memory write path."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        llm: LLMService | None = None,
        profile_service: SemanticScopeProfileService | None = None,
    ) -> None:
        self.db = db
        self.tenant_id = tenant_id
        self.llm = llm or LLMService()
        self.profile_service = profile_service or SemanticScopeProfileService(db, tenant_id=tenant_id)

    async def retain(
        self,
        body: MemoryEntryRequest,
        *,
        mode: RetentionExtractionMode = "raw_write",
        signing_key: str | None = None,
        auth_mode: str | None = None,
        allowed_scopes: list[str] | None = None,
        mcp_client_key: str | None = None,
    ) -> RetentionWriteResult:
        if body.tenant_id != self.tenant_id:
            raise HTTPException(status_code=403, detail="Memory entry tenant does not match authenticated tenant")
        if mode == "raw_write":
            admission = evaluate_memory_write_admission(
                body=body,
                auth_mode=auth_mode,
                allowed_scopes=list(allowed_scopes or []),
                mcp_client_key=mcp_client_key,
            )
            if not admission.accepted:
                raise HTTPException(status_code=admission.http_status_code, detail=admission.response_detail())
            result = await accept_canonical_memory_entry(
                self.db,
                body=body,
                signing_key=signing_key,
                admission_audit=admission.audit,
            )
            return RetentionWriteResult(
                mode=mode,
                created_count=1,
                rejected_count=0,
                skipped_count=0,
                acceptance_results=[result],
                candidate_artifact_ids=[],
                extraction_confidences=[],
                retain_mission="",
            )

        profile = await self.profile_service.get_profile(body.scope)
        if mode == "reflection_candidate":
            if not profile.reflection_enabled:
                return RetentionWriteResult(
                    mode=mode,
                    created_count=0,
                    rejected_count=0,
                    skipped_count=1,
                    acceptance_results=[],
                    candidate_artifact_ids=[],
                    extraction_confidences=[],
                    retain_mission="",
                )
            reflect_mission = profile.reflect_mission.strip() or _DEFAULT_REFLECT_MISSION
            return await self._create_reflection_candidates(
                body,
                reflect_mission=reflect_mission,
            )

        retain_mission = profile.retain_mission.strip() or _DEFAULT_RETAIN_MISSION
        try:
            extracted = await self.extract(body, retain_mission=retain_mission)
        except Exception:
            record_retention_extraction(status="error", mode=mode)
            raise
        if not extracted.entries:
            record_retention_extraction(status="empty", mode=mode)
            return RetentionWriteResult(
                mode=mode,
                created_count=0,
                rejected_count=0,
                skipped_count=0,
                acceptance_results=[],
                candidate_artifact_ids=[],
                extraction_confidences=[],
                retain_mission=retain_mission,
            )

        candidates = [
            self._entry_request_from_extraction(body, extracted_entry, retain_mission=retain_mission)
            for extracted_entry in extracted.entries
        ]
        accepted_candidates: list[tuple[MemoryEntryRequest, RetentionExtractedEntry, dict]] = []
        rejected_count = 0
        for candidate, extracted_entry in zip(candidates, extracted.entries, strict=True):
            admission = evaluate_memory_write_admission(
                body=candidate,
                auth_mode=auth_mode,
                allowed_scopes=list(allowed_scopes or []),
                mcp_client_key=mcp_client_key,
            )
            if not admission.accepted:
                rejected_count += 1
                logger.info("retention extraction candidate rejected: %s", admission.reason_code)
                continue
            accepted_candidates.append((candidate, extracted_entry, admission.audit))
        if rejected_count:
            record_retention_extraction(status="rejected", mode=mode)

        results: list[MemoryArtifactAcceptanceResult] = []
        for candidate, _extracted_entry, audit in accepted_candidates:
            results.append(
                await accept_canonical_memory_entry(
                    self.db,
                    body=candidate,
                    signing_key=signing_key,
                    admission_audit=audit,
                )
            )

        record_retention_extraction(status="written" if results else "empty", mode=mode)
        return RetentionWriteResult(
            mode=mode,
            created_count=len(results),
            rejected_count=rejected_count,
            skipped_count=len(extracted.entries) - len(accepted_candidates),
            acceptance_results=results,
            candidate_artifact_ids=[],
            extraction_confidences=[entry.confidence for _candidate, entry, _audit in accepted_candidates],
            retain_mission=retain_mission,
        )

    async def extract(self, body: MemoryEntryRequest, *, retain_mission: str) -> RetentionExtractionOutput:
        redacted_title = redact_codex_memory_preview(body.title, placeholder=_SECRET_PLACEHOLDER)
        redacted_body = redact_codex_memory_preview(body.body, placeholder=_SECRET_PLACEHOLDER)
        redacted_summary = (
            redact_codex_memory_preview(body.summary or "", placeholder=_SECRET_PLACEHOLDER)
            if body.summary
            else ""
        )
        prompt_tags = _sanitize_retention_tags(body.tags)
        messages = [
            {
                "role": "system",
                "content": (
                    "You extract durable Palace memory entries from raw agent or operator text. "
                    "Use the retain mission as the policy. Return JSON only. "
                    "If nothing should be retained, return an empty entries array. "
                    "Never include raw secret values. Choose fact_kind from world, experience, observation."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Scope: {body.scope.type}/{body.scope.key or ''}\n"
                    f"Retain mission: {retain_mission}\n"
                    f"Source: {body.source}\n"
                    f"Title: {redacted_title}\n"
                    f"Summary: {redacted_summary}\n"
                    f"Tags: {', '.join(prompt_tags)}\n\n"
                    f"Text:\n{redacted_body[:_MAX_EXTRACTION_INPUT_CHARS]}"
                ),
            },
        ]
        try:
            output = await self.llm.complete_structured(
                messages,
                RetentionExtractionOutput,
                schema_name="retention_extraction",
            )
        except Exception as exc:
            raise RuntimeError("Retention extraction failed before any memory entries were written") from exc
        if not isinstance(output, RetentionExtractionOutput):
            raise RuntimeError("Retention extraction returned an unexpected response type")
        return output

    async def _create_reflection_candidates(
        self,
        source: MemoryEntryRequest,
        *,
        reflect_mission: str,
    ) -> RetentionWriteResult:
        try:
            extracted = await self.extract(source, retain_mission=reflect_mission)
        except Exception:
            record_retention_extraction(status="error", mode="reflection_candidate")
            raise
        if not extracted.entries:
            record_retention_extraction(status="empty", mode="reflection_candidate")
            return RetentionWriteResult(
                mode="reflection_candidate",
                created_count=0,
                rejected_count=0,
                skipped_count=0,
                acceptance_results=[],
                candidate_artifact_ids=[],
                extraction_confidences=[],
                retain_mission=reflect_mission,
            )

        source_memory_ids = _metadata_string_list(source.metadata, "source_memory_ids")
        contradicts_memory_ids = _metadata_string_list(source.metadata, "contradicts_memory_ids")
        source_digests = {
            source_id: hashlib.sha256(
                json.dumps(
                    {
                        "source_id": source_id,
                        "source_idempotency_key": source.idempotency_key,
                        "source_created_at": source.created_at.astimezone(timezone.utc).isoformat(),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            for source_id in source_memory_ids
        }
        status = "reviewable" if source_memory_ids else "needs_source"
        artifact_ids: list[str] = []
        rejected_count = 0
        for extracted_entry in extracted.entries:
            candidate_body = self._reflection_candidate_body(extracted_entry)
            if scan_codex_memory_privacy(candidate_body).has_findings:
                candidate_body = redact_codex_memory_preview(candidate_body, placeholder=_SECRET_PLACEHOLDER)
            metadata = {
                **dict(source.metadata or {}),
                "semantic_memory_reflection": {
                    "schema_version": 1,
                    "provenance_state": "generated_unpromoted",
                    "source_memory_ids": source_memory_ids,
                    "contradicts_memory_ids": contradicts_memory_ids,
                    "reflect_mission_sha256": hashlib.sha256(reflect_mission.encode()).hexdigest(),
                    "source_idempotency_key": source.idempotency_key,
                    "scope": source.scope.model_dump(mode="json"),
                    "fact_kind": extracted_entry.fact_kind,
                },
                "source_conflicts": bool(contradicts_memory_ids),
            }
            try:
                artifact = await create_candidate_curation_artifact(
                    self.db,
                    tenant_id=self.tenant_id,
                    body=CandidateCurationArtifactCreate(
                        artifact_kind="candidate_memory_reflection",
                        target_runtime="palace-memory",
                        target_surface=_scope_surface(source.scope),
                        status=status,
                        source_item_ids=source_memory_ids,
                        source_digests=source_digests,
                        candidate_body=candidate_body,
                        privacy_review={
                            "safe_for_review": True,
                            "raw_sensitive_content_excluded": True,
                            "contains_sensitive_content": False,
                        },
                        eval_summary={
                            "confidence": extracted_entry.confidence,
                            "fact_kind": extracted_entry.fact_kind,
                            "source_count": len(source_memory_ids),
                            "blocks_promotion_until_conflict_reviewed": bool(contradicts_memory_ids),
                        },
                        approval={},
                        metadata=metadata,
                    ),
                )
            except Exception:
                rejected_count += 1
                logger.info("reflection candidate rejected before review artifact creation", exc_info=True)
                continue
            artifact_ids.append(str(artifact.id))

        record_retention_extraction(status="written" if artifact_ids else "rejected", mode="reflection_candidate")
        return RetentionWriteResult(
            mode="reflection_candidate",
            created_count=0,
            rejected_count=rejected_count,
            skipped_count=len(extracted.entries) - len(artifact_ids) - rejected_count,
            acceptance_results=[],
            candidate_artifact_ids=artifact_ids,
            extraction_confidences=[entry.confidence for entry in extracted.entries],
            retain_mission=reflect_mission,
        )

    @staticmethod
    def _reflection_candidate_body(extracted: RetentionExtractedEntry) -> str:
        return "\n".join(
            part for part in (extracted.title, extracted.summary or "", extracted.body) if part
        )

    def _entry_request_from_extraction(
        self,
        source: MemoryEntryRequest,
        extracted: RetentionExtractedEntry,
        *,
        retain_mission: str,
    ) -> MemoryEntryRequest:
        entry_text = "\n".join(
            part for part in (extracted.title, extracted.summary or "", extracted.body) if part
        )
        if scan_codex_memory_privacy(entry_text).has_findings:
            redacted_title = redact_codex_memory_preview(extracted.title, placeholder=_SECRET_PLACEHOLDER)
            redacted_body = redact_codex_memory_preview(extracted.body, placeholder=_SECRET_PLACEHOLDER)
            redacted_summary = (
                redact_codex_memory_preview(extracted.summary or "", placeholder=_SECRET_PLACEHOLDER)
                if extracted.summary
                else None
            )
        else:
            redacted_title = extracted.title
            redacted_body = extracted.body
            redacted_summary = extracted.summary

        created_at = source.created_at or datetime.now(timezone.utc)
        tags = [
            *_sanitize_retention_tags(source.tags),
            *_sanitize_retention_tags(extracted.tags),
            f"fact-kind:{extracted.fact_kind}",
            "retention:extracted",
        ]
        metadata = dict(source.metadata or {})
        metadata["retention_extraction"] = {
            "schema_version": 1,
            "mode": "extracted_write",
            "confidence": extracted.confidence,
            "retain_mission_sha256": hashlib.sha256(retain_mission.encode()).hexdigest(),
            "source_title": redact_codex_memory_preview(source.title, placeholder=_SECRET_PLACEHOLDER),
            "source_idempotency_key": source.idempotency_key,
        }
        return MemoryEntryRequest(
            tenant_id=source.tenant_id,
            title=redacted_title,
            body=redacted_body,
            summary=redacted_summary,
            source=source.source,
            created_at=created_at,
            tags=tags,
            scope=MemoryScope(type=source.scope.type, key=source.scope.key),
            source_url=source.source_url,
            created_by_role=source.created_by_role,
            metadata=metadata,
            idempotency_key=self._extracted_idempotency_key(source, extracted),
            valid_from=source.valid_from,
            valid_until=source.valid_until,
            supersedes_entry_id=source.supersedes_entry_id,
            fact_kind=extracted.fact_kind,
            webhook_url=source.webhook_url,
            enable_ai_enrichment=source.enable_ai_enrichment,
            relationship_policy=source.relationship_policy,
        )

    @staticmethod
    def _extracted_idempotency_key(source: MemoryEntryRequest, extracted: RetentionExtractedEntry) -> str:
        identity = {
            "source_idempotency_key": source.idempotency_key,
            "source_title": source.title,
            "source_created_at": source.created_at.astimezone(timezone.utc).isoformat(),
            "scope": source.scope.model_dump(mode="json"),
            "title": extracted.title,
            "body_sha256": hashlib.sha256(extracted.body.encode()).hexdigest(),
            "fact_kind": extracted.fact_kind,
        }
        canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        return f"ret:{hashlib.sha256(canonical.encode()).hexdigest()[:60]}"


def _sanitize_retention_tags(tags: list[str]) -> list[str]:
    sanitized: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        cleaned = tag.strip()
        if not cleaned:
            continue
        if scan_codex_memory_privacy(cleaned).has_findings:
            cleaned = _SECRET_TAG_PLACEHOLDER
        if cleaned not in seen:
            seen.add(cleaned)
            sanitized.append(cleaned)
    return sanitized


def _metadata_string_list(metadata: dict[str, Any] | None, key: str) -> list[str]:
    value = (metadata or {}).get(key)
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        stripped = item.strip()
        if stripped and stripped not in cleaned:
            cleaned.append(stripped)
    return cleaned


def _scope_surface(scope: MemoryScope) -> str:
    if scope.type == "tenant_shared":
        return "tenant_shared"
    return f"{scope.type}/{scope.key}"
