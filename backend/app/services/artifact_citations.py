from typing import Any

from app.schemas.artifact_citation import ArtifactCitation


def build_artifact_citation(
    metadata: dict[str, Any] | None,
    *,
    source_url: str | None = None,
    original_artifact_url: str | None = None,
) -> ArtifactCitation | None:
    if not metadata:
        return None

    browser_image = metadata.get("browser_capture_image")
    if isinstance(browser_image, dict):
        return _browser_image_citation(browser_image, source_url=source_url)

    image_analysis = metadata.get("image_analysis")
    if isinstance(image_analysis, dict):
        return _image_analysis_citation(
            image_analysis,
            source_url=source_url,
            original_artifact_url=original_artifact_url,
        )

    return None


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def _float(value: Any) -> float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _dimensions(value: Any) -> dict[str, int | None] | None:
    if not isinstance(value, dict):
        return None
    width = value.get("width")
    height = value.get("height")
    return {
        "width": width if isinstance(width, int) else None,
        "height": height if isinstance(height, int) else None,
    }


def _browser_image_citation(data: dict[str, Any], *, source_url: str | None) -> ArtifactCitation:
    source_post_url = _string(data.get("source_post_url"))
    candidate_url = _string(data.get("candidate_url"))
    final_url = _string(data.get("final_url"))
    return ArtifactCitation(
        kind="browser_image_candidate",
        thumbnail_url=final_url or candidate_url,
        caption=_string(data.get("alt_text")),
        source_url=source_post_url or source_url,
        source_label="Parent social post" if source_post_url else "Source",
        original_artifact_url=final_url or candidate_url,
        original_artifact_label=final_url or candidate_url,
        media_type=_string(data.get("media_type")),
        dimensions=_dimensions(data.get("dimensions")),
        byte_hash=_string(data.get("byte_hash")),
    )


def _image_analysis_citation(
    data: dict[str, Any],
    *,
    source_url: str | None,
    original_artifact_url: str | None,
) -> ArtifactCitation:
    artifact = data.get("artifact") if isinstance(data.get("artifact"), dict) else {}
    vision = data.get("vision") if isinstance(data.get("vision"), dict) else {}
    storage_path = _string(artifact.get("storage_path")) if isinstance(artifact, dict) else None
    filename = _string(artifact.get("filename")) if isinstance(artifact, dict) else None
    return ArtifactCitation(
        kind="image_analysis",
        thumbnail_url=original_artifact_url,
        caption=_string(data.get("caption")),
        extracted_text=_string_list(data.get("visible_text")),
        source_url=source_url,
        source_label="Source" if source_url else None,
        original_artifact_url=original_artifact_url,
        original_artifact_label=storage_path or filename,
        filename=filename,
        media_type=_string(artifact.get("media_type")) if isinstance(artifact, dict) else None,
        dimensions=_dimensions(data.get("dimensions")),
        model=_string(vision.get("model")) if isinstance(vision, dict) else None,
        provider=_string(vision.get("provider")) if isinstance(vision, dict) else None,
        confidence=_float(vision.get("confidence")) if isinstance(vision, dict) else None,
        byte_hash=_string(data.get("byte_hash")),
    )
