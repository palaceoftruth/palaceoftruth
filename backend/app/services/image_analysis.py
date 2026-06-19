import base64
import hashlib
import logging
import os
import struct
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


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
    description: str | None,
    filename: str,
    media_type: str,
    extension: str | None,
    image_bytes: bytes,
    byte_hash: str,
    artifact_storage_path: str | None = None,
    status: str = "completed",
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "image_analysis": {
            "status": status,
            "caption": description or "",
            "visible_text": [],
            "objects": [],
            "entities": [],
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
                "provider": "openai",
                "model": settings.vision_model,
                "version": None,
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
) -> tuple[str, bytes, str]:
    if not storage_path or not os.path.exists(storage_path):
        raise ImageAnalysisError("Image artifact is missing; re-upload required", retryable=False)

    with open(storage_path, "rb") as f:
        image_bytes = f.read()

    image_b64 = base64.b64encode(image_bytes).decode()
    try:
        description = await llm.analyze_image(image_b64, media_type, filename)
    except Exception as exc:
        status_code = _provider_status_code(exc)
        retryable = _provider_failure_is_retryable(status_code)
        message = "Vision analysis failed transiently" if retryable else "Vision provider rejected the image"
        raise ImageAnalysisError(message, retryable=retryable, provider_status_code=status_code) from exc

    if not description.strip():
        raise ImageAnalysisError("Vision API returned an empty description", retryable=False)

    return description, image_bytes, image_bytes_hash(image_bytes)


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
