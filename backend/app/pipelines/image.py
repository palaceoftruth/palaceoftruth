import logging
from typing import Any

from app.pipelines.base import BasePipeline
from app.services.image_analysis import analyze_image_artifact, build_image_analysis_metadata

logger = logging.getLogger(__name__)


class ImagePipeline(BasePipeline):
    """Process an image description through the standard pipeline.

    Legacy jobs may still pass a precomputed description. New jobs pass only
    durable upload metadata, then the worker performs vision analysis here so
    provider failures are observable and retryable through job state.
    """

    async def extract(self, description: str = "", image_metadata: dict | None = None, **_kwargs) -> tuple[str, dict[str, Any]]:
        metadata = image_metadata or {}
        if description.strip():
            return description, metadata

        analysis = metadata.get("image_analysis")
        artifact = analysis.get("artifact") if isinstance(analysis, dict) else None
        storage_path = artifact.get("storage_path") if isinstance(artifact, dict) else None
        filename = str(metadata.get("filename") or (artifact or {}).get("filename") or "Uploaded image")
        media_type = str(metadata.get("media_type") or (artifact or {}).get("media_type") or "image/jpeg")
        extension = (artifact or {}).get("extension") if isinstance(artifact, dict) else None
        if extension is not None:
            extension = str(extension)
        if not isinstance(storage_path, str) or not storage_path.strip():
            raise ValueError("Image job is missing upload artifact storage path")

        generated_description, image_bytes, byte_hash = await analyze_image_artifact(
            self.llm,
            storage_path=storage_path,
            media_type=media_type,
            filename=filename,
        )
        completed_metadata = {
            **metadata,
            **build_image_analysis_metadata(
                description=generated_description,
                filename=filename,
                media_type=media_type,
                extension=extension,
                image_bytes=image_bytes,
                byte_hash=byte_hash,
                artifact_storage_path=storage_path,
                status="completed",
            ),
        }
        return generated_description, completed_metadata
