import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).resolve().parents[2] / "scripts"


def _load_script(name: str):
    script_path = SCRIPT_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


if "benchmark_secondbrain_staging" not in sys.modules:
    _load_script("benchmark_secondbrain_staging")

base_benchmark = sys.modules["benchmark_secondbrain_staging"]
nist_benchmark = _load_script("benchmark_nist_sp800_staging")


def test_validate_nist_run_id_accepts_idempotency_key_boundary() -> None:
    run_id = "r" * nist_benchmark.MAX_NIST_RUN_ID_LENGTH

    assert nist_benchmark.validate_run_id(run_id) == run_id

    key = nist_benchmark.build_nist_idempotency_key(run_id, 0)
    assert len(key) == nist_benchmark.MAX_IDEMPOTENCY_KEY_LENGTH


def test_validate_nist_run_id_rejects_idempotency_key_overflow() -> None:
    run_id = "r" * (nist_benchmark.MAX_NIST_RUN_ID_LENGTH + 1)

    with pytest.raises(SystemExit) as exc_info:
        nist_benchmark.validate_run_id(run_id)

    assert f"at most {nist_benchmark.MAX_NIST_RUN_ID_LENGTH} chars" in str(exc_info.value)
    assert "items.idempotency_key" in str(exc_info.value)


def test_nist_entry_idempotency_key_fits_database_column() -> None:
    run_id = "r" * nist_benchmark.MAX_NIST_RUN_ID_LENGTH
    chunk = nist_benchmark.CorpusChunk(
        index=249,
        publication_id="800-207",
        publication_title="Zero Trust Architecture",
        publication_year="2020",
        doi="10.6028/NIST.SP.800-207",
        doi_url="https://doi.org/10.6028/NIST.SP.800-207",
        pdf_url="https://example.test/nist.pdf",
        source_text_sha256="abc123",
        chunk_index=1,
        chunk_offset=0,
        chunk_text="zero trust policy engine policy administrator",
    )

    entry = nist_benchmark.make_entry(
        run_id,
        "tenant-a",
        chunk,
        enable_ai_enrichment=False,
        relationship_policy="deferred",
    )

    assert len(entry["idempotency_key"]) == nist_benchmark.MAX_IDEMPOTENCY_KEY_LENGTH


def test_secondbrain_ingest_rejects_duplicate_active_run(monkeypatch, tmp_path: Path) -> None:
    run_id = "duplicate-run"
    monkeypatch.setattr(base_benchmark, "RUN_DIR", tmp_path)
    monkeypatch.setattr(base_benchmark, "client_from_args", lambda _args: object())
    monkeypatch.setattr(base_benchmark, "resolve_tenant_id", lambda _client, _requested: "tenant-a")

    args = argparse.Namespace(
        run_id=run_id,
        tenant_id=None,
        count=1,
        concurrency=1,
        timeout=1.0,
        progress_every=1,
        enable_ai_enrichment=False,
        resume=False,
        dry_run=True,
        keep_going=False,
    )

    with base_benchmark.acquire_run_artifact_lock(run_id, purpose="unit test"):
        with pytest.raises(SystemExit) as exc_info:
            base_benchmark.cmd_ingest(args)

    assert "benchmark run artifact lock is already held" in str(exc_info.value)
    assert str(base_benchmark.run_artifact_lock_path(run_id)) in str(exc_info.value)


def test_nist_ingest_rejects_duplicate_active_run(monkeypatch, tmp_path: Path) -> None:
    run_id = "nist-duplicate-run"
    monkeypatch.setattr(base_benchmark, "RUN_DIR", tmp_path)
    monkeypatch.setattr(nist_benchmark, "RUN_DIR", tmp_path)
    monkeypatch.setattr(nist_benchmark, "client_from_args", lambda _args: object())
    monkeypatch.setattr(nist_benchmark, "resolve_tenant_id", lambda _client, _requested: "tenant-a")

    args = argparse.Namespace(
        run_id=run_id,
        tenant_id=None,
    )

    with base_benchmark.acquire_run_artifact_lock(run_id, namespace="nist-sp800", purpose="unit test"):
        with pytest.raises(SystemExit) as exc_info:
            nist_benchmark.cmd_ingest(args)

    assert "benchmark run artifact lock is already held" in str(exc_info.value)
    assert str(base_benchmark.run_artifact_lock_path(run_id, namespace="nist-sp800")) in str(exc_info.value)


def test_relationship_queue_summary_normalizes_control_tower_metrics() -> None:
    summary = nist_benchmark.relationship_queue_summary(
        {
            "worker_backpressure": {
                "queues": [
                    {"key": "memory", "queued_depth": 3},
                    {
                        "key": "relationships",
                        "label": "Relationships",
                        "queued_depth": "142",
                        "deferred_depth": 0,
                        "worker_queue_depth": 1.0,
                        "recent_failed": False,
                        "telemetry_error": None,
                    },
                ]
            }
        }
    )

    assert summary["queued_depth"] == 142
    assert summary["worker_queue_depth"] == 1
    assert nist_benchmark.relationship_queue_needs_wait(summary) is True


def test_wait_for_relationship_queue_drained_polls_until_clean(monkeypatch) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.responses = [
                {"worker_backpressure": {"queues": [{"key": "relationships", "queued_depth": 2}]}},
                {"worker_backpressure": {"queues": [{"key": "relationships", "queued_depth": 0}]}},
            ]
            self.calls = 0

        def request(self, *_args, **_kwargs):
            response = self.responses[min(self.calls, len(self.responses) - 1)]
            self.calls += 1
            return response

    sleeps: list[int] = []
    monkeypatch.setattr(nist_benchmark.time, "sleep", sleeps.append)

    client = FakeClient()
    rc = nist_benchmark.wait_for_relationship_queue_drained(
        client,
        timeout_seconds=60,
        interval_seconds=5,
    )

    assert rc == 0
    assert client.calls == 2
    assert sleeps == [5]


def test_result_expected_rank_returns_first_expected_publication_rank() -> None:
    results = [
        {
            "title": "NIST SP 800-39 - Managing Information Security Risk",
            "source_url": "https://doi.org/10.6028/NIST.SP.800-39",
            "chunk_text": "organization risk",
        },
        {
            "title": "NIST SP 800-37r2 - Risk Management Framework",
            "source_url": "https://doi.org/10.6028/NIST.SP.800-37r2",
            "chunk_text": "categorize select implement assess authorize monitor",
        },
    ]

    assert nist_benchmark.result_expected_rank(results, ["800-37r2"]) == 2
    assert nist_benchmark.result_expected_rank(results, ["800-145"]) is None


def test_build_contrastive_eval_packs_prefers_neighboring_decoys() -> None:
    publications = [
        {
            "publication_id": "800-37r2",
            "title": "Risk Management Framework for Information Systems and Organizations",
        },
        {
            "publication_id": "800-39",
            "title": "Managing Information Security Risk",
        },
        {
            "publication_id": "800-82r3",
            "title": "Guide to Operational Technology Security",
        },
    ]

    packs = nist_benchmark.build_contrastive_eval_packs(
        publications,
        [
            {
                "id": "risk-management-framework",
                "query": "risk management framework categorize select implement assess authorize monitor",
                "expected_publications": ["800-37r2"],
            }
        ],
    )

    assert packs == [
        {
            "id": "risk-management-framework-contrastive",
            "source_eval_id": "risk-management-framework",
            "question_types": ["adjacent-publication-confusion"],
            "expected_publications": ["800-37r2"],
            "neighboring_decoys": [
                {
                    "publication_id": "800-39",
                    "title": "Managing Information Security Risk",
                    "overlap_terms": ["risk"],
                },
                {
                    "publication_id": "800-82r3",
                    "title": "Guide to Operational Technology Security",
                    "overlap_terms": [],
                },
            ],
            "probes": [
                {
                    "id": "risk-management-framework-contrastive-title",
                    "query": (
                        "Risk Management Framework for Information Systems and Organizations risk management "
                        "framework categorize select implement assess authorize monitor contrast with "
                        "Managing Security Guide to Operational Technology"
                    ),
                    "question_type": "adjacent-publication-confusion",
                    "expected_publications": ["800-37r2"],
                    "decoy_publications": ["800-39", "800-82r3"],
                }
            ],
        }
    ]


def _authority_publication(publication_id: str, title: str) -> dict:
    return {
        "publication_id": publication_id,
        "title": title,
        "year": "2024",
        "doi": f"10.6028/NIST.SP.{publication_id}",
        "doi_url": f"https://doi.org/10.6028/NIST.SP.{publication_id}",
        "pdf_url": f"https://example.test/{publication_id}.pdf",
        "source_text_sha256": f"sha-{publication_id}",
        "chunks": 1,
        "selected_chunks": [{"chunk_index": 0, "chunk_offset": 0}],
    }


def _authority_publications_for_cases() -> list[dict]:
    return [
        _authority_publication(
            case["expected_governing_source"],
            f"NIST SP {case['expected_governing_source']}",
        )
        for case in nist_benchmark.AUTHORITY_EVAL_CASES
    ]


def test_build_authority_eval_packs_encodes_documented_cases() -> None:
    publications = [
        _authority_publication("800-37r2", "Risk Management Framework for Information Systems"),
        _authority_publication("800-39", "Managing Information Security Risk"),
        _authority_publication("800-30r1", "Guide for Conducting Risk Assessments"),
        _authority_publication("800-53r5", "Security and Privacy Controls"),
        _authority_publication("800-53Ar5", "Assessing Security and Privacy Controls"),
        _authority_publication("800-53B", "Control Baselines for Information Systems"),
        _authority_publication("800-144", "Guidelines on Security and Privacy in Public Cloud Computing"),
        _authority_publication("800-145", "The NIST Definition of Cloud Computing"),
        _authority_publication("800-207", "Zero Trust Architecture"),
        _authority_publication("800-160v1r1", "Systems Security Engineering"),
        _authority_publication("800-160v2r1", "Developing Cyber-Resilient Systems"),
        _authority_publication("800-161r1", "Cybersecurity Supply Chain Risk Management Practices"),
        _authority_publication("800-218", "Secure Software Development Framework"),
    ]

    packs = nist_benchmark.build_authority_eval_packs(
        {
            "run_id": "testrun",
            "run_tag": "benchmark-run-testrun",
            "relationship_policy": "deferred",
            "publications": publications,
        }
    )

    assert len(packs) == 12
    assert [pack["id"] for pack in packs][:3] == [
        "rmf-steps-governing-source",
        "enterprise-risk-context-source",
        "risk-assessment-process-source",
    ]
    assert packs[0]["expected_governing_source"] == "800-37r2"
    assert packs[0]["confusing_sources_to_demote"] == ["800-39", "800-82r3"]
    assert packs[0]["missing_confusing_sources"] == ["800-82r3"]
    assert packs[0]["expected_support"] == [
        {
            "publication_id": "800-37r2",
            "publication_title": "Risk Management Framework for Information Systems",
            "publication_year": "2024",
            "doi": "10.6028/NIST.SP.800-37r2",
            "doi_url": "https://doi.org/10.6028/NIST.SP.800-37r2",
            "pdf_url": "https://example.test/800-37r2.pdf",
            "source_text_sha256": "sha-800-37r2",
            "chunk_index": 0,
            "chunk_offset": 0,
            "benchmark_run_tag": "benchmark-run-testrun",
            "relationship_policy": "deferred",
        }
    ]
    assert packs[0]["support_contract"]["requires_weak_support_state"] is True


def test_summarize_authority_eval_pack_readiness_reports_complete_manifest() -> None:
    manifest = {
        "run_id": "testrun",
        "run_tag": "benchmark-run-testrun",
        "relationship_policy": "deferred",
        "publications": _authority_publications_for_cases(),
    }
    manifest["authority_eval_packs"] = nist_benchmark.build_authority_eval_packs(manifest)

    summary = nist_benchmark.summarize_authority_eval_pack_readiness(manifest)

    assert summary["pack_count"] == 12
    assert summary["case_ids"][0] == "rmf-steps-governing-source"
    assert summary["governing_source_coverage"]["covered"] == 12
    assert summary["governing_source_coverage"]["expected"] == 12
    assert summary["missing_case_ids"] == []
    assert summary["missing_metadata"] == []
    assert summary["missing_chunk_case_ids"] == []
    assert summary["ready_for_answer_support_validation"] is True
    assert summary["warnings"] == []


def test_summarize_authority_eval_pack_readiness_warns_on_missing_metadata() -> None:
    publication = _authority_publication(
        "800-37r2",
        "Risk Management Framework for Information Systems",
    )
    publication.pop("source_text_sha256")
    publication["selected_chunks"] = [{"chunk_index": 0}]
    manifest = {
        "run_id": "testrun",
        "run_tag": "benchmark-run-testrun",
        "relationship_policy": "deferred",
        "publications": [publication],
        "authority_eval_packs": [
            {
                "id": "rmf-steps-governing-source",
                "expected_governing_source": "800-37r2",
            }
        ],
    }
    warnings: list[str] = []

    summary = nist_benchmark.summarize_authority_eval_pack_readiness(
        manifest,
        warnings=warnings,
    )

    assert summary["pack_count"] == 1
    assert summary["ready_for_answer_support_validation"] is False
    assert summary["missing_metadata"] == [
        {
            "case_id": "rmf-steps-governing-source",
            "publication_id": "800-37r2",
            "fields": ["source_text_sha256"],
        }
    ]
    assert summary["missing_chunk_case_ids"] == ["rmf-steps-governing-source"]
    assert summary["malformed_pack_ids"] == ["rmf-steps-governing-source"]
    assert any("missing metadata: source_text_sha256" in warning for warning in warnings)
    assert any("is missing expected_support" in warning for warning in warnings)


def test_build_authority_eval_packs_fails_without_chunk_location() -> None:
    publication = _authority_publication(
        "800-37r2",
        "Risk Management Framework for Information Systems",
    )
    publication.pop("selected_chunks")

    with pytest.raises(SystemExit) as exc_info:
        nist_benchmark.build_authority_eval_packs(
            {
                "run_id": "testrun",
                "run_tag": "benchmark-run-testrun",
                "relationship_policy": "deferred",
                "publications": [publication],
            }
        )

    assert "missing required manifest metadata selected_chunks" in str(exc_info.value)


def test_write_manifest_includes_contrastive_eval_packs(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(nist_benchmark, "RUN_DIR", tmp_path)
    chunks = [
        nist_benchmark.CorpusChunk(
            index=0,
            publication_id="800-37r2",
            publication_title="Risk Management Framework for Information Systems and Organizations",
            publication_year="2018",
            doi="10.6028/NIST.SP.800-37r2",
            doi_url="https://doi.org/10.6028/NIST.SP.800-37r2",
            pdf_url="https://example.test/800-37r2.pdf",
            source_text_sha256="abc123",
            chunk_index=0,
            chunk_offset=0,
            chunk_text="categorize select implement assess authorize monitor",
        ),
        nist_benchmark.CorpusChunk(
            index=1,
            publication_id="800-39",
            publication_title="Managing Information Security Risk",
            publication_year="2011",
            doi="10.6028/NIST.SP.800-39",
            doi_url="https://doi.org/10.6028/NIST.SP.800-39",
            pdf_url="https://example.test/800-39.pdf",
            source_text_sha256="def456",
            chunk_index=0,
            chunk_offset=0,
            chunk_text="organization mission business process risk",
        ),
    ]

    nist_benchmark.write_manifest(
        "testrun",
        chunks,
        [],
        argparse.Namespace(
            target_count=2,
            chunks_per_document=1,
            chunk_chars=2000,
            overlap_chars=200,
            relationship_policy="deferred",
            enable_ai_enrichment=False,
        ),
    )

    manifest = json.loads((tmp_path / "testrun-nist-corpus-manifest.json").read_text(encoding="utf-8"))

    assert [query["id"] for query in manifest["eval_queries"]] == [
        "risk-management-framework",
        "enterprise-risk",
    ]
    assert [pack["id"] for pack in manifest["contrastive_eval_packs"]] == [
        "risk-management-framework-contrastive",
        "enterprise-risk-contrastive",
    ]
    assert manifest["contrastive_eval_packs"][0]["neighboring_decoys"][0]["publication_id"] == "800-39"
    assert [pack["id"] for pack in manifest["authority_eval_packs"]] == [
        "rmf-steps-governing-source",
        "enterprise-risk-context-source",
    ]
    assert manifest["authority_eval_packs"][0]["expected_support"][0]["chunk_index"] == 0


def test_append_eval_packs_updates_existing_manifest(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(nist_benchmark, "RUN_DIR", tmp_path)
    manifest_path = tmp_path / "testrun-nist-corpus-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "benchmark": "nist-sp800-corpus",
                "run_id": "testrun",
                "publications": [
                    {
                        "publication_id": "800-37r2",
                        "title": "Risk Management Framework for Information Systems and Organizations",
                    },
                    {
                        "publication_id": "800-39",
                        "title": "Managing Information Security Risk",
                    },
                ],
                "eval_queries": [
                    {
                        "id": "risk-management-framework",
                        "query": "risk management framework categorize select implement assess authorize monitor",
                        "expected_publications": ["800-37r2"],
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    rc = nist_benchmark.cmd_append_eval_packs(argparse.Namespace(run_id="testrun"))

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert rc == 0
    assert manifest["contrastive_eval_packs"][0]["id"] == "risk-management-framework-contrastive"


def test_append_authority_eval_packs_updates_existing_manifest(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(nist_benchmark, "RUN_DIR", tmp_path)
    manifest_path = tmp_path / "testrun-nist-corpus-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "benchmark": "nist-sp800-corpus",
                "run_id": "testrun",
                "run_tag": "benchmark-run-testrun",
                "relationship_policy": "deferred",
                "publications": [
                    _authority_publication(
                        "800-37r2",
                        "Risk Management Framework for Information Systems and Organizations",
                    )
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    rc = nist_benchmark.cmd_append_authority_eval_packs(argparse.Namespace(run_id="testrun"))

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert rc == 0
    assert manifest["authority_eval_packs"][0]["id"] == "rmf-steps-governing-source"


def test_prepare_hf_authority_corpus_writes_offline_manifest(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(nist_benchmark, "RUN_DIR", tmp_path)
    input_path = tmp_path / "train.jsonl"
    input_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "id": 1,
                        "text": "NIST SP 800-37r2 risk management framework authorize monitor.",
                        "embedding": [0.1] * nist_benchmark.HF_NIST_EMBEDDING_DIMENSION,
                        "metadata": json.dumps(
                            {
                                "source": "NIST SP 800-37r2 Risk Management Framework",
                                "type": "semantic_chunk",
                                "chunk_id": 4,
                                "doi": "10.6028/NIST.SP.800-37r2",
                            }
                        ),
                    }
                ),
                json.dumps(
                    {
                        "id": 2,
                        "text": "NIST SP 800-39 organization risk mission business process.",
                        "embedding": [0.2] * nist_benchmark.HF_NIST_EMBEDDING_DIMENSION,
                        "metadata": {
                            "source": "NIST SP 800-39 Managing Information Security Risk",
                            "type": "section",
                            "chunk_id": 0,
                            "source_url": "https://doi.org/10.6028/NIST.SP.800-39",
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    rc = nist_benchmark.cmd_prepare_hf_authority_corpus(
        argparse.Namespace(
            run_id="hf-smoke",
            input=[f"train={input_path}"],
            sample_limit=2,
            output=None,
        )
    )

    manifest = json.loads((tmp_path / "hf-smoke-nist-hf-corpus-manifest.json").read_text(encoding="utf-8"))
    assert rc == 0
    assert manifest["benchmark"] == "nist-hf-authority-corpus"
    assert manifest["source"]["dataset_id"] == "ethanolivertroy/nist-cybersecurity-training"
    assert manifest["source"]["expected_total_rows"] == 530912
    assert manifest["precomputed_embedding_trust"] == {
        "decision": "Use dataset-provided embeddings for offline authority-eval preparation.",
        "dimension": 1536,
        "model": "text-embedding-3-small",
        "openai_embedding_calls": 0,
        "trusted": True,
    }
    assert manifest["observed"]["rows"] == 2
    assert manifest["observed"]["split_rows"] == {"train": 2}
    assert manifest["observed"]["embedding_dimensions"] == [1536]
    assert manifest["observed"]["token_count"] == 16
    assert manifest["records"][0]["publication_id"] == "800-37r2"
    assert manifest["records"][0]["source_url"] == "https://doi.org/10.6028/NIST.SP.800-37r2"
    assert manifest["records"][0]["text_sha256"]
    assert manifest["records"][0]["embedding_model"] == "text-embedding-3-small"
    assert manifest["records"][0]["embedding_dimension"] == 1536
    assert manifest["publications"][0]["selected_chunks"][0]["chunk_index"] == 4
    assert manifest["validation"]["token_count_source"].startswith("computed from local input text")
    assert manifest["authority_report"]["offline_report_only"] is True
    assert manifest["authority_report"]["governing_source_coverage"]["covered"] == 2
    assert manifest["authority_report"]["weak_support"]["cases"] == 10
    assert manifest["authority_report"]["provenance"]["missing_provenance_cases"] == 0


def test_summarize_hf_authority_manifest_validation_reports_ready_fixture(tmp_path) -> None:
    input_path = tmp_path / "train.jsonl"
    rows = []
    for index, case in enumerate(nist_benchmark.AUTHORITY_EVAL_CASES):
        publication_id = case["expected_governing_source"]
        rows.append(
            {
                "id": index,
                "text": f"NIST SP {publication_id} authoritative support text.",
                "embedding": [0.1] * nist_benchmark.HF_NIST_EMBEDDING_DIMENSION,
                "metadata": {
                    "source": f"NIST SP {publication_id} source",
                    "type": "semantic_chunk",
                    "chunk_id": index,
                    "doi": f"10.6028/NIST.SP.{publication_id}",
                },
            }
        )
    input_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    manifest = nist_benchmark.prepare_hf_nist_authority_manifest(
        run_id="hf-ready",
        inputs=[f"train={input_path}"],
        sample_limit=len(rows),
    )

    report = nist_benchmark.summarize_hf_authority_manifest_validation(manifest)

    assert report["offline_report_only"] is True
    assert report["ready_for_answer_support_validation"] is True
    assert report["governing_source_coverage"]["covered"] == 12
    assert report["governing_source_coverage"]["missing_publication_ids"] == []
    assert report["weak_support"]["cases"] == 0
    assert report["provenance"]["missing_provenance_cases"] == 0
    assert report["adjacent_source_confusion"]["cases_with_confusing_sources_present"] > 0


def test_summarize_hf_authority_manifest_validation_flags_missing_provenance() -> None:
    manifest = {
        "benchmark": "nist-hf-authority-corpus",
        "provenance_contract": {"preserved_fields": ["source_url", "text_sha256"]},
        "publications": [
            {
                "publication_id": "800-37r2",
                "source": "NIST SP 800-37r2",
                "source_url": "",
                "chunks": 1,
                "selected_chunks": [{"split": "train", "row_id": "1", "chunk_index": 0}],
            }
        ],
        "records": [
            {
                "split": "train",
                "row_id": "1",
                "publication_id": "800-37r2",
                "source": "NIST SP 800-37r2",
                "chunk_index": 0,
                "chunk_offset": 0,
                "embedding_model": "text-embedding-3-small",
                "embedding_dimension": 1536,
            }
        ],
    }

    report = nist_benchmark.summarize_hf_authority_manifest_validation(manifest)
    case = next(case for case in report["cases"] if case["id"] == "rmf-steps-governing-source")

    assert report["ready_for_answer_support_validation"] is False
    assert report["weak_support"]["weak_support_case_ids"][0] == "rmf-steps-governing-source"
    assert "source_url" in case["publication_provenance_missing_fields"]
    assert "selected_chunks[0].text_sha256" in case["publication_provenance_missing_fields"]
    assert "source_url" in case["record_provenance_missing_fields"]
    assert "text_sha256" in case["record_provenance_missing_fields"]


def test_report_hf_authority_corpus_command_writes_read_only_report(tmp_path) -> None:
    input_path = tmp_path / "train.jsonl"
    rows = []
    for index, case in enumerate(nist_benchmark.AUTHORITY_EVAL_CASES):
        publication_id = case["expected_governing_source"]
        rows.append(
            {
                "id": index,
                "text": f"NIST SP {publication_id} authoritative support text.",
                "embedding": [0.1] * nist_benchmark.HF_NIST_EMBEDDING_DIMENSION,
                "metadata": {
                    "source": f"NIST SP {publication_id} source",
                    "type": "semantic_chunk",
                    "chunk_id": index,
                    "source_url": f"https://doi.org/10.6028/NIST.SP.{publication_id}",
                },
            }
        )
    input_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    manifest = nist_benchmark.prepare_hf_nist_authority_manifest(
        run_id="hf-report",
        inputs=[f"train={input_path}"],
        sample_limit=len(rows),
    )
    manifest_path = tmp_path / "hf-report-nist-hf-corpus-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    report_path = tmp_path / "hf-report-authority-report.json"

    rc = nist_benchmark.cmd_report_hf_authority_corpus(
        argparse.Namespace(manifest=str(manifest_path), output=str(report_path))
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert rc == 0
    assert report["manifest"] == str(manifest_path)
    assert report["authority_report"]["ready_for_answer_support_validation"] is True
    assert not (tmp_path / "hf-report-nist-corpus-report.json").exists()


def test_prepare_hf_authority_corpus_fails_fast_on_embedding_dimension(tmp_path) -> None:
    input_path = tmp_path / "train.jsonl"
    input_path.write_text(
        json.dumps(
            {
                "id": 1,
                "text": "NIST text",
                "embedding": [0.1, 0.2],
                "metadata": {"source": "NIST SP 800-37r2", "type": "section", "chunk_id": 0},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc_info:
        nist_benchmark.prepare_hf_nist_authority_manifest(
            run_id="hf-bad",
            inputs=[f"train={input_path}"],
            sample_limit=1,
        )

    assert "embedding dimension 2 does not match 1536" in str(exc_info.value)


def test_run_eval_queries_reports_expected_ranks(monkeypatch) -> None:
    class FakeClient:
        pass

    def fake_client_request(_client, _method, path, *, body, **_kwargs):
        assert body["tags_mode"] == "all"
        if path == "/api/v1/search":
            return {
                "total": 1,
                "results": [
                    {
                        "title": "NIST SP 800-37r2 - Risk Management Framework",
                        "source_url": "https://doi.org/10.6028/NIST.SP.800-37r2",
                        "chunk_text": "categorize select implement assess authorize monitor",
                    }
                ],
            }
        if path == "/api/v1/memory/retrieve":
            return {
                "total": 2,
                "results": [
                    {
                        "title": "NIST SP 800-39 - Managing Information Security Risk",
                        "source_url": "https://doi.org/10.6028/NIST.SP.800-39",
                        "chunk_text": "organization risk",
                    },
                    {
                        "title": "NIST SP 800-37r2 - Risk Management Framework",
                        "source_url": "https://doi.org/10.6028/NIST.SP.800-37r2",
                        "chunk_text": "categorize select implement assess authorize monitor",
                    },
                ],
                "trace": {"fallback_used": False},
            }
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(nist_benchmark, "client_request", fake_client_request)

    reports = nist_benchmark.run_eval_queries(
        FakeClient(),
        "20260429-rel250i",
        {"publications": [{"publication_id": "800-37r2"}]},
        limit=10,
    )

    assert len(reports) == 1
    assert reports[0]["id"] == "risk-management-framework"
    assert reports[0]["search_expected_rank"] == 1
    assert reports[0]["retrieve_expected_rank"] == 2
    assert reports[0]["retrieve_expected_hit"] is True
    assert reports[0]["retrieve_top_publications"][:2] == [["80039"], ["80037r2"]]


def test_run_contrastive_eval_packs_reports_advisory_pack_summary(monkeypatch) -> None:
    class FakeClient:
        pass

    def fake_client_request(_client, _method, path, *, body, **_kwargs):
        assert body["tags"] == ["benchmark-run-20260429-rel250i", "nist-sp800"]
        assert body["tags_mode"] == "all"
        if path == "/api/v1/search":
            return {
                "total": 1,
                "results": [
                    {
                        "title": "NIST SP 800-37r2 - Risk Management Framework",
                        "source_url": "https://doi.org/10.6028/NIST.SP.800-37r2",
                        "chunk_text": "categorize select implement assess authorize monitor",
                    }
                ],
            }
        if path == "/api/v1/memory/retrieve":
            return {
                "total": 2,
                "results": [
                    {
                        "title": "NIST SP 800-39 - Managing Information Security Risk",
                        "source_url": "https://doi.org/10.6028/NIST.SP.800-39",
                        "chunk_text": "organization risk",
                    },
                    {
                        "title": "NIST SP 800-37r2 - Risk Management Framework",
                        "source_url": "https://doi.org/10.6028/NIST.SP.800-37r2",
                        "chunk_text": "categorize select implement assess authorize monitor",
                    },
                ],
                "trace": {"fallback_used": False},
            }
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(nist_benchmark, "client_request", fake_client_request)

    pack_reports = nist_benchmark.run_contrastive_eval_packs(
        FakeClient(),
        "20260429-rel250i",
        {
            "contrastive_eval_packs": [
                {
                    "id": "risk-management-framework-contrastive",
                    "source_eval_id": "risk-management-framework",
                    "question_types": ["adjacent-publication-confusion"],
                    "expected_publications": ["800-37r2"],
                    "neighboring_decoys": [
                        {
                            "publication_id": "800-39",
                            "title": "Managing Information Security Risk",
                            "overlap_terms": ["risk"],
                        }
                    ],
                    "probes": [
                        {
                            "id": "risk-management-framework-contrastive-title",
                            "query": "Risk Management Framework contrast with Managing Information Security Risk",
                            "question_type": "adjacent-publication-confusion",
                            "expected_publications": ["800-37r2"],
                            "decoy_publications": ["800-39"],
                        }
                    ],
                }
            ]
        },
        limit=10,
    )

    assert pack_reports[0]["probe_count"] == 1
    assert pack_reports[0]["search_expected_hit_ratio"] == 1.0
    assert pack_reports[0]["retrieve_expected_hit_ratio"] == 1.0
    assert pack_reports[0]["search_expected_top_rank_ratio"] == 1.0
    assert pack_reports[0]["retrieve_expected_top_rank_ratio"] == 0.0
    assert pack_reports[0]["probes"][0]["decoy_publications"] == ["800-39"]


def test_validate_authority_support_case_reports_governing_support() -> None:
    manifest = {
        "run_id": "20260429-rel250i",
        "run_tag": "benchmark-run-20260429-rel250i",
        "relationship_policy": "deferred",
        "publications": _authority_publications_for_cases(),
    }
    pack = nist_benchmark.build_authority_eval_packs(manifest)[0]

    report = nist_benchmark.validate_authority_support_case(
        pack,
        [
            {
                "title": "NIST SP 800-37r2 - Risk Management Framework",
                "source_url": "https://doi.org/10.6028/NIST.SP.800-37r2",
                "chunk_text": "categorize select implement assess authorize monitor",
            },
            {
                "title": "NIST SP 800-39 - Managing Information Security Risk",
                "source_url": "https://doi.org/10.6028/NIST.SP.800-39",
                "chunk_text": "organization risk framing",
            },
        ],
    )

    assert report["id"] == "rmf-steps-governing-source"
    assert report["status"] == "pass"
    assert report["support_metadata_ready"] is True
    assert report["governing_source_present"] is True
    assert report["governing_source_rank"] == 1
    assert report["decoy_publications_present"] == ["80039"]
    assert report["confusing_source_only"] is False
    assert report["weak_support_state"] is False


def test_validate_authority_support_case_flags_confusing_source_only_and_missing_provenance() -> None:
    pack = {
        "id": "rmf-steps-governing-source",
        "expected_governing_source": "800-37r2",
        "confusing_sources_to_demote": ["800-39"],
        "expected_support": [
            {
                "publication_id": "800-37r2",
                "publication_title": "Risk Management Framework",
                "chunk_index": 0,
                "benchmark_run_tag": "benchmark-run-20260429-rel250i",
            }
        ],
    }

    report = nist_benchmark.validate_authority_support_case(
        pack,
        [
            {
                "title": "NIST SP 800-39 - Managing Information Security Risk",
                "source_url": "https://doi.org/10.6028/NIST.SP.800-39",
                "chunk_text": "organization risk framing",
            }
        ],
    )

    assert report["status"] == "confusing_source_only"
    assert report["governing_source_present"] is False
    assert report["confusing_source_only"] is True
    assert report["weak_support_state"] is True
    assert report["missing_provenance_fields"] == [
        "chunk_offset",
        "doi_or_doi_url",
        "publication_year",
    ]


def test_run_authority_support_validation_uses_retrieve_only(monkeypatch) -> None:
    class FakeClient:
        pass

    def fake_client_request(_client, method, path, *, body, **_kwargs):
        assert method == "POST"
        assert path == "/api/v1/memory/retrieve"
        assert body["scope"] == {"type": "tenant_shared", "key": None}
        assert body["tags"] == ["benchmark-run-20260429-rel250i", "nist-sp800"]
        assert body["tags_mode"] == "all"
        return {
            "total": 1,
            "results": [
                {
                    "title": "NIST SP 800-37r2 - Risk Management Framework",
                    "source_url": "https://doi.org/10.6028/NIST.SP.800-37r2",
                    "chunk_text": "categorize select implement assess authorize monitor",
                }
            ],
        }

    manifest = {
        "run_id": "20260429-rel250i",
        "run_tag": "benchmark-run-20260429-rel250i",
        "relationship_policy": "deferred",
        "publications": [_authority_publication("800-37r2", "Risk Management Framework")],
        "authority_eval_packs": [
            {
                "id": "rmf-steps-governing-source",
                "expected_governing_source": "800-37r2",
                "confusing_sources_to_demote": ["800-39"],
                "expected_result": "Answer cites RMF steps.",
                "expected_support": [
                    {
                        "publication_id": "800-37r2",
                        "publication_title": "Risk Management Framework",
                        "publication_year": "2024",
                        "doi": "10.6028/NIST.SP.800-37r2",
                        "chunk_index": 0,
                        "chunk_offset": 0,
                        "benchmark_run_tag": "benchmark-run-20260429-rel250i",
                    }
                ],
            }
        ],
    }
    monkeypatch.setattr(nist_benchmark, "client_request", fake_client_request)

    validation = nist_benchmark.run_authority_support_validation(
        FakeClient(),
        "20260429-rel250i",
        manifest,
        limit=10,
    )

    assert validation["case_count"] == 1
    assert validation["passed_cases"] == 1
    assert validation["ready"] is True
    assert validation["cases"][0]["query"] == "Answer cites RMF steps."


def test_summarize_authority_report_promotes_operator_semantics() -> None:
    authority_eval = {
        "pack_count": 2,
        "case_ids": ["rmf-steps-governing-source", "enterprise-risk-context-source"],
        "ready_for_answer_support_validation": True,
    }
    authority_support = {
        "case_count": 2,
        "ready": False,
        "governing_source_hit_cases": 1,
        "support_metadata_ready_cases": 1,
        "weak_support_cases": 1,
        "confusing_source_only_cases": 1,
        "missing_provenance_cases": 1,
        "cases": [
            {
                "id": "rmf-steps-governing-source",
                "status": "pass",
                "expected_governing_source": "800-37r2",
                "governing_source_present": True,
                "governing_source_rank": 1,
                "decoy_publications_present": ["80039"],
                "confusing_source_only": False,
                "weak_support_state": False,
                "support_metadata_ready": True,
                "missing_provenance_fields": [],
            },
            {
                "id": "enterprise-risk-context-source",
                "status": "confusing_source_only",
                "expected_governing_source": "800-39",
                "governing_source_present": False,
                "governing_source_rank": None,
                "decoy_publications_present": ["80037r2"],
                "confusing_source_only": True,
                "weak_support_state": True,
                "support_metadata_ready": False,
                "missing_provenance_fields": ["doi_or_doi_url"],
            },
        ],
    }

    report = nist_benchmark.summarize_authority_report(authority_eval, authority_support)

    assert report["governing_source"]["hit_cases"] == 1
    assert report["governing_source"]["top_rank_cases"] == 1
    assert report["adjacent_source_demotion"] == {
        "cases_with_adjacent_sources_seen": 2,
        "demoted_cases": 1,
        "confusing_source_only_cases": 1,
    }
    assert report["weak_support"]["weak_support_case_ids"] == ["enterprise-risk-context-source"]
    assert report["provenance"]["missing_provenance_case_ids"] == ["enterprise-risk-context-source"]
    assert report["cases"][0]["adjacent_source_demoted"] is True


def test_summarize_graph_relationships_counts_edges_touching_tagged_nodes() -> None:
    graph = {
        "nodes": [
            {"id": "a", "tags": ["benchmark-run-20260501-rm-500-d", "nist-sp800"]},
            {"id": "b", "tags": ["nist-sp800"]},
            {"id": "c", "tags": ["benchmark-run-other"]},
        ],
        "edges": [
            {"source": "a", "target": "b"},
            {"source": "b", "target": "c"},
            {"source": "c", "target": "a"},
        ],
        "meta": {"orphaned_ready_items": 2},
    }

    assert nist_benchmark.summarize_graph_relationships(graph, "benchmark-run-20260501-rm-500-d") == {
        "tagged_graph_nodes": 1,
        "edges_touching_tagged_nodes": 2,
        "total_graph_nodes": 3,
        "total_graph_edges": 3,
        "orphaned_ready_items": 2,
    }


def test_retained_nist_top_rank_failures_require_rank_one_for_targeted_evals() -> None:
    failures = nist_benchmark.retained_nist_top_rank_failures(
        [
            {
                "id": "risk-management-framework",
                "retrieve_expected_rank": 2,
            },
            {
                "id": "cloud-definition",
                "retrieve_expected_rank": 1,
            },
        ]
    )

    assert failures == [
        "retained NIST eval risk-management-framework expected 800-37r2 at retrieval rank 1; got rank 2"
    ]


def test_retained_nist_top_rank_failures_pass_when_targets_rank_first() -> None:
    failures = nist_benchmark.retained_nist_top_rank_failures(
        [
            {
                "id": "risk-management-framework",
                "retrieve_expected_rank": 1,
            },
            {
                "id": "cloud-definition",
                "retrieve_expected_rank": 1,
            },
        ]
    )

    assert failures == []


def test_build_relationship_matrix_plan_is_deterministic_and_safe(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(nist_benchmark, "RUN_DIR", tmp_path)

    plan = nist_benchmark.build_relationship_matrix_plan(
        matrix_id="20260501-rm",
        target_counts=[500, 1000],
        relationship_policies=["deferred", "immediate"],
        chunks_per_document=10,
        chunk_chars=3600,
        overlap_chars=250,
        source_document_candidates=80,
        enable_ai_enrichment=False,
        eval_limit=10,
        min_expected_hit_ratio=0.5,
        relationship_queue_timeout_seconds=5400,
    )

    assert plan["benchmark"] == "nist-sp800-relationship-matrix"
    assert plan["cell_count"] == 4
    assert [cell["run_id"] for cell in plan["cells"]] == [
        "20260501-rm-500-d",
        "20260501-rm-500-i",
        "20260501-rm-1000-d",
        "20260501-rm-1000-i",
    ]
    assert all(len(cell["run_id"]) <= nist_benchmark.MAX_NIST_RUN_ID_LENGTH for cell in plan["cells"])
    assert plan["safety"] == {
        "cleanup_is_manual": True,
        "live_ingest_requires_dry_run_false": True,
        "do_not_target_hermes": True,
    }


def test_summarize_relationship_matrix_uses_existing_artifacts(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(nist_benchmark, "RUN_DIR", tmp_path)
    run_id = "20260501-rm-500-d"
    nist_benchmark.nist_artifact_path(run_id).write_text(
        "\n".join(
            [
                json.dumps({"index": 0, "accept_latency_ms": 25}),
                json.dumps({"index": 1, "accept_latency_ms": 75}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    nist_benchmark.nist_manifest_path(run_id).write_text(
        json.dumps(
            {
                "actual_count": 500,
                "publications": [
                    _authority_publication(
                        "800-37r2",
                        "Risk Management Framework for Information Systems",
                    )
                ],
                "authority_eval_packs": [
                    {
                        "id": "rmf-steps-governing-source",
                        "expected_governing_source": "800-37r2",
                        "expected_support": [
                            {
                                "publication_id": "800-37r2",
                                "publication_title": "Risk Management Framework for Information Systems",
                                "publication_year": "2024",
                                "chunk_index": 0,
                                "chunk_offset": 0,
                                "benchmark_run_tag": "benchmark-run-20260501-rm-500-d",
                            }
                        ],
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    nist_benchmark.nist_report_path(run_id).write_text(
        json.dumps(
            {
                "ready_tagged_items": 500,
                "graph_relationships": {
                    "tagged_graph_nodes": 500,
                    "edges_touching_tagged_nodes": 640,
                    "total_graph_nodes": 515,
                    "total_graph_edges": 650,
                    "orphaned_ready_items": 3,
                },
                "search_expected_hit_ratio": 1.0,
                "retrieve_expected_hit_ratio": 0.95,
                "contrastive_eval_pack_count": 12,
                "authority_support_validation": {
                    "advisory": True,
                    "cases": [
                        {
                            "id": "rmf-steps-governing-source",
                            "status": "pass",
                            "support_metadata_ready": True,
                            "governing_source_present": True,
                            "confusing_source_only": False,
                            "weak_support_state": False,
                            "missing_provenance_fields": [],
                        }
                    ],
                },
                "worker_backpressure": {
                    "queues": [
                        {
                            "key": "relationships",
                            "label": "Relationships",
                            "queued_depth": 0,
                            "deferred_depth": 0,
                            "worker_queue_depth": 0,
                            "recent_failed": 0,
                            "recent_avg_latency_seconds": "3.5",
                            "telemetry_error": None,
                        }
                    ]
                },
                "dogfood_gate": {"passed": True, "failures": []},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    nist_benchmark.nist_cleanup_plan_path(run_id).write_text("{}\n", encoding="utf-8")
    plan = nist_benchmark.build_relationship_matrix_plan(
        matrix_id="20260501-rm",
        target_counts=[500],
        relationship_policies=["deferred"],
        chunks_per_document=10,
        chunk_chars=3600,
        overlap_chars=250,
        source_document_candidates=80,
        enable_ai_enrichment=False,
        eval_limit=10,
        min_expected_hit_ratio=0.5,
        relationship_queue_timeout_seconds=5400,
    )

    report = nist_benchmark.summarize_relationship_matrix(plan)

    assert report["completed_cell_count"] == 1
    assert report["all_completed_cells_passed"] is True
    assert report["cells"][0]["accept_latency"] == {
        "count": 2,
        "min_ms": 25,
        "p50_ms": 75,
        "p95_ms": 75,
        "max_ms": 75,
    }
    assert report["cells"][0]["relationship_queue_drained"] is True
    assert report["cells"][0]["relationship_recent_avg_latency_seconds"] == 3.5
    assert report["cells"][0]["cleanup_plan_exists"] is True
    assert report["cells"][0]["graph_relationships"]["edges_touching_tagged_nodes"] == 640
    assert report["cells"][0]["authority_eval_pack_count"] == 1
    assert report["cells"][0]["authority_eval"]["case_ids"] == ["rmf-steps-governing-source"]
    assert report["cells"][0]["authority_eval"]["governing_source_coverage"]["covered"] == 1
    assert report["cells"][0]["authority_eval_ready_for_answer_support_validation"] is False
    assert report["cells"][0]["authority_support_validation_ready"] is True
    assert report["cells"][0]["authority_support_validation_passed_cases"] == 1


def test_cmd_matrix_dry_run_writes_report_without_api_calls(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(nist_benchmark, "RUN_DIR", tmp_path)

    rc = nist_benchmark.cmd_matrix(
        argparse.Namespace(
            matrix_id="20260501-rm",
            target_counts=[500],
            relationship_policies=["deferred", "immediate"],
            chunks_per_document=10,
            chunk_chars=3600,
            overlap_chars=250,
            source_document_candidates=80,
            enable_ai_enrichment=False,
            eval_limit=10,
            min_expected_hit_ratio=0.5,
            relationship_queue_timeout_seconds=5400,
            dry_run=True,
            report_only=False,
        )
    )

    output = capsys.readouterr().out
    report_path = tmp_path / "20260501-rm-nist-relationship-matrix.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert rc == 0
    assert "20260501-rm-nist-relationship-matrix.json" in output
    assert report["completed_cell_count"] == 0
    assert [cell["run_id"] for cell in report["cells"]] == [
        "20260501-rm-500-d",
        "20260501-rm-500-i",
    ]


def test_cmd_matrix_report_only_writes_report_without_api_calls(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(nist_benchmark, "RUN_DIR", tmp_path)

    def fail_client_from_args(*_args, **_kwargs):
        raise AssertionError("matrix --report-only must not construct an API client")

    monkeypatch.setattr(nist_benchmark, "client_from_args", fail_client_from_args)

    rc = nist_benchmark.cmd_matrix(
        argparse.Namespace(
            matrix_id="20260501-rm",
            target_counts=[500],
            relationship_policies=["deferred"],
            chunks_per_document=10,
            chunk_chars=3600,
            overlap_chars=250,
            source_document_candidates=80,
            enable_ai_enrichment=False,
            eval_limit=10,
            min_expected_hit_ratio=0.5,
            relationship_queue_timeout_seconds=5400,
            dry_run=False,
            report_only=True,
        )
    )

    output = capsys.readouterr().out
    report_path = tmp_path / "20260501-rm-nist-relationship-matrix.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert rc == 0
    assert "20260501-rm-nist-relationship-matrix.json" in output
    assert report["completed_cell_count"] == 0
    assert report["cells"][0]["authority_eval_pack_count"] == 0


def test_compare_nist_run_artifacts_summarizes_reports_and_cleanup(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(nist_benchmark, "RUN_DIR", tmp_path)
    run_id = "20260501-cmp250"
    nist_benchmark.nist_artifact_path(run_id).write_text(
        "\n".join(
            [
                json.dumps({"index": 0, "accept_latency_ms": 10}),
                json.dumps({"index": 1, "accept_latency_ms": 30}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    nist_benchmark.nist_manifest_path(run_id).write_text(
        json.dumps(
            {
                "actual_count": 250,
                "publications": _authority_publications_for_cases(),
                "authority_eval_packs": nist_benchmark.build_authority_eval_packs(
                    {
                        "run_id": run_id,
                        "run_tag": f"benchmark-run-{run_id}",
                        "relationship_policy": "deferred",
                        "publications": _authority_publications_for_cases(),
                    }
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    nist_benchmark.nist_report_path(run_id).write_text(
        json.dumps(
            {
                "expected_count": 250,
                "tagged_items": 250,
                "ready_tagged_items": 250,
                "deployment": {"chart": "0.1.182", "appVersion": "d56de457"},
                "graph_relationships": {
                    "edges_touching_tagged_nodes": 312,
                    "orphaned_ready_items": 0,
                },
                "worker_backpressure": {
                    "queues": [
                        {
                            "key": "relationships",
                            "queued_depth": 0,
                            "deferred_depth": 0,
                            "worker_queue_depth": 0,
                            "recent_failed": 0,
                            "recent_avg_latency_seconds": "4.25",
                        }
                    ]
                },
                "search_expected_hit_ratio": 1.0,
                "retrieve_expected_hit_ratio": 0.8,
                "contrastive_eval_pack_count": 3,
                "authority_support_validation": {
                    "advisory": True,
                    "cases": [
                        {
                            "id": "rmf-steps-governing-source",
                            "status": "pass",
                            "support_metadata_ready": True,
                            "governing_source_present": True,
                            "confusing_source_only": False,
                            "weak_support_state": False,
                            "missing_provenance_fields": [],
                        },
                        {
                            "id": "enterprise-risk-context-source",
                            "status": "confusing_source_only",
                            "support_metadata_ready": True,
                            "governing_source_present": False,
                            "confusing_source_only": True,
                            "weak_support_state": True,
                            "missing_provenance_fields": [],
                        },
                    ],
                },
                "dogfood_gate": {
                    "passed": True,
                    "failures": [],
                    "room_artifacts": {"blocked_rooms": 0},
                    "wakeup_briefs": {"stale": 0},
                },
                "evals": [
                    {
                        "id": "risk-management-framework",
                        "retrieve_expected_rank": 1,
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    nist_benchmark.nist_cleanup_plan_path(run_id).write_text(
        json.dumps(
            {
                "count": 250,
                "unsafe_item_ids": [],
                "delete_confirmation": "BENCHMARK-RUN-20260501-cmp250",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    comparison = nist_benchmark.compare_nist_run_artifacts([run_id])

    assert comparison["warning_count"] == 0
    summary = comparison["runs"][0]
    assert summary["expected_count"] == 250
    assert summary["artifact_entry_count"] == 2
    assert summary["ready_tagged_items"] == 250
    assert summary["accept_latency"]["p50_ms"] == 30
    assert summary["relationship_queue_drained"] is True
    assert summary["relationship_recent_avg_latency_seconds"] == 4.25
    assert summary["edges_touching_tagged_nodes"] == 312
    assert summary["cleanup_plan_count"] == 250
    assert summary["deployment"] == {"chart": "0.1.182", "appVersion": "d56de457"}
    assert summary["authority_eval_pack_count"] == 12
    assert summary["authority_eval_ready_for_answer_support_validation"] is True
    assert summary["authority_eval_case_ids"][0] == "rmf-steps-governing-source"
    assert summary["authority_support_validation_case_count"] == 2
    assert summary["authority_support_validation_passed_cases"] == 1
    assert summary["authority_support_validation_weak_support_cases"] == 1
    assert summary["authority_support_validation_confusing_source_only_cases"] == 1
    assert summary["authority_report"]["governing_source"]["top_rank_cases"] == 0
    assert summary["authority_report"]["adjacent_source_demotion"]["confusing_source_only_cases"] == 1
    assert summary["authority_report"]["provenance"]["missing_provenance_cases"] == 0


def _write_durable_matrix_artifacts(
    run_id: str,
    *,
    run_dir: Path,
    dogfood_passed: bool,
    authority_ready: bool,
    top_rank: int = 1,
    queue_depth: int = 0,
    wakeup_stale: int = 0,
    blocked_rooms: int = 0,
    search_ratio: float = 1.0,
    retrieve_ratio: float = 1.0,
) -> None:
    nist_benchmark.nist_artifact_path(run_id).write_text(
        json.dumps({"index": 0, "accept_latency_ms": 10}) + "\n",
        encoding="utf-8",
    )
    nist_benchmark.nist_manifest_path(run_id).write_text(
        json.dumps(
            {
                "actual_count": 1,
                "publications": _authority_publications_for_cases(),
                "authority_eval_packs": nist_benchmark.build_authority_eval_packs(
                    {
                        "run_id": run_id,
                        "run_tag": f"benchmark-run-{run_id}",
                        "relationship_policy": "deferred",
                        "publications": _authority_publications_for_cases(),
                    }
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    nist_benchmark.nist_report_path(run_id).write_text(
        json.dumps(
            {
                "expected_count": 1,
                "ready_tagged_items": 1,
                "search_expected_hit_ratio": search_ratio,
                "retrieve_expected_hit_ratio": retrieve_ratio,
                "worker_backpressure": {
                    "queues": [
                        {
                            "key": "relationships",
                            "label": "Relationships",
                            "queued_depth": queue_depth,
                            "deferred_depth": 0,
                            "worker_queue_depth": 0,
                            "recent_failed": 0,
                        }
                    ]
                },
                "authority_support_validation": {
                    "advisory": True,
                    "cases": [
                        {
                            "id": "rmf-steps-governing-source",
                            "status": "pass" if authority_ready else "confusing_source_only",
                            "support_metadata_ready": True,
                            "governing_source_present": authority_ready,
                            "governing_source_rank": 1 if authority_ready else None,
                            "decoy_publications_present": [] if authority_ready else ["80039"],
                            "confusing_source_only": not authority_ready,
                            "weak_support_state": not authority_ready,
                            "missing_provenance_fields": [],
                        }
                    ],
                },
                "dogfood_gate": {
                    "passed": dogfood_passed,
                    "failures": [] if dogfood_passed else ["durable memory health failed"],
                    "room_artifacts": {"blocked_rooms": blocked_rooms},
                    "wakeup_briefs": {"stale": wakeup_stale},
                },
                "evals": [
                    {
                        "id": "risk-management-framework",
                        "retrieve_expected_rank": top_rank,
                    },
                    {
                        "id": "cloud-definition",
                        "retrieve_expected_rank": 1,
                    },
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    nist_benchmark.nist_cleanup_plan_path(run_id).write_text(
        json.dumps({"count": 1, "unsafe_item_ids": []}) + "\n",
        encoding="utf-8",
    )
    assert run_dir.exists()


def test_durable_memory_matrix_treats_replay_as_blocking_and_authority_as_advisory(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(nist_benchmark, "RUN_DIR", tmp_path)
    run_id = "20260507-dm-ok"
    _write_durable_matrix_artifacts(
        run_id,
        run_dir=tmp_path,
        dogfood_passed=True,
        authority_ready=False,
    )

    report = nist_benchmark.build_durable_memory_matrix_report(
        matrix_id="20260507-dm",
        run_ids=[run_id],
        min_expected_hit_ratio=0.5,
        retrieval_replay_report={"summary": {"matched_records": 2, "failure_counts": {"top1_changed": 1}}},
        database_health_report={"mode": "static", "ok": True, "checks": []},
        database_health_source="test-db-health.json",
    )

    assert report["blocking_passed"] is False
    assert report["advisory_ready"] is False
    assert report["blocking_failure_count"] == 1
    assert {failure["gate"]["name"] for failure in report["blocking_failures"]} == {
        "retrieval replay stability",
    }
    assert {failure["gate"]["name"] for failure in report["advisory_failures"]} == {"authority support advisory"}
    assert all(failure["gate"]["name"] != "authority support advisory" for failure in report["blocking_failures"])


def test_durable_memory_matrix_reports_blocking_gate_failures(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(nist_benchmark, "RUN_DIR", tmp_path)
    run_id = "20260507-dm-bad"
    _write_durable_matrix_artifacts(
        run_id,
        run_dir=tmp_path,
        dogfood_passed=False,
        authority_ready=True,
        top_rank=2,
        queue_depth=3,
        wakeup_stale=1,
        blocked_rooms=2,
        retrieve_ratio=0.25,
    )

    report = nist_benchmark.build_durable_memory_matrix_report(
        matrix_id="20260507-dm",
        run_ids=[run_id],
        min_expected_hit_ratio=0.5,
        retrieval_replay_report={"summary": {"matched_records": 2, "failure_counts": {}}},
        database_health_report={
            "mode": "static",
            "ok": False,
            "checks": [{"name": "alembic migration chain", "status": "fail", "detail": "bad"}],
        },
        database_health_source="test-db-health.json",
    )

    failed_gate_names = {failure["gate"]["name"] for failure in report["blocking_failures"]}
    assert report["blocking_passed"] is False
    assert "memory retrieve fixed-truth expected-hit ratio" in failed_gate_names
    assert "retained NIST top-rank requirements" in failed_gate_names
    assert "relationship queue drain" in failed_gate_names
    assert "room artifacts" in failed_gate_names
    assert "wake-up freshness" in failed_gate_names
    assert "dogfood gate" in failed_gate_names
    assert "database health" in failed_gate_names
    assert report["advisory_ready"] is True


def test_cmd_durable_matrix_writes_report_without_api_calls(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(nist_benchmark, "RUN_DIR", tmp_path)
    run_id = "20260507-dm-cli"
    db_health_path = tmp_path / "db-health.json"
    db_health_path.write_text(
        json.dumps({"mode": "static", "ok": True, "checks": []}) + "\n",
        encoding="utf-8",
    )
    _write_durable_matrix_artifacts(
        run_id,
        run_dir=tmp_path,
        dogfood_passed=True,
        authority_ready=True,
    )

    def fail_client_from_args(*_args, **_kwargs):
        raise AssertionError("durable-matrix must not construct an API client")

    monkeypatch.setattr(nist_benchmark, "client_from_args", fail_client_from_args)

    rc = nist_benchmark.cmd_durable_matrix(
        argparse.Namespace(
            matrix_id="20260507-dm",
            run_ids=[run_id],
            min_expected_hit_ratio=0.5,
            retrieval_replay_report=None,
            database_health_report=str(db_health_path),
            skip_static_database_health=False,
            format="json",
            output=None,
        )
    )

    output = capsys.readouterr().out
    report_path = nist_benchmark.nist_durable_matrix_report_path("20260507-dm")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert rc == 1
    assert "20260507-dm-nist-durable-memory-matrix.json" in output
    assert report["advisory_ready"] is True
    assert report["blocking_passed"] is False
    assert report["blocking_failures"][0]["gate"]["name"] == "retrieval replay stability"


def test_compare_nist_run_artifacts_reports_missing_artifacts_without_api_calls(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(nist_benchmark, "RUN_DIR", tmp_path)

    def fail_client_from_args(*_args, **_kwargs):
        raise AssertionError("compare must not construct an API client")

    monkeypatch.setattr(nist_benchmark, "client_from_args", fail_client_from_args)

    comparison = nist_benchmark.compare_nist_run_artifacts(["20260501-missing"])
    markdown = nist_benchmark.format_nist_artifact_comparison_markdown(comparison)

    assert comparison["warning_count"] == 4
    assert comparison["runs"][0]["artifact_entry_count"] == 0
    assert comparison["runs"][0]["cleanup_plan_exists"] is False
    assert "| 20260501-missing |" in markdown
    assert "## Warnings" in markdown
    assert "missing artifact:" in markdown


def test_cmd_compare_writes_stable_markdown(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(nist_benchmark, "RUN_DIR", tmp_path)
    run_id = "20260501-cmp500"
    output_path = tmp_path / "comparison.md"
    nist_benchmark.nist_artifact_path(run_id).write_text(
        json.dumps({"index": 0, "accept_latency_ms": 12}) + "\n",
        encoding="utf-8",
    )
    nist_benchmark.nist_manifest_path(run_id).write_text(
        json.dumps({"actual_count": 500}) + "\n",
        encoding="utf-8",
    )
    nist_benchmark.nist_report_path(run_id).write_text(
        json.dumps(
            {
                "ready_tagged_items": 499,
                "dogfood_gate": {
                    "passed": False,
                    "failures": ["Wake-up briefs are stale: stale=1"],
                    "room_artifacts": {"blocked_rooms": 2},
                    "wakeup_briefs": {"stale": 1},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    nist_benchmark.nist_cleanup_plan_path(run_id).write_text(
        json.dumps({"count": 500, "unsafe_item_ids": ["unsafe-item"]}) + "\n",
        encoding="utf-8",
    )

    rc = nist_benchmark.cmd_compare(
        argparse.Namespace(
            run_ids=[run_id],
            format="markdown",
            output=str(output_path),
        )
    )

    output = capsys.readouterr().out
    markdown = output_path.read_text(encoding="utf-8")
    assert rc == 0
    assert f"wrote {output_path}" in output
    assert "| run_id | expected | accepted | ready | rel_queue | rel_edges | rel_latency_s | search_hit | retrieve_hit | auth_packs | auth_ready | auth_support | auth_weak | auth_top1 | auth_demoted | auth_provenance_missing |" in markdown
    assert "| 20260501-cmp500 | 500 | 1 | 499 |" in markdown
    assert "| - | - | 0 | no | 0/0 | 0 | 0 | 0 | 0 | 0 | 1 | 2 |" in markdown
    assert "500 items, 1 unsafe" in markdown
