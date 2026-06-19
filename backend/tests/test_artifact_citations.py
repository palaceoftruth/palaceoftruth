from app.services.artifact_citations import build_artifact_citation


def test_build_artifact_citation_exposes_image_analysis_metadata() -> None:
    citation = build_artifact_citation(
        {
            "image_analysis": {
                "caption": "Whiteboard roadmap with three launch phases.",
                "visible_text": ["Phase 1", "Pilot"],
                "dimensions": {"width": 1200, "height": 800},
                "byte_hash": "a" * 64,
                "artifact": {
                    "filename": "roadmap.png",
                    "media_type": "image/png",
                    "storage_path": "/tmp/palaceoftruth/upload-artifacts/roadmap.png",
                },
                "vision": {
                    "provider": "openai",
                    "model": "gpt-4o-mini",
                    "confidence": 0.82,
                },
            }
        },
        source_url="https://example.test/source",
        original_artifact_url="/api/v1/items/00000000-0000-0000-0000-000000000001/artifact",
    )

    assert citation is not None
    assert citation.kind == "image_analysis"
    assert citation.caption == "Whiteboard roadmap with three launch phases."
    assert citation.extracted_text == ["Phase 1", "Pilot"]
    assert citation.original_artifact_label == "/tmp/palaceoftruth/upload-artifacts/roadmap.png"
    assert citation.original_artifact_url == "/api/v1/items/00000000-0000-0000-0000-000000000001/artifact"
    assert citation.thumbnail_url == "/api/v1/items/00000000-0000-0000-0000-000000000001/artifact"
    assert citation.source_url == "https://example.test/source"
    assert citation.provider == "openai"
    assert citation.model == "gpt-4o-mini"
    assert citation.confidence == 0.82


def test_build_artifact_citation_exposes_browser_image_candidate_provenance() -> None:
    citation = build_artifact_citation(
        {
            "browser_capture_image": {
                "source_post_url": "https://x.com/example/status/123",
                "candidate_url": "https://pbs.twimg.com/media/diagram.jpg",
                "final_url": "https://pbs.twimg.com/media/diagram-large.jpg",
                "alt_text": "Architecture diagram",
                "media_type": "image/jpeg",
                "dimensions": {"width": 1600, "height": 900},
                "byte_hash": "b" * 64,
            }
        },
        source_url=None,
    )

    assert citation is not None
    assert citation.kind == "browser_image_candidate"
    assert citation.thumbnail_url == "https://pbs.twimg.com/media/diagram-large.jpg"
    assert citation.caption == "Architecture diagram"
    assert citation.source_url == "https://x.com/example/status/123"
    assert citation.source_label == "Parent social post"
    assert citation.original_artifact_url == "https://pbs.twimg.com/media/diagram-large.jpg"
