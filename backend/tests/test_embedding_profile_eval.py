import importlib.util
import json
import sys
from pathlib import Path

import pytest

from app.services.embedding_profile_eval import (
    EmbeddingProfileCapture,
    EmbeddingProfileEvalInputError,
    build_native_image_provider_capture_report,
    compare_embedding_profiles,
    materialize_live_capture_pack,
    parse_profile_metadata,
    read_profile_captures,
)
from app.services.retrieval_replay import read_capture_file


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "retrieval_replay"
LIVE_CAPTURE_DIR = Path(__file__).parent / "fixtures" / "embedding_live_capture"
SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "benchmark_embedding_profiles.py"
SPEC = importlib.util.spec_from_file_location("benchmark_embedding_profiles", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
benchmark_module = importlib.util.module_from_spec(SPEC)
sys.modules["benchmark_embedding_profiles"] = benchmark_module
SPEC.loader.exec_module(benchmark_module)


def _profiles() -> list[EmbeddingProfileCapture]:
    baseline = read_capture_file(FIXTURE_DIR / "baseline.ndjson")
    current = read_capture_file(FIXTURE_DIR / "current.ndjson")
    return [
        EmbeddingProfileCapture(
            name="openai",
            records=baseline,
            metadata={"provider": "openai", "model": "text-embedding-3-small", "dimensions": 1536},
        ),
        EmbeddingProfileCapture(
            name="gte-modernbert-base",
            records=current,
            metadata={"provider": "local-http", "model": "gte-modernbert-base", "dimensions": 768},
        ),
    ]


def _slow_fallback_profiles() -> list[EmbeddingProfileCapture]:
    profiles = _profiles()
    profiles[1] = EmbeddingProfileCapture(
        name="gte-modernbert-base",
        records=[
            {
                **record,
                "fallback_used": index == 0,
                "latency_ms": 900.0 if index == 0 else record["latency_ms"],
            }
            for index, record in enumerate(profiles[1].records)
        ],
        metadata=profiles[1].metadata,
    )
    return profiles


def test_compare_embedding_profiles_reports_profile_metrics() -> None:
    report = compare_embedding_profiles(
        _slow_fallback_profiles(),
        baseline_profile="openai",
        top_k=2,
        max_top1_change_rate=0.5,
    )

    candidate = report["profiles"]["gte-modernbert-base"]
    assert report["summary"]["passed"] is False
    assert report["summary"]["failure_counts"] == {
        "gte-modernbert-base:fallback_changed": 1,
        "gte-modernbert-base:latency_delta_warn": 1,
    }
    assert candidate["metadata"] == {
        "provider": "local-http",
        "model": "gte-modernbert-base",
        "dimensions": 768,
    }
    assert candidate["metrics"]["matched_records"] == 2
    assert candidate["metrics"]["recall_at_k"] is None
    assert candidate["metrics"]["top1_stability"] == 1.0
    assert candidate["metrics"]["overlap_at_k"] == 1.0
    assert candidate["metrics"]["average_latency_ms"] == 495.5


def test_compare_embedding_profiles_reports_query_dimensions_and_modality_mix() -> None:
    profiles = read_profile_captures(
        [
            f"text-description={FIXTURE_DIR / 'sar604_text_description.ndjson'}",
            f"native-candidate={FIXTURE_DIR / 'sar604_native_multimodal_multilingual.ndjson'}",
        ],
        profile_metadata={
            "text-description": {"modality": "image_description", "rollout": "current"},
            "native-candidate": {
                "modality": "image_native_and_multilingual_text",
                "rollout": "report_only",
                "source_span_caveat": "OCR/caption/source spans must remain visible before rollout.",
            },
        },
    )

    report = compare_embedding_profiles(
        profiles,
        baseline_profile="text-description",
        top_k=3,
        min_recall=1.0,
        min_mrr=1.0,
        min_ndcg=1.0,
        max_top1_change_rate=1.0,
    )

    baseline = report["profiles"]["text-description"]
    candidate = report["profiles"]["native-candidate"]
    assert report["summary"]["passed"] is False
    assert report["summary"]["failure_counts"] == {"native-candidate:top1_changed": 2}
    assert baseline["query_dimensions"]["visual_native"]["mrr"] == 0.5
    assert baseline["query_dimensions"]["multilingual"]["forbidden_hit_count"] == 1
    assert candidate["query_dimensions"] == {
        "multilingual": {
            "record_count": 1,
            "recall_at_k": 1.0,
            "mrr": 1.0,
            "ndcg_at_k": 1.0,
            "forbidden_hit_count": 0,
            "top1_change_rate": 1.0,
        },
        "visual_native": {
            "record_count": 1,
            "recall_at_k": 1.0,
            "mrr": 1.0,
            "ndcg_at_k": 1.0,
            "forbidden_hit_count": 0,
            "top1_change_rate": 1.0,
        },
    }
    assert candidate["modality_mix"] == {
        "image_description": 1,
        "image_native": 1,
        "multilingual_text": 3,
        "ocr_text": 1,
    }


def test_materialize_live_capture_pack_preserves_provenance_and_metrics(tmp_path: Path) -> None:
    materialized = materialize_live_capture_pack(
        LIVE_CAPTURE_DIR / "sar607_live_capture_pack.json",
        tmp_path / "captures",
    )
    profiles = read_profile_captures(
        materialized.profile_specs,
        profile_metadata=parse_profile_metadata(materialized.profile_metadata_specs),
    )

    report = compare_embedding_profiles(
        profiles,
        baseline_profile="text-description",
        top_k=4,
        min_recall=1.0,
        min_mrr=1.0,
        min_ndcg=1.0,
        max_top1_change_rate=0.75,
    )

    baseline = report["profiles"]["text-description"]
    candidate = report["profiles"]["native-candidate"]
    assert report["summary"]["passed"] is False
    assert report["summary"]["failure_counts"] == {"native-candidate:top1_changed": 3}
    assert materialized.manifest["query_count"] == 4
    assert baseline["query_dimensions"]["visual_native"]["forbidden_hit_count"] == 2
    assert baseline["query_dimensions"]["multilingual"]["mrr"] == 0.75
    assert baseline["provenance"] == {
        "result_count": 10,
        "with_caption": 2,
        "with_ocr_text": 2,
        "with_source_item_id": 6,
        "with_source_span": 6,
    }
    assert candidate["query_dimensions"]["visual_native"]["mrr"] == 1.0
    assert candidate["query_dimensions"]["multilingual"]["forbidden_hit_count"] == 0
    assert candidate["modality_mix"] == {
        "image_description": 2,
        "image_native": 2,
        "multilingual_text": 3,
    }
    assert candidate["provenance"]["with_source_span"] == 4


def test_sar611_dogfood_capture_pack_expands_multimodal_multilingual_evidence(tmp_path: Path) -> None:
    materialized = materialize_live_capture_pack(
        LIVE_CAPTURE_DIR / "sar611_dogfood_capture_pack.json",
        tmp_path / "captures",
    )
    profiles = read_profile_captures(
        materialized.profile_specs,
        profile_metadata=parse_profile_metadata(materialized.profile_metadata_specs),
    )

    report = compare_embedding_profiles(
        profiles,
        baseline_profile="text-description",
        top_k=4,
        min_recall=1.0,
        min_mrr=1.0,
        min_ndcg=1.0,
        max_top1_change_rate=0.75,
    )

    baseline = report["profiles"]["text-description"]
    candidate = report["profiles"]["native-candidate"]
    assert report["summary"]["passed"] is False
    assert report["summary"]["failure_counts"] == {
        "native-candidate:top1_change_rate_above_threshold": 1,
        "native-candidate:top1_changed": 7,
    }
    assert materialized.manifest["capture_set"] == "sar611-expanded-dogfood-multimodal-multilingual"
    assert materialized.manifest["query_count"] == 8
    assert baseline["query_dimensions"]["visual_native"]["record_count"] == 4
    assert baseline["query_dimensions"]["multilingual"]["record_count"] == 4
    assert baseline["query_dimensions"]["visual_native"]["forbidden_hit_count"] == 4
    assert baseline["query_dimensions"]["multilingual"]["forbidden_hit_count"] == 4
    assert candidate["query_dimensions"]["visual_native"]["mrr"] == 1.0
    assert candidate["query_dimensions"]["multilingual"]["mrr"] == 1.0
    assert candidate["query_dimensions"]["visual_native"]["top1_change_rate"] == 1.0
    assert candidate["query_dimensions"]["multilingual"]["top1_change_rate"] == 0.75
    assert candidate["metrics"]["top1_change_rate"] == 0.875
    assert candidate["provenance"]["with_source_item_id"] == 8
    assert candidate["provenance"]["with_source_span"] == 8


def test_sar655_visual_dogfood_pack_compares_text_ocr_and_native_profiles(tmp_path: Path) -> None:
    materialized = materialize_live_capture_pack(
        LIVE_CAPTURE_DIR / "sar655_visual_dogfood_capture_pack.json",
        tmp_path / "captures",
    )
    profiles = read_profile_captures(
        materialized.profile_specs,
        profile_metadata=parse_profile_metadata(materialized.profile_metadata_specs),
    )

    report = compare_embedding_profiles(
        profiles,
        baseline_profile="text-description",
        top_k=4,
        min_recall=1.0,
        min_mrr=1.0,
        min_ndcg=1.0,
        max_top1_change_rate=1.0,
    )

    ocr_candidate = report["profiles"]["ocr-caption-candidate"]
    native_candidate = report["profiles"]["native-image-candidate"]
    assert materialized.manifest["capture_set"] == "sar655-expanded-visual-dogfood"
    assert materialized.manifest["query_count"] == 12
    assert report["summary"]["profiles"] == [
        "text-description",
        "ocr-caption-candidate",
        "native-image-candidate",
    ]
    assert report["profiles"]["text-description"]["query_dimensions"]["diagram"]["record_count"] == 2
    assert report["profiles"]["text-description"]["query_dimensions"]["dense_ocr"]["record_count"] == 2
    assert ocr_candidate["query_dimensions"]["receipt"]["mrr"] == 1.0
    assert ocr_candidate["query_dimensions"]["diagram"]["mrr"] == 0.5
    assert native_candidate["query_dimensions"]["diagram"]["mrr"] == 1.0
    assert native_candidate["query_dimensions"]["x_post_image"]["mrr"] == 1.0
    assert native_candidate["metrics"]["top1_change_rate"] == 1.0
    assert native_candidate["metrics"]["forbidden_hit_count"] == 0
    assert native_candidate["provenance"]["with_source_item_id"] == 36
    assert native_candidate["provenance"]["with_source_span"] == 36


def test_native_image_provider_capture_report_stays_report_only() -> None:
    report = build_native_image_provider_capture_report(
        profile_name="local-http-clip-native-image-768",
        image_references=["file:///tmp/invoice.png", "file:///tmp/diagram.png"],
        vectors=[[0.1] * 768, [0.2] * 768],
        latency_ms=42.5,
    )

    assert report["report_kind"] == "native_image_provider_capture"
    assert report["report_only"] is True
    assert report["storage_mutation"] is False
    assert report["default_change"] is False
    assert report["profile"]["profile_kind"] == "native_image"
    assert report["profile"]["input_modality"] == "image"
    assert report["profile"]["enabled_by_default"] is False
    assert report["capture"]["dimension_counts"] == {"768": 2}
    assert report["readiness"] == {
        "passed": True,
        "default_enablement_blocked": True,
        "required_before_default_enablement": [
            "compare this provider capture against the SAR-611 dogfood pack",
            "accept top-rank drift explicitly",
            "keep text-description retrieval as the fallback until rollout approval",
        ],
    }


def test_native_image_provider_capture_report_rejects_dimension_mismatch() -> None:
    report = build_native_image_provider_capture_report(
        profile_name="local-http-clip-native-image-768",
        image_references=["file:///tmp/invoice.png"],
        vectors=[[0.1] * 384],
    )

    assert report["readiness"]["passed"] is False
    assert report["capture"]["mismatched_dimensions"] == {"384": 1}


def test_native_image_provider_capture_report_rejects_non_native_profile() -> None:
    with pytest.raises(EmbeddingProfileEvalInputError, match="not a native image profile"):
        build_native_image_provider_capture_report(
            profile_name="local-http-gte-modernbert-base",
            image_references=["file:///tmp/invoice.png"],
            vectors=[[0.1] * 768],
        )


def test_materialize_live_capture_pack_rejects_output_filename_collisions(tmp_path: Path) -> None:
    pack_path = tmp_path / "pack.json"
    pack_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "capture_set": "collision-pack",
                "corpus_id": "collision-v1",
                "profiles": [
                    {"name": "native candidate"},
                    {"name": "native-candidate"},
                ],
                "queries": [
                    {
                        "query_fingerprint": "same-file",
                        "profiles": {
                            "native candidate": {"results": []},
                            "native-candidate": {"results": []},
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(EmbeddingProfileEvalInputError, match="same output filename"):
        materialize_live_capture_pack(pack_path, tmp_path / "out")


def test_compare_embedding_profiles_enforces_profile_thresholds() -> None:
    profiles = _profiles()
    profiles[1] = EmbeddingProfileCapture(
        name="bge-small-en-v1.5",
        records=[
            {
                **record,
                "results": list(reversed(record["results"])),
                "latency_ms": 10.0,
                "fallback_used": False,
            }
            for record in profiles[0].records
        ],
        metadata={"provider": "local-http", "model": "bge-small-en-v1.5", "dimensions": 384},
    )

    report = compare_embedding_profiles(
        profiles,
        baseline_profile="openai",
        top_k=2,
        max_top1_change_rate=0.0,
    )

    assert report["summary"]["passed"] is False
    assert report["summary"]["failure_counts"] == {
        "bge-small-en-v1.5:top1_change_rate_above_threshold": 1,
        "bge-small-en-v1.5:top1_changed": 2,
    }
    assert report["profiles"]["bge-small-en-v1.5"]["metrics"]["top1_change_rate"] == 1.0


def test_read_profile_captures_rejects_duplicate_names() -> None:
    spec = f"openai={FIXTURE_DIR / 'baseline.ndjson'}"

    with pytest.raises(EmbeddingProfileEvalInputError, match="duplicate profile name"):
        read_profile_captures([spec, spec])


def test_parse_profile_metadata_reads_json_files(tmp_path: Path) -> None:
    metadata_path = tmp_path / "openai.json"
    metadata_path.write_text(
        json.dumps({"provider": "openai", "model": "text-embedding-3-small", "dimensions": 1536}),
        encoding="utf-8",
    )

    assert parse_profile_metadata([f"openai={metadata_path}"]) == {
        "openai": {"provider": "openai", "model": "text-embedding-3-small", "dimensions": 1536}
    }


def test_benchmark_embedding_profiles_script_writes_report(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    args = benchmark_module.build_parser().parse_args(
        [
            "compare",
            "--profile",
            f"openai={FIXTURE_DIR / 'baseline.ndjson'}",
            "--profile",
            f"gte-modernbert-base={FIXTURE_DIR / 'current.ndjson'}",
            "--baseline-profile",
            "openai",
            "--top-k",
            "2",
            "--output",
            str(report_path),
        ]
    )

    assert benchmark_module.cmd_compare(args) == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["summary"]["profiles"] == ["openai", "gte-modernbert-base"]
    assert report["profiles"]["gte-modernbert-base"]["metrics"]["top1_stability"] == 1.0


def test_benchmark_embedding_profiles_live_pack_writes_report_and_manifest(tmp_path: Path) -> None:
    report_path = tmp_path / "sar607-report.json"
    output_dir = tmp_path / "captures"
    args = benchmark_module.build_parser().parse_args(
        [
            "live-pack",
            "--pack",
            str(LIVE_CAPTURE_DIR / "sar607_live_capture_pack.json"),
            "--output-dir",
            str(output_dir),
            "--baseline-profile",
            "text-description",
            "--top-k",
            "4",
            "--min-recall",
            "1.0",
            "--min-mrr",
            "1.0",
            "--min-ndcg",
            "1.0",
            "--max-top1-change-rate",
            "0.75",
            "--output",
            str(report_path),
        ]
    )

    assert benchmark_module.cmd_live_pack(args) == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    assert report["summary"]["profiles"] == ["text-description", "native-candidate"]
    assert manifest["capture_set"] == "sar607-live-multimodal-multilingual"
    assert (output_dir / "text-description.ndjson").exists()
    assert (output_dir / "native-candidate.ndjson").exists()


def test_benchmark_embedding_profiles_native_image_provider_capture_writes_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path = tmp_path / "native-provider-report.json"

    class FakeEmbeddingService:
        def __init__(self) -> None:
            self.profile = type("Profile", (), {"profile_name": "local-http-clip-native-image-768"})()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc_info):
            return None

        async def embed_image_references(self, image_references: list[str]) -> list[list[float]]:
            return [[0.1] * 768 for _reference in image_references]

    monkeypatch.setattr(benchmark_module, "_embedding_service_factory", FakeEmbeddingService)
    args = benchmark_module.build_parser().parse_args(
        [
            "native-image-provider-capture",
            "--profile-name",
            "local-http-clip-native-image-768",
            "--image-reference",
            "file:///tmp/invoice.png",
            "--output",
            str(report_path),
        ]
    )

    assert benchmark_module.cmd_native_image_provider_capture(args) == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["report_kind"] == "native_image_provider_capture"
    assert report["capture"]["vector_count"] == 1
    assert report["readiness"]["default_enablement_blocked"] is True


def test_benchmark_embedding_profiles_native_image_provider_capture_reports_dimension_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path = tmp_path / "native-provider-report.json"

    class FakeEmbeddingService:
        def __init__(self) -> None:
            self.profile = type("Profile", (), {"profile_name": "local-http-clip-native-image-768"})()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc_info):
            return None

        async def embed_image_references(self, image_references: list[str]) -> list[list[float]]:
            return [[0.1] * 384 for _reference in image_references]

    monkeypatch.setattr(benchmark_module, "_embedding_service_factory", FakeEmbeddingService)
    args = benchmark_module.build_parser().parse_args(
        [
            "native-image-provider-capture",
            "--profile-name",
            "local-http-clip-native-image-768",
            "--image-reference",
            "file:///tmp/invoice.png",
            "--output",
            str(report_path),
        ]
    )

    assert benchmark_module.cmd_native_image_provider_capture(args) == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["capture"]["mismatched_dimensions"] == {"384": 1}
    assert report["readiness"]["passed"] is False
