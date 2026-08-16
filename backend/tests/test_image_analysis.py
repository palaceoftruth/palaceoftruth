from pathlib import Path
from types import SimpleNamespace

import pytest

from app.pipelines.image import ImagePipeline
from app.pipelines.base import BasePipeline
from app.services.image_analysis import (
    ImageAnalysisError,
    VisionAnalysis,
    VisionAnalysisResult,
    VisionProviderMetadata,
    VisionRelationship,
    analyze_image_artifact,
    build_image_analysis_metadata,
    normalized_image_content,
)


def _diagram_result() -> VisionAnalysisResult:
    return VisionAnalysisResult(
        analysis=VisionAnalysis(
            summary="API sends work to Worker.",
            image_type="directed diagram",
            visible_text=["API", "Worker", "enqueue"],
            objects=["two boxes", "arrow"],
            entities=["API", "Worker"],
            relationships=[
                VisionRelationship(
                    source="API",
                    target="Worker",
                    direction="source_to_target",
                    label="enqueue",
                )
            ],
            visual_details=["Blue API box is left of the Worker box."],
            uncertainties=["Small footer text is unreadable."],
        ),
        provider=VisionProviderMetadata(
            requested_model="google/gemini-2.5-flash-lite",
            returned_model="google/gemini-2.5-flash-lite",
        ),
    )


def test_structured_diagram_content_preserves_labels_direction_and_uncertainty() -> None:
    content = normalized_image_content(_diagram_result().analysis)

    assert "- API" in content
    assert "API | source_to_target | Worker | label: enqueue" in content
    assert "Small footer text is unreadable." in content


def test_image_analysis_metadata_is_additive_and_sanitized() -> None:
    metadata = build_image_analysis_metadata(
        vision_result=_diagram_result(),
        filename="diagram.png",
        media_type="image/png",
        extension=".png",
        image_bytes=b"safe image bytes",
        byte_hash="b" * 64,
        artifact_storage_path="/safe/tenant/item.png",
    )["image_analysis"]

    assert metadata["caption"] == "API sends work to Worker."
    assert metadata["visible_text"] == ["API", "Worker", "enqueue"]
    assert metadata["relationships"][0]["direction"] == "source_to_target"
    assert metadata["vision"] == {
        "provider": "openrouter",
        "model": "google/gemini-2.5-flash-lite",
        "requested_model": "google/gemini-2.5-flash-lite",
        "returned_model": "google/gemini-2.5-flash-lite",
        "usage": None,
        "confidence": None,
        "error": None,
    }


@pytest.mark.asyncio
async def test_image_pipeline_uses_vision_summary_and_normalized_content(tmp_path: Path) -> None:
    artifact = tmp_path / "diagram.png"
    artifact.write_bytes(b"image bytes")

    class LLM:
        async def analyze_image(self, *_args):
            return _diagram_result()

    pipeline = ImagePipeline(db=None, embedder=None, llm=LLM())
    raw_content, metadata = await pipeline.extract(
        image_metadata={
            "image_analysis": {
                "artifact": {
                    "storage_path": str(artifact),
                    "filename": "diagram.png",
                    "media_type": "image/png",
                    "extension": ".png",
                }
            }
        }
    )

    assert pipeline._authoritative_summary(raw_content, metadata) == "API sends work to Worker."
    assert "## Visible relationships" in raw_content
    assert metadata["image_analysis"]["status"] == "completed"


@pytest.mark.asyncio
async def test_invalid_provider_output_does_not_invent_summary(tmp_path: Path) -> None:
    artifact = tmp_path / "image.png"
    artifact.write_bytes(b"image bytes")

    class LLM:
        async def analyze_image(self, *_args):
            return {"analysis": {"summary": ""}, "provider": {}}

    with pytest.raises(ImageAnalysisError, match="invalid structured output"):
        await analyze_image_artifact(
            LLM(),
            storage_path=str(artifact),
            media_type="image/png",
            filename="image.png",
        )


@pytest.mark.asyncio
async def test_authoritative_summary_skips_only_generic_summarization() -> None:
    calls: list[str] = []

    class LLM:
        async def summarize(self, *_args, **_kwargs):
            calls.append("summarize")
            return "generic"

        async def generate_tags(self, *_args, **_kwargs):
            calls.append("tags")
            return ["diagram"], ["architecture"]

        async def extract_entities(self, *_args, **_kwargs):
            calls.append("entities")
            return SimpleNamespace(model_dump=lambda: {"key_topics": ["API"]})

    pipeline = BasePipeline(db=None, embedder=None, llm=LLM())
    summary, tags, categories, entities = await pipeline._run_enrichment(
        "normalized diagram content",
        [],
        authoritative_summary="Factual vision summary",
    )

    assert summary == "Factual vision summary"
    assert calls == ["tags", "entities"]
    assert tags == ["diagram"]
    assert categories == ["architecture"]
    assert entities == {"key_topics": ["API"]}
    assert pipeline._authoritative_summary("text", {}) is None
