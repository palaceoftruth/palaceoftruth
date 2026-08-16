import base64
import hashlib
import logging
import os
import struct
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)

_MAX_SUMMARY_LENGTH = 2_000
_MAX_VALUE_LENGTH = 500
_MAX_LIST_ITEMS = 64


class VisionRelationship(BaseModel):
    """One visible image-local edge. It is not a database item relationship."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source: str = Field(min_length=1, max_length=_MAX_VALUE_LENGTH)
    target: str = Field(min_length=1, max_length=_MAX_VALUE_LENGTH)
    direction: Literal[
        "source_to_target",
        "target_to_source",
        "bidirectional",
        "undirected",
        "unclear",
    ]
    label: str | None = Field(default=None, max_length=_MAX_VALUE_LENGTH)

    @field_validator("label")
    @classmethod
    def empty_label_is_none(cls, value: str | None) -> str | None:
        return value or None


class VisionAnalysis(BaseModel):
    """Bounded, evidence-only structured output from the vision provider."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    summary: str = Field(min_length=1, max_length=_MAX_SUMMARY_LENGTH)
    image_type: str = Field(min_length=1, max_length=100)
    visible_text: list[str] = Field(default_factory=list, max_length=_MAX_LIST_ITEMS)
    objects: list[str] = Field(default_factory=list, max_length=_MAX_LIST_ITEMS)
    entities: list[str] = Field(default_factory=list, max_length=_MAX_LIST_ITEMS)
    relationships: list[VisionRelationship] = Field(default_factory=list, max_length=_MAX_LIST_ITEMS)
    visual_details: list[str] = Field(default_factory=list, max_length=_MAX_LIST_ITEMS)
    uncertainties: list[str] = Field(default_factory=list, max_length=_MAX_LIST_ITEMS)

    @field_validator("visible_text", "objects", "entities", "visual_details", "uncertainties")
    @classmethod
    def normalize_string_lists(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            clean = str(value).strip()
            if not clean:
                continue
            normalized.append(clean[:_MAX_VALUE_LENGTH])
        return normalized


class VisionUsage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class VisionProviderMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider: Literal["openrouter"] = "openrouter"
    requested_model: str = Field(min_length=1, max_length=200)
    returned_model: str = Field(min_length=1, max_length=200)
    usage: VisionUsage | None = None


class VisionAnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis: VisionAnalysis
    provider: VisionProviderMetadata


class ImageAnalysisError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool, provider_status_code: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.provider_status_code = provider_status_code


def image_bytes_hash(image_bytes: bytes) -> str:
    return hashlib.sha256(image_bytes).hexdigest()


def sniff_image_dimensions(image_bytes: bytes) -> dict[str, int | None]:
    try:
        if image_bytes.startswith(b"\x89PNG\r\n\x1a\n") and len(image_bytes) >= 24:
            width, height = struct.unpack(">II", image_bytes[16:24])
            return {"width": width, "height": height}
        if image_bytes.startswith(b"GIF87a") or image_bytes.startswith(b"GIF89a"):
            if len(image_bytes) >= 10:
                width, height = struct.unpack("<HH", image_bytes[6:10])
                return {"width": width, "height": height}
        if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
            return _sniff_webp_dimensions(image_bytes)
        if image_bytes.startswith(b"\xff\xd8"):
            return _sniff_jpeg_dimensions(image_bytes)
    except (struct.error, ValueError):
        logger.debug("could not parse uploaded image dimensions", exc_info=True)
    return {"width": None, "height": None}


def build_image_analysis_metadata(
    *,
    description: str | None = None,
    vision_result: VisionAnalysisResult | None = None,
    filename: str,
    media_type: str,
    extension: str | None,
    image_bytes: bytes,
    byte_hash: str,
    artifact_storage_path: str | None = None,
    status: str = "completed",
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    analysis = vision_result.analysis if vision_result is not None else None
    provider = vision_result.provider if vision_result is not None else None
    caption = analysis.summary if analysis is not None else (description or "")
    return {
        "image_analysis": {
            "status": status,
            "caption": caption,
            "image_type": analysis.image_type if analysis is not None else "",
            "visible_text": analysis.visible_text if analysis is not None else [],
            "objects": analysis.objects if analysis is not None else [],
            "entities": analysis.entities if analysis is not None else [],
            "relationships": (
                [relationship.model_dump() for relationship in analysis.relationships]
                if analysis is not None
                else []
            ),
            "visual_details": analysis.visual_details if analysis is not None else [],
            "uncertainties": analysis.uncertainties if analysis is not None else [],
            "dimensions": sniff_image_dimensions(image_bytes),
            "byte_hash": byte_hash,
            "byte_size": len(image_bytes),
            "artifact": {
                "source": "user_upload",
                "filename": filename,
                "media_type": media_type,
                "extension": extension,
                "storage_path": artifact_storage_path,
            },
            "vision": {
                "provider": provider.provider if provider is not None else "openrouter",
                "model": provider.returned_model if provider is not None else None,
                "requested_model": provider.requested_model if provider is not None else None,
                "returned_model": provider.returned_model if provider is not None else None,
                "usage": provider.usage.model_dump(exclude_none=True) if provider and provider.usage else None,
                "confidence": None,
                "error": error,
            },
        }
    }


async def analyze_image_artifact(
    llm,
    *,
    storage_path: str,
    media_type: str,
    filename: str,
) -> tuple[VisionAnalysisResult, bytes, str]:
    if not storage_path or not os.path.exists(storage_path):
        raise ImageAnalysisError("Image artifact is missing; re-upload required", retryable=False)

    with open(storage_path, "rb") as f:
        image_bytes = f.read()

    image_b64 = base64.b64encode(image_bytes).decode()
    try:
        result = await llm.analyze_image(image_b64, media_type, filename)
    except Exception as exc:
        status_code = _provider_status_code(exc)
        explicit_retryable = getattr(exc, "retryable", None)
        retryable = (
            explicit_retryable
            if isinstance(explicit_retryable, bool)
            else _provider_failure_is_retryable(status_code)
        )
        message = "Vision analysis failed transiently" if retryable else "Vision provider rejected the image"
        raise ImageAnalysisError(message, retryable=retryable, provider_status_code=status_code) from exc

    try:
        validated = result if isinstance(result, VisionAnalysisResult) else VisionAnalysisResult.model_validate(result)
    except Exception as exc:
        raise ImageAnalysisError("Vision API returned invalid structured output", retryable=False) from exc

    return validated, image_bytes, image_bytes_hash(image_bytes)


def normalized_image_content(analysis: VisionAnalysis) -> str:
    """Build deterministic searchable text without creating cross-item edges."""

    sections: list[tuple[str, list[str]]] = [
        ("Summary", [analysis.summary]),
        ("Image type", [analysis.image_type]),
        ("Visible text", analysis.visible_text),
        ("Objects", analysis.objects),
        ("Entities", analysis.entities),
        (
            "Visible relationships",
            [
                f"{edge.source} | {edge.direction} | {edge.target}"
                + (f" | label: {edge.label}" if edge.label else "")
                for edge in analysis.relationships
            ],
        ),
        ("Visual details", analysis.visual_details),
        ("Uncertainties", analysis.uncertainties),
    ]
    return "\n\n".join(
        f"## {heading}\n" + "\n".join(f"- {value}" for value in values)
        for heading, values in sections
        if values
    )


def _provider_status_code(exc: Exception) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    response_status = getattr(response, "status_code", None)
    return response_status if isinstance(response_status, int) else None


def _provider_failure_is_retryable(status_code: int | None) -> bool:
    if status_code is None:
        return True
    if status_code == 429:
        return True
    return status_code >= 500


def _sniff_jpeg_dimensions(image_bytes: bytes) -> dict[str, int | None]:
    offset = 2
    while offset + 9 < len(image_bytes):
        if image_bytes[offset] != 0xFF:
            break
        marker = image_bytes[offset + 1]
        offset += 2
        if marker in {0xD8, 0xD9}:
            continue
        if offset + 2 > len(image_bytes):
            break
        segment_length = struct.unpack(">H", image_bytes[offset:offset + 2])[0]
        if segment_length < 2:
            break
        if 0xC0 <= marker <= 0xCF and marker not in {0xC4, 0xC8, 0xCC}:
            if offset + 7 > len(image_bytes):
                break
            height, width = struct.unpack(">HH", image_bytes[offset + 3:offset + 7])
            return {"width": width, "height": height}
        offset += segment_length
    return {"width": None, "height": None}


def _sniff_webp_dimensions(image_bytes: bytes) -> dict[str, int | None]:
    chunk_type = image_bytes[12:16]
    if chunk_type == b"VP8X" and len(image_bytes) >= 30:
        width = int.from_bytes(image_bytes[24:27], "little") + 1
        height = int.from_bytes(image_bytes[27:30], "little") + 1
        return {"width": width, "height": height}
    if chunk_type == b"VP8 " and len(image_bytes) >= 30:
        width, height = struct.unpack("<HH", image_bytes[26:30])
        return {"width": width & 0x3FFF, "height": height & 0x3FFF}
    if chunk_type == b"VP8L" and len(image_bytes) >= 25:
        bits = int.from_bytes(image_bytes[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return {"width": width, "height": height}
    return {"width": None, "height": None}
