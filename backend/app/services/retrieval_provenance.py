from typing import Any
import uuid

from app.schemas.artifact_citation import ArtifactCitation
from app.schemas.retrieval_provenance import (
    RetrievalDerivedRawClass,
    RetrievalFreshnessClass,
    RetrievalModality,
    RetrievalProvenance,
    RetrievalSourceSupportState,
    RetrievalSupportLevel,
    RetrievalTrustClass,
    RetrievalTrustMetadata,
)

_IMAGE_MODALITY_SOURCE_TYPES = {"image_description", "ocr_text", "image_native", "image_candidate"}
_DIRECT_SOURCE_TYPES = {"pdf", "doc", "markdown", "web", "feed_article", "media", "image_description", "ocr_text"}


def build_retrieval_provenance(
    metadata: dict[str, Any] | None,
    *,
    item_id: Any,
    source_type: str,
    source_url: str | None,
    source_item_id: Any | None = None,
    source_span: dict[str, Any] | None = None,
    artifact_citation: ArtifactCitation | None = None,
) -> RetrievalProvenance | None:
    metadata = metadata or {}
    explicit = metadata.get("retrieval_provenance")
    if isinstance(explicit, dict):
        return _from_explicit_metadata(
            explicit,
            item_id=item_id,
            source_type=source_type,
            source_url=source_url,
            source_item_id=source_item_id,
            source_span=source_span,
            artifact_citation=artifact_citation,
        )

    image_analysis = metadata.get("image_analysis")
    if isinstance(image_analysis, dict):
        return _image_analysis_provenance(
            image_analysis,
            item_id=item_id,
            source_type=source_type,
            source_url=source_url,
            artifact_citation=artifact_citation,
        )

    browser_image = metadata.get("browser_capture_image")
    if isinstance(browser_image, dict):
        return _browser_image_provenance(
            browser_image,
            item_id=item_id,
            source_url=source_url,
            artifact_citation=artifact_citation,
        )

    if source_type in _IMAGE_MODALITY_SOURCE_TYPES:
        modality = _modality(source_type, default="image_description")
        return RetrievalProvenance(
            modality=modality,
            candidate_source=source_type,
            support_level="weak" if modality == "image_native" else "unknown",
            source_url=source_url,
            source_item_id=_uuid(item_id),
            source_span=source_span,
            original_artifact_url=artifact_citation.original_artifact_url if artifact_citation else None,
            original_artifact_label=artifact_citation.original_artifact_label if artifact_citation else None,
            media_type=artifact_citation.media_type if artifact_citation else None,
            notes=_weak_native_notes(modality, artifact_citation),
        )

    return None


def classify_retrieval_trust(
    *,
    source_type: str,
    source_url: str | None,
    artifact_provenance_type: str | None,
    derived_artifact_keys: list[str] | tuple[str, ...],
    retrieval_provenance: RetrievalProvenance | None,
    source_item_id: Any | None,
    source_span: dict[str, Any] | None,
    retrieved_scope_label: str | None,
    effective_date_source: str | None,
    effective_date_quality: str | None,
    source_support_level: str | None = None,
) -> RetrievalTrustMetadata:
    source_support_state = _source_support_state(
        retrieval_provenance=retrieval_provenance,
        source_type=source_type,
        source_url=source_url,
        source_item_id=source_item_id,
        source_span=source_span,
        source_support_level=source_support_level,
    )
    freshness = _freshness_class(
        effective_date_source=effective_date_source,
        effective_date_quality=effective_date_quality,
        source_support_level=source_support_level,
    )
    derived_raw_classification = _derived_raw_classification(
        artifact_provenance_type=artifact_provenance_type,
        derived_artifact_keys=derived_artifact_keys,
        retrieved_scope_label=retrieved_scope_label,
    )
    trust_class = _trust_class(
        source_support_state=source_support_state,
        freshness=freshness,
        derived_raw_classification=derived_raw_classification,
        artifact_provenance_type=artifact_provenance_type,
    )
    return RetrievalTrustMetadata(
        trust_class=trust_class,
        source_support_state=source_support_state,
        freshness=freshness,
        derived_raw_classification=derived_raw_classification,
    )


def _source_support_state(
    *,
    retrieval_provenance: RetrievalProvenance | None,
    source_type: str,
    source_url: str | None,
    source_item_id: Any | None,
    source_span: dict[str, Any] | None,
    source_support_level: str | None,
) -> RetrievalSourceSupportState:
    if source_support_level in {"single_source", "multi_source"}:
        return "source_backed"
    if source_support_level in {"partial_source", "conflicting"}:
        return "partial_source"
    if source_support_level in {"no_source"}:
        return "unsupported"
    if retrieval_provenance is not None:
        if retrieval_provenance.support_level == "strong":
            return "direct_source"
        if retrieval_provenance.support_level == "weak":
            return "partial_source"
    if source_span or source_item_id:
        return "source_backed"
    if source_type in _DIRECT_SOURCE_TYPES or _is_direct_source_url(source_url):
        return "direct_source"
    return "unknown"


def _freshness_class(
    *,
    effective_date_source: str | None,
    effective_date_quality: str | None,
    source_support_level: str | None,
) -> RetrievalFreshnessClass:
    if source_support_level == "stale":
        return "stale"
    if effective_date_quality == "low":
        return "stale"
    if effective_date_source and effective_date_quality in {"high", "medium"}:
        return "fresh"
    if effective_date_source:
        return "dated"
    return "undated"


def _derived_raw_classification(
    *,
    artifact_provenance_type: str | None,
    derived_artifact_keys: list[str] | tuple[str, ...],
    retrieved_scope_label: str | None,
) -> RetrievalDerivedRawClass:
    if derived_artifact_keys:
        return "derived"
    if artifact_provenance_type in {"canonical_memory", "legacy_memory_artifact"}:
        return "curated"
    if retrieved_scope_label in {None, "", "general"} and artifact_provenance_type == "corpus_item":
        return "fallback"
    return "raw"


def _trust_class(
    *,
    source_support_state: RetrievalSourceSupportState,
    freshness: RetrievalFreshnessClass,
    derived_raw_classification: RetrievalDerivedRawClass,
    artifact_provenance_type: str | None,
) -> RetrievalTrustClass:
    if freshness == "stale":
        return "stale_context"
    if derived_raw_classification == "derived":
        if source_support_state in {"unsupported", "unknown", "partial_source"}:
            return "low_support_generated"
        return "generated_synthesis"
    if derived_raw_classification == "curated" or artifact_provenance_type in {"canonical_memory", "legacy_memory_artifact"}:
        return "curated_memory"
    if source_support_state in {"direct_source", "source_backed"}:
        return "raw_source"
    if derived_raw_classification == "fallback" or source_support_state in {"unknown", "unsupported", "partial_source"}:
        return "broad_fallback"
    return "broad_fallback"


def _from_explicit_metadata(
    data: dict[str, Any],
    *,
    item_id: Any,
    source_type: str,
    source_url: str | None,
    source_item_id: Any | None,
    source_span: dict[str, Any] | None,
    artifact_citation: ArtifactCitation | None,
) -> RetrievalProvenance:
    modality = _modality(data.get("modality"), default=_modality(source_type, default="text"))
    support_level = _support_level(data.get("support_level"), default="weak" if modality == "image_native" else "unknown")
    explicit_source_item_id = _uuid(data.get("source_item_id"))
    citation_source_item_id = _uuid(source_item_id)
    return RetrievalProvenance(
        modality=modality,
        candidate_source=_string(data.get("candidate_source")) or source_type,
        support_level=support_level,
        source_url=_string(data.get("source_url")) or (artifact_citation.source_url if artifact_citation else source_url),
        source_label=_string(data.get("source_label")) or (artifact_citation.source_label if artifact_citation else None),
        source_item_id=explicit_source_item_id or citation_source_item_id or _uuid(item_id),
        source_span=data.get("source_span") if isinstance(data.get("source_span"), dict) else source_span,
        original_artifact_url=_string(data.get("original_artifact_url"))
        or (artifact_citation.original_artifact_url if artifact_citation else None),
        original_artifact_label=_string(data.get("original_artifact_label"))
        or (artifact_citation.original_artifact_label if artifact_citation else None),
        media_type=_string(data.get("media_type")) or (artifact_citation.media_type if artifact_citation else None),
        model=_string(data.get("model")) or (artifact_citation.model if artifact_citation else None),
        provider=_string(data.get("provider")) or (artifact_citation.provider if artifact_citation else None),
        confidence=_float(data.get("confidence")) if data.get("confidence") is not None else (
            artifact_citation.confidence if artifact_citation else None
        ),
        byte_hash=_string(data.get("byte_hash")) or (artifact_citation.byte_hash if artifact_citation else None),
        notes=_string_list(data.get("notes")) or _weak_native_notes(modality, artifact_citation),
    )


def _image_analysis_provenance(
    data: dict[str, Any],
    *,
    item_id: Any,
    source_type: str,
    source_url: str | None,
    artifact_citation: ArtifactCitation | None,
) -> RetrievalProvenance:
    modality = _modality(source_type, default="image_description")
    if modality == "text":
        modality = "image_description"
    vision = data.get("vision") if isinstance(data.get("vision"), dict) else {}
    return RetrievalProvenance(
        modality=modality,
        candidate_source="image_analysis.visible_text" if modality == "ocr_text" else "image_analysis.caption",
        support_level="strong",
        source_url=source_url,
        source_label="Source" if source_url else None,
        source_item_id=_uuid(item_id),
        original_artifact_url=artifact_citation.original_artifact_url if artifact_citation else None,
        original_artifact_label=artifact_citation.original_artifact_label if artifact_citation else None,
        media_type=artifact_citation.media_type if artifact_citation else None,
        model=_string(vision.get("model")) if isinstance(vision, dict) else None,
        provider=_string(vision.get("provider")) if isinstance(vision, dict) else None,
        confidence=_float(vision.get("confidence")) if isinstance(vision, dict) else None,
        byte_hash=_string(data.get("byte_hash")),
    )


def _browser_image_provenance(
    data: dict[str, Any],
    *,
    item_id: Any,
    source_url: str | None,
    artifact_citation: ArtifactCitation | None,
) -> RetrievalProvenance:
    source_post_url = _string(data.get("source_post_url")) or source_url
    return RetrievalProvenance(
        modality="image_native",
        candidate_source="browser_capture_image",
        support_level="weak",
        source_url=source_post_url,
        source_label="Parent social post" if source_post_url else None,
        source_item_id=_uuid(item_id),
        original_artifact_url=artifact_citation.original_artifact_url if artifact_citation else _string(data.get("final_url")),
        original_artifact_label=artifact_citation.original_artifact_label if artifact_citation else _string(data.get("final_url")),
        media_type=_string(data.get("media_type")),
        byte_hash=_string(data.get("byte_hash")),
        notes=["image-native evidence has no supporting OCR/caption text"],
    )


def _modality(value: Any, *, default: RetrievalModality) -> RetrievalModality:
    if value in {"text", "image_description", "ocr_text", "image_native"}:
        return value
    if value == "image_candidate":
        return "image_native"
    return default


def _support_level(value: Any, *, default: RetrievalSupportLevel) -> RetrievalSupportLevel:
    return value if value in {"strong", "weak", "unknown"} else default


def _string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _is_direct_source_url(value: str | None) -> bool:
    normalized = _string(value)
    if not normalized:
        return False
    return normalized.startswith(("http://", "https://", "s3://", "file://"))


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _uuid(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def _weak_native_notes(modality: RetrievalModality, artifact_citation: ArtifactCitation | None) -> list[str]:
    if modality != "image_native":
        return []
    if artifact_citation and (artifact_citation.caption or artifact_citation.extracted_text):
        return []
    return ["image-native evidence has no supporting OCR/caption text"]
