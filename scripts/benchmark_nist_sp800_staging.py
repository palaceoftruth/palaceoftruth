#!/usr/bin/env python3
"""Realistic NIST SP 800 corpus benchmark for Palace of Truth staging.

The helper downloads public NIST SP 800 PDFs, extracts text with pdftotext,
creates deterministic benchmark memory entries, waits for durable memory jobs,
verifies search/retrieval, and writes a human-reviewed cleanup plan.

Cleanup is never automatic. Deletion requires an explicit confirmation string.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from benchmark_secondbrain_staging import (
    DEFAULT_API_BASE_URL,
    DEFAULT_FRONTEND_URL,
    RUN_DIR,
    ApiError,
    Client,
    append_jsonl,
    build_dogfood_gate_report,
    client_from_args,
    acquire_run_artifact_lock,
    delete_benchmark_items,
    list_tagged_items,
    read_jsonl,
    resolve_tenant_id,
    run_tag,
    utc_now,
    validate_run_id as validate_base_run_id,
    wait_for_palace_fresh,
)


NIST_TREE_API_URL = "https://api.github.com/repos/usnistgov/NIST-Tech-Pubs/git/trees/nist-pages?recursive=1"
NIST_RAW_BASE_URL = "https://raw.githubusercontent.com/usnistgov/NIST-Tech-Pubs/nist-pages"
NIST_TECH_PUBS_PAGE_URL = "https://pages.nist.gov/NIST-Tech-Pubs/"
NIST_TECH_PUBS_README_URL = "https://raw.githubusercontent.com/usnistgov/NIST-Tech-Pubs/nist-pages/xml/readme.md"
HF_NIST_DATASET_ID = "ethanolivertroy/nist-cybersecurity-training"
HF_NIST_DATASET_URL = f"https://huggingface.co/datasets/{HF_NIST_DATASET_ID}"
HF_NIST_DATASET_VERSION = "1.1"
HF_NIST_LICENSE = "cc0-1.0"
HF_NIST_EXPECTED_TOTAL_ROWS = 530_912
HF_NIST_EXPECTED_SPLIT_ROWS = {"train": 424_729, "validation": 106_183}
HF_NIST_EXPECTED_DOCUMENTS = 596
HF_NIST_EMBEDDING_MODEL = "text-embedding-3-small"
HF_NIST_EMBEDDING_DIMENSION = 1536
USER_AGENT = "palaceoftruth-nist-sp800-benchmark/1.0"
RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}
PUBLICATION_ID_RE = re.compile(r"\b800(?:-[0-9A-Za-z]+|[A-Za-z][0-9A-Za-z]*)+\b")

DEFAULT_RUN_ID_PREFIX = "nist-sp800"
NIST_IDEMPOTENCY_KEY_PREFIX = "palaceoftruth-nist-sp800-benchmark"
MAX_IDEMPOTENCY_KEY_LENGTH = 64
NIST_IDEMPOTENCY_KEY_INDEX_WIDTH = 4
MAX_NIST_RUN_ID_LENGTH = (
    MAX_IDEMPOTENCY_KEY_LENGTH
    - len(NIST_IDEMPOTENCY_KEY_PREFIX)
    - len(":")
    - len(":")
    - NIST_IDEMPOTENCY_KEY_INDEX_WIDTH
)
RELATIONSHIP_QUEUE_KEYS = {"relationships", "relationship"}
RELATIONSHIP_QUEUE_DRAIN_FIELDS = (
    "queued_depth",
    "deferred_depth",
    "worker_queue_depth",
    "recent_failed",
)

PREFERRED_PUBLICATION_IDS = (
    "800-207",
    "800-53r5",
    "800-53Ar5",
    "800-53B",
    "800-37r2",
    "800-30r1",
    "800-39",
    "800-61r2",
    "800-63-3",
    "800-63b",
    "800-63c",
    "800-82r3",
    "800-88r1",
    "800-92",
    "800-100",
    "800-115",
    "800-122",
    "800-128",
    "800-137",
    "800-144",
    "800-145",
    "800-160v1r1",
    "800-160v2r1",
    "800-161r1",
    "800-218",
    "800-190",
    "800-204",
    "800-204A",
    "800-204B",
    "800-172",
    "800-181r1",
    "800-184",
    "800-185",
    "800-193",
    "800-209",
    "800-210",
    "800-213",
    "800-213A",
    "800-215",
    "800-216",
    "800-219r1",
    "800-220",
    "800-223",
    "800-225",
)

TOPIC_RULES = (
    ("zero-trust", ("zero trust", "policy engine", "policy administrator")),
    ("controls", ("control", "baseline", "assessment")),
    ("risk-management", ("risk", "risk management", "rmf")),
    ("incident-response", ("incident", "handling", "containment")),
    ("identity", ("identity", "authenticator", "federation")),
    ("industrial-control", ("industrial control", "operational technology", "ics")),
    ("media-sanitization", ("sanitization", "media", "purge")),
    ("logging", ("log", "event", "security monitoring")),
    ("cloud", ("cloud", "saas", "iaas", "paas")),
    ("systems-security", ("systems security", "engineering", "resilience")),
    ("supply-chain", ("supply chain", "scrm")),
    ("software-security", ("software", "development", "ssdf")),
    ("privacy", ("privacy", "personally identifiable", "pii")),
    ("configuration", ("configuration", "change control")),
)

EVAL_QUERIES = (
    {
        "id": "zero-trust",
        "query": "zero trust architecture policy engine policy administrator trust algorithm",
        "expected_publications": ["800-207"],
    },
    {
        "id": "security-controls",
        "query": "security and privacy controls control baselines assessment procedures",
        "expected_publications": ["800-53r5", "800-53Ar5", "800-53B"],
    },
    {
        "id": "risk-management-framework",
        "query": "risk management framework categorize select implement assess authorize monitor",
        "expected_publications": ["800-37r2"],
    },
    {
        "id": "risk-assessment",
        "query": "conducting risk assessments threat vulnerability likelihood impact",
        "expected_publications": ["800-30r1"],
    },
    {
        "id": "enterprise-risk",
        "query": "managing information security risk organization mission business process",
        "expected_publications": ["800-39"],
    },
    {
        "id": "incident-handling",
        "query": "computer security incident handling preparation detection analysis containment eradication recovery",
        "expected_publications": ["800-61r2"],
    },
    {
        "id": "digital-identity",
        "query": "digital identity guidelines enrollment authentication federation assurance levels",
        "expected_publications": ["800-63-3", "800-63b", "800-63c"],
    },
    {
        "id": "industrial-control-systems",
        "query": "industrial control systems security operational technology scada distributed control",
        "expected_publications": ["800-82r3"],
    },
    {
        "id": "media-sanitization",
        "query": "media sanitization clear purge destroy disposal storage devices",
        "expected_publications": ["800-88r1"],
    },
    {
        "id": "log-management",
        "query": "computer security log management event sources analysis retention",
        "expected_publications": ["800-92"],
    },
    {
        "id": "information-security-handbook",
        "query": "information security handbook managers program planning implementation",
        "expected_publications": ["800-100"],
    },
    {
        "id": "technical-testing",
        "query": "technical guide information security testing assessment penetration vulnerability",
        "expected_publications": ["800-115"],
    },
    {
        "id": "privacy-pii",
        "query": "protecting personally identifiable information confidentiality impact level",
        "expected_publications": ["800-122"],
    },
    {
        "id": "configuration-management",
        "query": "security-focused configuration management information systems baseline change control",
        "expected_publications": ["800-128"],
    },
    {
        "id": "continuous-monitoring",
        "query": "information security continuous monitoring strategy metrics automation",
        "expected_publications": ["800-137"],
    },
    {
        "id": "public-cloud",
        "query": "security and privacy public cloud computing outsourcing risk",
        "expected_publications": ["800-144"],
    },
    {
        "id": "cloud-definition",
        "query": "cloud computing essential characteristics service models deployment models",
        "expected_publications": ["800-145"],
    },
    {
        "id": "systems-security-engineering",
        "query": "systems security engineering trustworthy secure resilient systems",
        "expected_publications": ["800-160v1r1"],
    },
    {
        "id": "cyber-resiliency",
        "query": "developing cyber-resilient systems adversary disruption resilience techniques",
        "expected_publications": ["800-160v2r1"],
    },
    {
        "id": "supply-chain",
        "query": "cybersecurity supply chain risk management practices suppliers acquirers",
        "expected_publications": ["800-161r1"],
    },
    {
        "id": "secure-software",
        "query": "secure software development framework practices tasks implementation examples",
        "expected_publications": ["800-218"],
    },
)
RETAINED_NIST_TOP_RANK_REQUIREMENTS = {
    "risk-management-framework": ["800-37r2"],
    "cloud-definition": ["800-145"],
}
CONTRASTIVE_DECOY_LIMIT = 3
CONTRASTIVE_STOPWORDS = {
    "and",
    "for",
    "from",
    "guidelines",
    "information",
    "introduction",
    "nist",
    "privacy",
    "publication",
    "revision",
    "security",
    "special",
    "systems",
    "the",
    "with",
}
AUTHORITY_EVAL_PLAN_PATH = "external-plans/authority-sensitive-grounding-eval-plan.md"
AUTHORITY_REQUIRED_METADATA_FIELDS = (
    "publication_id",
    "title",
    "year",
    "pdf_url",
    "source_text_sha256",
    "selected_chunks",
)
AUTHORITY_EVAL_CASES = (
    {
        "id": "rmf-steps-governing-source",
        "question_type": "authority hierarchy",
        "expected_governing_source": "800-37r2",
        "confusing_sources_to_demote": ["800-39", "800-82r3"],
        "expected_result": (
            "Answer cites RMF steps from SP 800-37r2 and marks enterprise risk material as contextual."
        ),
    },
    {
        "id": "enterprise-risk-context-source",
        "question_type": "authority hierarchy",
        "expected_governing_source": "800-39",
        "confusing_sources_to_demote": ["800-37r2"],
        "expected_result": (
            "Answer uses SP 800-39 for organization-level risk framing and does not present RMF lifecycle "
            "steps as the main authority."
        ),
    },
    {
        "id": "risk-assessment-process-source",
        "question_type": "adjacent-publication confusion",
        "expected_governing_source": "800-30r1",
        "confusing_sources_to_demote": ["800-37r2", "800-39"],
        "expected_result": (
            "Answer distinguishes risk assessment process from RMF orchestration and risk governance."
        ),
    },
    {
        "id": "control-baseline-source",
        "question_type": "source role distinction",
        "expected_governing_source": "800-53B",
        "confusing_sources_to_demote": ["800-53r5", "800-53Ar5"],
        "expected_result": (
            "Answer cites baseline material and treats control catalog and assessment procedures as different "
            "source roles."
        ),
    },
    {
        "id": "control-assessment-source",
        "question_type": "source role distinction",
        "expected_governing_source": "800-53Ar5",
        "confusing_sources_to_demote": ["800-53r5", "800-53B"],
        "expected_result": (
            "Answer cites assessment procedure material and avoids substituting baseline or catalog text."
        ),
    },
    {
        "id": "cloud-definition-source",
        "question_type": "applicability",
        "expected_governing_source": "800-145",
        "confusing_sources_to_demote": ["800-144"],
        "expected_result": (
            "Answer uses SP 800-145 for cloud characteristics and treats SP 800-144 as risk guidance."
        ),
    },
    {
        "id": "public-cloud-risk-source",
        "question_type": "applicability",
        "expected_governing_source": "800-144",
        "confusing_sources_to_demote": ["800-145"],
        "expected_result": (
            "Answer uses SP 800-144 for public-cloud risk guidance and does not over-cite the cloud definition."
        ),
    },
    {
        "id": "zero-trust-policy-engine-source",
        "question_type": "exact support",
        "expected_governing_source": "800-207",
        "confusing_sources_to_demote": ["800-53r5"],
        "expected_result": (
            "Answer cites zero trust architecture concepts rather than generic access-control material."
        ),
    },
    {
        "id": "systems-engineering-vs-resiliency",
        "question_type": "adjacent-publication confusion",
        "expected_governing_source": "800-160v1r1",
        "confusing_sources_to_demote": ["800-160v2r1"],
        "expected_result": (
            "Answer distinguishes systems security engineering from cyber-resiliency techniques."
        ),
    },
    {
        "id": "cyber-resiliency-techniques-source",
        "question_type": "adjacent-publication confusion",
        "expected_governing_source": "800-160v2r1",
        "confusing_sources_to_demote": ["800-160v1r1"],
        "expected_result": (
            "Answer cites resiliency technique material and avoids treating general systems engineering as "
            "sufficient."
        ),
    },
    {
        "id": "supply-chain-risk-source",
        "question_type": "applicability",
        "expected_governing_source": "800-161r1",
        "confusing_sources_to_demote": ["800-53r5", "800-39"],
        "expected_result": (
            "Answer uses supply-chain risk management material and names broader risk/control sources as "
            "supporting context only."
        ),
    },
    {
        "id": "secure-software-framework-source",
        "question_type": "exact support",
        "expected_governing_source": "800-218",
        "confusing_sources_to_demote": ["800-161r1", "800-53r5"],
        "expected_result": (
            "Answer cites SSDF practices/tasks and avoids replacing them with supply-chain or control catalog text."
        ),
    },
)
AUTHORITY_EVAL_CASES_BY_ID = {case["id"]: case for case in AUTHORITY_EVAL_CASES}


@dataclass(frozen=True)
class Publication:
    publication_id: str
    ris_path: str
    raw_ris_url: str
    title: str
    year: str | None
    doi: str

    @property
    def doi_url(self) -> str:
        return f"https://doi.org/{self.doi}"


@dataclass(frozen=True)
class CorpusChunk:
    index: int
    publication_id: str
    publication_title: str
    publication_year: str | None
    doi: str
    doi_url: str
    pdf_url: str
    source_text_sha256: str
    chunk_index: int
    chunk_offset: int
    chunk_text: str


def default_run_id() -> str:
    return f"{utc_now().strftime('%Y%m%d')}-{DEFAULT_RUN_ID_PREFIX}-250"


def default_matrix_id() -> str:
    return f"{utc_now().strftime('%Y%m%d')}-rm"


def build_nist_idempotency_key(run_id: str, index: int) -> str:
    key = f"{NIST_IDEMPOTENCY_KEY_PREFIX}:{run_id}:{index:04d}"
    if len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ValueError(
            "NIST benchmark idempotency key would exceed "
            f"{MAX_IDEMPOTENCY_KEY_LENGTH} chars: {len(key)}"
        )
    return key


def validate_run_id(run_id: str) -> str:
    run_id = validate_base_run_id(run_id)
    if len(run_id) > MAX_NIST_RUN_ID_LENGTH:
        raise SystemExit(
            "NIST benchmark run id must be at most "
            f"{MAX_NIST_RUN_ID_LENGTH} chars so generated idempotency keys fit "
            f"the {MAX_IDEMPOTENCY_KEY_LENGTH}-char items.idempotency_key column"
        )
    return run_id


def nist_artifact_path(run_id: str) -> Path:
    return RUN_DIR / f"{run_id}-nist-corpus.jsonl"


def nist_manifest_path(run_id: str) -> Path:
    return RUN_DIR / f"{run_id}-nist-corpus-manifest.json"


def nist_report_path(run_id: str) -> Path:
    return RUN_DIR / f"{run_id}-nist-corpus-report.json"


def nist_cleanup_plan_path(run_id: str) -> Path:
    return RUN_DIR / f"{run_id}-nist-corpus-cleanup-plan.json"


def nist_matrix_report_path(matrix_id: str) -> Path:
    return RUN_DIR / f"{matrix_id}-nist-relationship-matrix.json"


def nist_durable_matrix_report_path(matrix_id: str) -> Path:
    return RUN_DIR / f"{matrix_id}-nist-durable-memory-matrix.json"


def nist_hf_manifest_path(run_id: str) -> Path:
    return RUN_DIR / f"{run_id}-nist-hf-corpus-manifest.json"


def nist_cache_dir() -> Path:
    return RUN_DIR / "nist-sp800-cache"


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip()).strip("-").lower()
    return slug or "untitled"


def normalize_publication_id(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def contrastive_terms(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.lower())
        if len(token) >= 3 and token not in CONTRASTIVE_STOPWORDS
    }


def _dedupe_query_words(value: str) -> str:
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9-]*", value)
    return " ".join(dict.fromkeys(words))


def build_contrastive_probe_query(
    eval_query: dict[str, Any],
    target_publications: list[dict[str, Any]],
    decoys: list[dict[str, Any]],
) -> str:
    target_titles = " ".join(str(publication.get("title") or "") for publication in target_publications)
    decoy_titles = " ".join(str(decoy.get("title") or "") for decoy in decoys[:2])
    shared_terms = " ".join(
        sorted({term for decoy in decoys for term in decoy.get("overlap_terms", [])})[:8]
    )
    return _dedupe_query_words(
        f"{target_titles} {eval_query['query']} {shared_terms} contrast with {decoy_titles}"
    )


def build_contrastive_eval_packs(
    publications: list[dict[str, Any]],
    eval_queries: list[dict[str, Any]] | tuple[dict[str, Any], ...] = EVAL_QUERIES,
    *,
    decoy_limit: int = CONTRASTIVE_DECOY_LIMIT,
) -> list[dict[str, Any]]:
    """Derive deterministic advisory retrieval probes from manifest metadata."""
    publications_by_id = {
        normalize_publication_id(publication["publication_id"]): publication
        for publication in publications
    }
    publication_positions = {
        normalize_publication_id(publication["publication_id"]): index
        for index, publication in enumerate(publications)
    }
    packs: list[dict[str, Any]] = []
    for eval_query in eval_queries:
        expected_ids = [
            normalize_publication_id(publication)
            for publication in eval_query["expected_publications"]
            if normalize_publication_id(publication) in publications_by_id
        ]
        if not expected_ids:
            continue

        target_publications = [publications_by_id[publication_id] for publication_id in expected_ids]
        target_terms = contrastive_terms(eval_query["query"])
        for publication in target_publications:
            target_terms.update(contrastive_terms(str(publication.get("title") or "")))
        target_position = min(publication_positions[publication_id] for publication_id in expected_ids)

        scored_decoys: list[tuple[int, int, str, dict[str, Any]]] = []
        for publication in publications:
            publication_id = normalize_publication_id(publication["publication_id"])
            if publication_id in expected_ids:
                continue
            terms = contrastive_terms(str(publication.get("title") or ""))
            overlap_terms = sorted(terms & target_terms)
            distance = abs(publication_positions[publication_id] - target_position)
            scored_decoys.append(
                (
                    len(overlap_terms),
                    distance,
                    publication_id,
                    {
                        "publication_id": publication["publication_id"],
                        "title": publication.get("title"),
                        "overlap_terms": overlap_terms,
                    },
                )
            )

        scored_decoys.sort(key=lambda item: (-item[0], item[1], item[2]))
        decoys = [item[3] for item in scored_decoys[:decoy_limit]]
        if not decoys:
            continue

        probe = {
            "id": f"{eval_query['id']}-contrastive-title",
            "query": build_contrastive_probe_query(eval_query, target_publications, decoys),
            "question_type": "adjacent-publication-confusion",
            "expected_publications": [publication["publication_id"] for publication in target_publications],
            "decoy_publications": [decoy["publication_id"] for decoy in decoys],
        }
        packs.append(
            {
                "id": f"{eval_query['id']}-contrastive",
                "source_eval_id": eval_query["id"],
                "question_types": ["adjacent-publication-confusion"],
                "expected_publications": [publication["publication_id"] for publication in target_publications],
                "neighboring_decoys": decoys,
                "probes": [probe],
            }
        )
    return packs


def _manifest_publications_by_id(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        normalize_publication_id(publication["publication_id"]): publication
        for publication in manifest.get("publications", [])
    }


def validate_authority_publication(publication: dict[str, Any], *, case_id: str) -> None:
    publication_id = publication.get("publication_id", "<missing>")
    missing = [
        field
        for field in AUTHORITY_REQUIRED_METADATA_FIELDS
        if publication.get(field) in (None, "", [])
    ]
    if not (publication.get("doi") or publication.get("doi_url")):
        missing.append("doi_or_doi_url")
    if missing:
        raise SystemExit(
            f"authority eval case {case_id} cannot use publication {publication_id}: "
            f"missing required manifest metadata {', '.join(missing)}"
        )

    for chunk in publication.get("selected_chunks", []):
        if chunk.get("chunk_index") is None or chunk.get("chunk_offset") is None:
            raise SystemExit(
                f"authority eval case {case_id} cannot use publication {publication_id}: "
                "selected chunk is missing chunk_index or chunk_offset"
            )


def authority_support_reference(
    publication: dict[str, Any],
    *,
    run_tag_value: str,
    relationship_policy: str | None,
) -> dict[str, Any]:
    selected_chunk = publication["selected_chunks"][0]
    return {
        "publication_id": publication["publication_id"],
        "publication_title": publication.get("title"),
        "publication_year": publication.get("year"),
        "doi": publication.get("doi"),
        "doi_url": publication.get("doi_url"),
        "pdf_url": publication.get("pdf_url"),
        "source_text_sha256": publication.get("source_text_sha256"),
        "chunk_index": selected_chunk["chunk_index"],
        "chunk_offset": selected_chunk["chunk_offset"],
        "benchmark_run_tag": run_tag_value,
        "relationship_policy": relationship_policy,
    }


def build_authority_eval_packs(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Build local-only authority grounding eval cases from manifest metadata."""
    publications_by_id = _manifest_publications_by_id(manifest)
    run_tag_value = manifest.get("run_tag") or run_tag(str(manifest.get("run_id", "")))
    relationship_policy = manifest.get("relationship_policy")
    packs: list[dict[str, Any]] = []
    for case in AUTHORITY_EVAL_CASES:
        governing_id = normalize_publication_id(case["expected_governing_source"])
        publication = publications_by_id.get(governing_id)
        if publication is None:
            continue
        validate_authority_publication(publication, case_id=case["id"])
        confusing_sources = list(case["confusing_sources_to_demote"])
        available_confusing_sources = [
            source
            for source in confusing_sources
            if normalize_publication_id(source) in publications_by_id
        ]
        packs.append(
            {
                "id": case["id"],
                "source_plan": AUTHORITY_EVAL_PLAN_PATH,
                "advisory": True,
                "question_type": case["question_type"],
                "expected_governing_source": case["expected_governing_source"],
                "expected_publications": [case["expected_governing_source"]],
                "confusing_sources_to_demote": confusing_sources,
                "available_confusing_sources": available_confusing_sources,
                "missing_confusing_sources": [
                    source for source in confusing_sources if source not in available_confusing_sources
                ],
                "expected_result": case["expected_result"],
                "support_contract": {
                    "requires_short_answer_or_abstention": True,
                    "requires_supporting_excerpts": True,
                    "requires_governing_source_class": True,
                    "requires_adjacent_source_reason": True,
                    "requires_weak_support_state": True,
                },
                "expected_support": [
                    authority_support_reference(
                        publication,
                        run_tag_value=run_tag_value,
                        relationship_policy=relationship_policy,
                    )
                ],
            }
        )
    return packs


def _authority_publication_missing_fields(publication: dict[str, Any]) -> list[str]:
    missing = [
        field
        for field in AUTHORITY_REQUIRED_METADATA_FIELDS
        if publication.get(field) in (None, "", [])
    ]
    if not (publication.get("doi") or publication.get("doi_url")):
        missing.append("doi_or_doi_url")
    return missing


def summarize_authority_eval_pack_readiness(
    manifest: dict[str, Any] | None,
    *,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Summarize local-only authority eval metadata without running answer validation."""
    emitted_warnings: list[str] = []

    def warn(message: str) -> None:
        emitted_warnings.append(message)
        if warnings is not None:
            warnings.append(message)

    if manifest is None:
        return {
            "pack_count": 0,
            "case_ids": [],
            "expected_case_count": len(AUTHORITY_EVAL_CASES),
            "missing_case_ids": [],
            "unknown_case_ids": [],
            "governing_source_coverage": {
                "covered": 0,
                "expected": len(
                    {
                        normalize_publication_id(case["expected_governing_source"])
                        for case in AUTHORITY_EVAL_CASES
                    }
                ),
                "covered_publication_ids": [],
                "missing_publication_ids": [],
            },
            "missing_metadata": [],
            "missing_chunk_case_ids": [],
            "malformed_pack_ids": [],
            "ready_for_answer_support_validation": False,
            "warnings": emitted_warnings,
        }

    publications_by_id: dict[str, dict[str, Any]] = {}
    for publication in manifest.get("publications", []):
        if not isinstance(publication, dict):
            warn("authority eval publication entry is malformed: expected object")
            continue
        publication_id = publication.get("publication_id")
        if not publication_id:
            warn("authority eval publication entry is missing publication_id")
            continue
        publications_by_id[normalize_publication_id(str(publication_id))] = publication

    expected_case_ids = [case["id"] for case in AUTHORITY_EVAL_CASES]
    expected_governing_sources = {
        case["id"]: normalize_publication_id(case["expected_governing_source"])
        for case in AUTHORITY_EVAL_CASES
    }
    missing_publication_ids = sorted(
        {
            case["expected_governing_source"]
            for case in AUTHORITY_EVAL_CASES
            if expected_governing_sources[case["id"]] not in publications_by_id
        }
    )

    missing_metadata: list[dict[str, Any]] = []
    missing_chunk_case_ids: list[str] = []
    for case in AUTHORITY_EVAL_CASES:
        publication = publications_by_id.get(expected_governing_sources[case["id"]])
        if publication is None:
            continue
        missing_fields = _authority_publication_missing_fields(publication)
        if missing_fields:
            missing_metadata.append(
                {
                    "case_id": case["id"],
                    "publication_id": case["expected_governing_source"],
                    "fields": missing_fields,
                }
            )
        selected_chunks = publication.get("selected_chunks")
        if not isinstance(selected_chunks, list) or not selected_chunks:
            missing_chunk_case_ids.append(case["id"])
            continue
        if any(
            not isinstance(chunk, dict)
            or chunk.get("chunk_index") is None
            or chunk.get("chunk_offset") is None
            for chunk in selected_chunks
        ):
            missing_chunk_case_ids.append(case["id"])

    packs_value = (manifest or {}).get("authority_eval_packs")
    if packs_value is None:
        packs: list[dict[str, Any]] = []
        warn("authority eval packs are missing from manifest")
    elif not isinstance(packs_value, list):
        packs = []
        warn("authority eval packs are malformed: expected list")
    else:
        packs = []
        for index, pack in enumerate(packs_value):
            if not isinstance(pack, dict):
                warn(f"authority eval pack at index {index} is malformed: expected object")
                continue
            packs.append(pack)

    malformed_pack_ids: list[str] = []
    case_ids: list[str] = []
    pack_governing_sources: set[str] = set()
    for index, pack in enumerate(packs):
        case_id = pack.get("id")
        if not isinstance(case_id, str) or not case_id:
            malformed_pack_ids.append(f"<index:{index}>")
            warn(f"authority eval pack at index {index} is missing id")
            continue
        case_ids.append(case_id)
        expected_source = pack.get("expected_governing_source")
        if isinstance(expected_source, str) and expected_source:
            pack_governing_sources.add(normalize_publication_id(expected_source))
        expected_support = pack.get("expected_support")
        if not isinstance(expected_support, list) or not expected_support:
            malformed_pack_ids.append(case_id)
            warn(f"authority eval pack {case_id} is missing expected_support")
            continue
        for support_index, support in enumerate(expected_support):
            if not isinstance(support, dict):
                malformed_pack_ids.append(case_id)
                warn(f"authority eval pack {case_id} support {support_index} is malformed")
                continue
            missing_support_fields = [
                field
                for field in (
                    "publication_id",
                    "publication_title",
                    "publication_year",
                    "chunk_index",
                    "chunk_offset",
                    "benchmark_run_tag",
                )
                if support.get(field) in (None, "", [])
            ]
            if missing_support_fields:
                malformed_pack_ids.append(case_id)
                warn(
                    f"authority eval pack {case_id} support {support_index} is missing "
                    f"{', '.join(missing_support_fields)}"
                )

    missing_case_ids = [case_id for case_id in expected_case_ids if case_id not in set(case_ids)]
    unknown_case_ids = [case_id for case_id in case_ids if case_id not in AUTHORITY_EVAL_CASES_BY_ID]
    if missing_publication_ids:
        warn(
            "authority eval governing publications missing from manifest: "
            + ", ".join(missing_publication_ids)
        )
    for item in missing_metadata:
        warn(
            f"authority eval case {item['case_id']} publication {item['publication_id']} "
            f"is missing metadata: {', '.join(item['fields'])}"
        )
    if missing_chunk_case_ids:
        warn(
            "authority eval cases missing selected chunk locations: "
            + ", ".join(sorted(set(missing_chunk_case_ids)))
        )
    if missing_case_ids:
        warn("authority eval packs missing cases: " + ", ".join(missing_case_ids))
    if unknown_case_ids:
        warn("authority eval packs include unknown cases: " + ", ".join(unknown_case_ids))

    covered_governing_sources = sorted(
        source
        for source in set(expected_governing_sources.values())
        if source in publications_by_id and source in pack_governing_sources
    )
    ready = (
        not missing_publication_ids
        and not missing_metadata
        and not missing_chunk_case_ids
        and not missing_case_ids
        and not unknown_case_ids
        and not malformed_pack_ids
        and len(case_ids) == len(expected_case_ids)
    )
    return {
        "pack_count": len(packs),
        "case_ids": case_ids,
        "expected_case_count": len(expected_case_ids),
        "missing_case_ids": missing_case_ids,
        "unknown_case_ids": unknown_case_ids,
        "governing_source_coverage": {
            "covered": len(covered_governing_sources),
            "expected": len(set(expected_governing_sources.values())),
            "covered_publication_ids": covered_governing_sources,
            "missing_publication_ids": missing_publication_ids,
        },
        "missing_metadata": missing_metadata,
        "missing_chunk_case_ids": sorted(set(missing_chunk_case_ids)),
        "malformed_pack_ids": sorted(set(malformed_pack_ids)),
        "ready_for_answer_support_validation": ready,
        "warnings": emitted_warnings,
    }


def manifest_authority_eval_packs(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    packs = manifest.get("authority_eval_packs")
    if not isinstance(packs, list):
        return []
    return [pack for pack in packs if isinstance(pack, dict)]


def authority_support_query(pack: dict[str, Any]) -> str:
    query = pack.get("query")
    if isinstance(query, str) and query.strip():
        return query.strip()
    expected_result = pack.get("expected_result")
    if isinstance(expected_result, str) and expected_result.strip():
        return expected_result.strip()
    case = AUTHORITY_EVAL_CASES_BY_ID.get(str(pack.get("id") or ""))
    if case is not None:
        return str(case["expected_result"])
    return str(pack.get("id") or "authority support validation")


def support_reference_ready(support: dict[str, Any]) -> tuple[bool, list[str]]:
    required_fields = (
        "publication_id",
        "publication_title",
        "publication_year",
        "chunk_index",
        "chunk_offset",
        "benchmark_run_tag",
    )
    missing = [
        field
        for field in required_fields
        if support.get(field) in (None, "", [])
    ]
    if not (support.get("doi") or support.get("doi_url")):
        missing.append("doi_or_doi_url")
    return not missing, missing


def summarize_authority_support_validation(
    case_reports: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    reports = case_reports or []
    case_count = len(reports)
    ready_cases = sum(1 for report in reports if report.get("support_metadata_ready"))
    governing_hits = sum(1 for report in reports if report.get("governing_source_present"))
    confusing_only = sum(1 for report in reports if report.get("confusing_source_only"))
    missing_provenance = sum(1 for report in reports if report.get("missing_provenance_fields"))
    weak_support = sum(1 for report in reports if report.get("weak_support_state"))
    passed = sum(1 for report in reports if report.get("status") == "pass")
    return {
        "case_count": case_count,
        "passed_cases": passed,
        "support_metadata_ready_cases": ready_cases,
        "governing_source_hit_cases": governing_hits,
        "confusing_source_only_cases": confusing_only,
        "missing_provenance_cases": missing_provenance,
        "weak_support_cases": weak_support,
        "ready": bool(case_count) and passed == case_count,
    }


def authority_support_validation_from_report(report: dict[str, Any] | None) -> dict[str, Any]:
    validation = (report or {}).get("authority_support_validation")
    if isinstance(validation, dict):
        cases = validation.get("cases")
        if isinstance(cases, list):
            summary = summarize_authority_support_validation(
                [case for case in cases if isinstance(case, dict)]
            )
            return {**summary, **validation}
        return {**summarize_authority_support_validation([]), **validation}
    return {
        "advisory": True,
        "cases": [],
        **summarize_authority_support_validation([]),
    }


def summarize_authority_report(
    authority_eval: dict[str, Any] | None,
    authority_support: dict[str, Any] | None,
) -> dict[str, Any]:
    """Promote authority validation details into operator-facing report semantics."""
    eval_summary = authority_eval or {}
    support_summary = authority_support or authority_support_validation_from_report(None)
    cases = [
        case
        for case in support_summary.get("cases", [])
        if isinstance(case, dict)
    ]
    statuses: dict[str, int] = {}
    per_case: list[dict[str, Any]] = []
    top_rank_cases = 0
    adjacent_sources_seen = 0
    adjacent_sources_demoted = 0
    for case in cases:
        status = str(case.get("status") or "unknown")
        statuses[status] = statuses.get(status, 0) + 1
        governing_rank = case.get("governing_source_rank")
        decoys = case.get("decoy_publications_present")
        decoy_publications_present = decoys if isinstance(decoys, list) else []
        if governing_rank == 1:
            top_rank_cases += 1
        if decoy_publications_present:
            adjacent_sources_seen += 1
            if case.get("governing_source_present"):
                adjacent_sources_demoted += 1
        per_case.append(
            {
                "id": case.get("id"),
                "status": status,
                "expected_governing_source": case.get("expected_governing_source"),
                "governing_source_present": bool(case.get("governing_source_present")),
                "governing_source_rank": governing_rank,
                "adjacent_sources_present": decoy_publications_present,
                "adjacent_source_demoted": bool(
                    decoy_publications_present and case.get("governing_source_present")
                ),
                "confusing_source_only": bool(case.get("confusing_source_only")),
                "weak_support_state": bool(case.get("weak_support_state")),
                "support_metadata_ready": bool(case.get("support_metadata_ready")),
                "missing_provenance_fields": case.get("missing_provenance_fields") or [],
            }
        )

    case_count = int(support_summary.get("case_count") or len(cases))
    missing_provenance_cases = int(support_summary.get("missing_provenance_cases") or 0)
    weak_support_cases = int(support_summary.get("weak_support_cases") or 0)
    confusing_source_only_cases = int(support_summary.get("confusing_source_only_cases") or 0)
    return {
        "advisory": True,
        "source_plan": AUTHORITY_EVAL_PLAN_PATH,
        "ready": bool(support_summary.get("ready")),
        "case_count": case_count,
        "status_counts": statuses,
        "governing_source": {
            "ready_for_answer_support_validation": bool(
                eval_summary.get("ready_for_answer_support_validation")
            ),
            "pack_count": eval_summary.get("pack_count", 0),
            "case_ids": eval_summary.get("case_ids", []),
            "hit_cases": int(support_summary.get("governing_source_hit_cases") or 0),
            "top_rank_cases": top_rank_cases,
        },
        "adjacent_source_demotion": {
            "cases_with_adjacent_sources_seen": adjacent_sources_seen,
            "demoted_cases": adjacent_sources_demoted,
            "confusing_source_only_cases": confusing_source_only_cases,
        },
        "weak_support": {
            "cases": weak_support_cases,
            "weak_support_case_ids": [
                str(case.get("id"))
                for case in cases
                if case.get("weak_support_state")
            ],
        },
        "provenance": {
            "support_metadata_ready_cases": int(
                support_summary.get("support_metadata_ready_cases") or 0
            ),
            "missing_provenance_cases": missing_provenance_cases,
            "missing_provenance_case_ids": [
                str(case.get("id"))
                for case in cases
                if case.get("missing_provenance_fields")
            ],
        },
        "cases": per_case,
    }


def validate_authority_support_case(
    pack: dict[str, Any],
    retrieval_results: list[dict[str, Any]],
) -> dict[str, Any]:
    case_id = str(pack.get("id") or "<missing>")
    expected_governing_source = normalize_publication_id(str(pack.get("expected_governing_source") or ""))
    confusing_sources = [
        normalize_publication_id(str(source))
        for source in pack.get("confusing_sources_to_demote", [])
        if source
    ]
    expected_support = [
        support
        for support in pack.get("expected_support", [])
        if isinstance(support, dict)
    ]
    support_readiness = [support_reference_ready(support) for support in expected_support]
    missing_provenance_fields = sorted(
        {
            field
            for ready, missing_fields in support_readiness
            if not ready
            for field in missing_fields
        }
    )
    support_metadata_ready = bool(expected_support) and not missing_provenance_fields
    governing_rank = result_expected_rank(retrieval_results, [expected_governing_source])
    top_publications = result_top_publications(retrieval_results, limit=5)
    flattened_publications = {
        publication
        for publications in top_publications
        for publication in publications
    }
    decoy_publications_present = sorted(
        source for source in confusing_sources if source in flattened_publications
    )
    governing_source_present = governing_rank is not None
    confusing_source_only = bool(decoy_publications_present) and not governing_source_present
    weak_support_state = not governing_source_present or not support_metadata_ready
    status = "pass" if governing_source_present and support_metadata_ready else "weak_support"
    if confusing_source_only:
        status = "confusing_source_only"
    if not expected_support:
        status = "missing_expected_support"
    return {
        "id": case_id,
        "query": authority_support_query(pack),
        "advisory": True,
        "status": status,
        "expected_governing_source": pack.get("expected_governing_source"),
        "confusing_sources_to_demote": pack.get("confusing_sources_to_demote", []),
        "support_metadata_ready": support_metadata_ready,
        "missing_provenance_fields": missing_provenance_fields,
        "governing_source_present": governing_source_present,
        "governing_source_rank": governing_rank,
        "decoy_publications_present": decoy_publications_present,
        "confusing_source_only": confusing_source_only,
        "weak_support_state": weak_support_state,
        "retrieve_total": len(retrieval_results),
        "retrieve_top_titles": [row.get("title") for row in retrieval_results[:5]],
        "retrieve_top_publications": top_publications,
    }


def run_authority_support_validation(
    client: Client,
    run_id: str,
    manifest: dict[str, Any],
    *,
    limit: int,
) -> dict[str, Any]:
    case_reports: list[dict[str, Any]] = []
    for pack in manifest_authority_eval_packs(manifest):
        body = {
            "query": authority_support_query(pack),
            "limit": limit,
            "tags": [run_tag(run_id), "nist-sp800"],
            "tags_mode": "all",
            "scope": {"type": "tenant_shared", "key": None},
        }
        retrieve = client_request(client, "POST", "/api/v1/memory/retrieve", body=body, timeout=90)
        case_reports.append(
            validate_authority_support_case(
                pack,
                retrieve.get("results", []),
            )
        )
    return {
        "advisory": True,
        "cases": case_reports,
        **summarize_authority_support_validation(case_reports),
    }


def request_url(url: str, *, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GET {url} failed with HTTP {exc.code}: {body[:300]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GET {url} failed: {exc}") from exc


def request_json(url: str, *, timeout: float) -> dict[str, Any]:
    return json.loads(request_url(url, timeout=timeout).decode("utf-8"))


def request_text(url: str, *, timeout: float) -> str:
    return request_url(url, timeout=timeout).decode("utf-8", errors="replace")


def client_request(
    client: Client,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    timeout: float = 60.0,
    attempts: int = 4,
) -> Any:
    for attempt in range(1, attempts + 1):
        try:
            return client.request(method, path, body=body, query=query, timeout=timeout)
        except ApiError as exc:
            if exc.status not in RETRYABLE_HTTP_STATUSES or attempt == attempts:
                raise
            wait = min(2 ** (attempt - 1), 8)
            print(
                f"retrying {method} {path} after HTTP {exc.status}; "
                f"attempt {attempt + 1}/{attempts} in {wait}s",
                file=sys.stderr,
            )
            time.sleep(wait)
        except RuntimeError as exc:
            if attempt == attempts:
                raise
            wait = min(2 ** (attempt - 1), 8)
            print(
                f"retrying {method} {path} after transient client error: {exc}; "
                f"attempt {attempt + 1}/{attempts} in {wait}s",
                file=sys.stderr,
            )
            time.sleep(wait)
    raise RuntimeError(f"{method} {path} failed after {attempts} attempts")


def queue_metric(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def relationship_queue_summary(control_tower: dict[str, Any]) -> dict[str, Any]:
    queues = (control_tower.get("worker_backpressure") or {}).get("queues", [])
    for queue in queues:
        if not isinstance(queue, dict):
            continue
        key = str(queue.get("key") or "").lower()
        label = str(queue.get("label") or "").lower()
        if key not in RELATIONSHIP_QUEUE_KEYS and "relationship" not in label:
            continue
        return {
            "key": queue.get("key"),
            "label": queue.get("label"),
            "queued_depth": queue_metric(queue.get("queued_depth")),
            "deferred_depth": queue_metric(queue.get("deferred_depth")),
            "worker_queue_depth": queue_metric(queue.get("worker_queue_depth")),
            "recent_failed": queue_metric(queue.get("recent_failed")),
            "telemetry_error": queue.get("telemetry_error"),
        }
    return {
        "key": "relationships",
        "missing": True,
        "queued_depth": 0,
        "deferred_depth": 0,
        "worker_queue_depth": 0,
        "recent_failed": 0,
        "telemetry_error": None,
    }


def relationship_queue_needs_wait(summary: dict[str, Any]) -> bool:
    if summary.get("telemetry_error"):
        return True
    return any(queue_metric(summary.get(field)) for field in RELATIONSHIP_QUEUE_DRAIN_FIELDS)


def relationship_queue_latency_seconds(summary: dict[str, Any]) -> float | None:
    value = summary.get("recent_avg_latency_seconds")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def wait_for_relationship_queue_drained(
    client: Client,
    *,
    timeout_seconds: int,
    interval_seconds: int,
) -> int:
    deadline = time.monotonic() + timeout_seconds
    last_summary: dict[str, Any] = {}

    while True:
        control_tower = client_request(client, "GET", "/api/v1/palace/control-tower", timeout=60)
        summary = relationship_queue_summary(control_tower)
        if summary != last_summary:
            print(json.dumps({"relationship_queue": summary}, sort_keys=True))
            last_summary = summary

        if not relationship_queue_needs_wait(summary):
            return 0
        if time.monotonic() >= deadline:
            print("timed out waiting for relationship queue to drain", file=sys.stderr)
            return 2
        time.sleep(interval_seconds)


def parse_ris(text: str, *, path: str, raw_url: str) -> Publication | None:
    fields: dict[str, list[str]] = {}
    for line in text.splitlines():
        if len(line) >= 6 and line[2:6] == "  - ":
            key = line[:2].strip()
            value = line[6:].strip()
            if value:
                fields.setdefault(key, []).append(value)

    publication_id = Path(path).name.removesuffix(".ris").removeprefix("NIST.SP.")
    title = first_field(fields, "TI", "T1")
    doi = first_field(fields, "DO")
    if not title or not doi:
        return None
    return Publication(
        publication_id=publication_id,
        ris_path=path,
        raw_ris_url=raw_url,
        title=title,
        year=first_field(fields, "PY", "Y1"),
        doi=doi,
    )


def first_field(fields: dict[str, list[str]], *keys: str) -> str | None:
    for key in keys:
        values = fields.get(key)
        if values:
            return values[0]
    return None


def discover_publications(candidate_count: int, *, timeout: float) -> list[Publication]:
    payload = request_json(NIST_TREE_API_URL, timeout=timeout)
    tree = payload.get("tree")
    if not isinstance(tree, list):
        raise RuntimeError(f"unexpected NIST tree response from {NIST_TREE_API_URL}")

    ris_paths = [
        item["path"]
        for item in tree
        if isinstance(item, dict)
        and isinstance(item.get("path"), str)
        and item["path"].startswith("bib/NIST.SP.800-")
        and item["path"].endswith(".ris")
    ]
    by_key = {
        normalize_publication_id(Path(path).name.removesuffix(".ris").removeprefix("NIST.SP.")): path
        for path in ris_paths
        if not is_nonfinal_or_translation(path)
    }

    selected_paths: list[str] = []
    seen: set[str] = set()
    for publication_id in PREFERRED_PUBLICATION_IDS:
        path = by_key.get(normalize_publication_id(publication_id))
        if path and path not in seen:
            selected_paths.append(path)
            seen.add(path)

    for path in sorted(by_key.values()):
        if len(selected_paths) >= candidate_count:
            break
        if path not in seen:
            selected_paths.append(path)
            seen.add(path)

    publications: list[Publication] = []
    for path in selected_paths:
        raw_url = f"{NIST_RAW_BASE_URL}/{urllib.parse.quote(path)}"
        try:
            publication = parse_ris(request_text(raw_url, timeout=timeout), path=path, raw_url=raw_url)
        except RuntimeError as exc:
            print(f"metadata skip {path}: {exc}", file=sys.stderr)
            continue
        if publication:
            publications.append(publication)
        if len(publications) >= candidate_count:
            break

    if not publications:
        raise RuntimeError("no usable NIST SP 800 publication metadata found")
    return publications


def is_nonfinal_or_translation(path: str) -> bool:
    lowered = path.lower()
    excluded_markers = (
        ".ipd.",
        ".fpd.",
        ".2pd.",
        ".3pd.",
        ".4pd.",
        ".iwd.",
        ".upd.",
        ".wrd.",
        "draft",
        "errata",
        ".fre.",
        ".spa.",
        ".por.",
        ".ukr.",
        ".slo.",
        ".chi.",
        ".jpn.",
        ".kor.",
        ".pol.",
        ".rus.",
    )
    return any(marker in lowered for marker in excluded_markers)


def download_pdf(publication: Publication, *, cache_dir: Path, timeout: float) -> tuple[Path, str]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = cache_dir / f"{slugify(publication.publication_id)}.pdf"
    url_path = cache_dir / f"{slugify(publication.publication_id)}.pdf.url"
    if pdf_path.exists() and pdf_path.stat().st_size > 0:
        pdf_url = url_path.read_text(encoding="utf-8").strip() if url_path.exists() else publication.doi_url
        return pdf_path, pdf_url

    req = urllib.request.Request(publication.doi_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            pdf_bytes = resp.read()
            pdf_url = resp.geturl()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GET {publication.doi_url} failed with HTTP {exc.code}: {body[:300]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GET {publication.doi_url} failed: {exc}") from exc

    if not pdf_bytes.startswith(b"%PDF"):
        raise RuntimeError(f"{publication.doi_url} did not resolve to a PDF; final URL was {pdf_url}")
    pdf_path.write_bytes(pdf_bytes)
    url_path.write_text(pdf_url + "\n", encoding="utf-8")
    return pdf_path, pdf_url


def extract_pdf_text(pdf_path: Path, *, cache_dir: Path) -> str:
    pdftotext = shutil.which("pdftotext")
    if not pdftotext:
        raise RuntimeError("pdftotext is required; install poppler or add pdftotext to PATH")

    text_path = cache_dir / f"{pdf_path.stem}.txt"
    if not text_path.exists() or text_path.stat().st_mtime < pdf_path.stat().st_mtime:
        subprocess.run([pdftotext, "-layout", str(pdf_path), str(text_path)], check=True)
    return text_path.read_text(encoding="utf-8", errors="replace")


def clean_text(raw: str) -> str:
    text = raw.replace("\x0c", "\n")
    text = re.sub(r"-\n(?=[a-z])", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    paragraphs = [
        re.sub(r"\s+", " ", paragraph).strip()
        for paragraph in re.split(r"\n\s*\n", text)
    ]
    meaningful = [
        paragraph
        for paragraph in paragraphs
        if len(paragraph) >= 80 and not paragraph.lower().startswith("nist special publication")
    ]
    return "\n\n".join(meaningful)


def chunk_text(text: str, *, chunk_chars: int, overlap_chars: int) -> list[tuple[int, str]]:
    chunks: list[tuple[int, str]] = []
    start = 0
    text_len = len(text)
    while start < text_len:
        end = min(start + chunk_chars, text_len)
        if end < text_len:
            sentence_end = max(
                text.rfind(". ", start + max(500, chunk_chars // 2), end),
                text.rfind("\n\n", start + max(500, chunk_chars // 2), end),
            )
            if sentence_end > start:
                end = sentence_end + 1
        chunk = text[start:end].strip()
        if len(chunk) >= 500:
            chunks.append((start, chunk))
        if end >= text_len:
            break
        start = max(end - overlap_chars, start + 1)
    return chunks


def select_evenly(chunks: list[tuple[int, str]], count: int) -> list[tuple[int, tuple[int, str]]]:
    if len(chunks) <= count:
        return list(enumerate(chunks))
    if count == 1:
        index = len(chunks) // 2
        return [(index, chunks[index])]
    selected: list[tuple[int, tuple[int, str]]] = []
    seen: set[int] = set()
    for position in range(count):
        index = round(position * (len(chunks) - 1) / (count - 1))
        while index in seen and index + 1 < len(chunks):
            index += 1
        if index in seen:
            continue
        seen.add(index)
        selected.append((index, chunks[index]))
    return selected


def topic_tags_for(publication: Publication, chunk: str) -> list[str]:
    haystack = f"{publication.publication_id} {publication.title} {chunk[:1200]}".lower()
    tags: list[str] = []
    for tag, needles in TOPIC_RULES:
        if any(needle in haystack for needle in needles):
            tags.append(tag)
    return tags


def build_corpus(args: argparse.Namespace) -> list[CorpusChunk]:
    publications = discover_publications(args.source_document_candidates, timeout=args.source_timeout)
    cache_dir = nist_cache_dir()
    corpus: list[CorpusChunk] = []
    skipped: list[dict[str, str]] = []

    for publication in publications:
        if len(corpus) >= args.target_count:
            break
        try:
            pdf_path, pdf_url = download_pdf(publication, cache_dir=cache_dir, timeout=args.source_timeout)
            raw_text = extract_pdf_text(pdf_path, cache_dir=cache_dir)
            cleaned = clean_text(raw_text)
            source_hash = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
            all_chunks = chunk_text(
                cleaned,
                chunk_chars=args.chunk_chars,
                overlap_chars=args.overlap_chars,
            )
            selected_chunks = select_evenly(all_chunks, args.chunks_per_document)
            if not selected_chunks:
                raise RuntimeError("no extractable chunks")
        except (RuntimeError, subprocess.CalledProcessError) as exc:
            skipped.append({"publication_id": publication.publication_id, "reason": str(exc)})
            print(f"source skip {publication.publication_id}: {exc}", file=sys.stderr)
            continue

        print(
            f"prepared {publication.publication_id}: {len(selected_chunks)} chunk(s) "
            f"from {len(all_chunks)} window(s)"
        )
        for source_chunk_index, (offset, text) in selected_chunks:
            if len(corpus) >= args.target_count:
                break
            corpus.append(
                CorpusChunk(
                    index=len(corpus),
                    publication_id=publication.publication_id,
                    publication_title=publication.title,
                    publication_year=publication.year,
                    doi=publication.doi,
                    doi_url=publication.doi_url,
                    pdf_url=pdf_url,
                    source_text_sha256=source_hash,
                    chunk_index=source_chunk_index,
                    chunk_offset=offset,
                    chunk_text=text,
                )
            )

    if len(corpus) < args.target_count:
        raise SystemExit(
            f"only prepared {len(corpus)}/{args.target_count} NIST chunks; "
            f"increase --source-document-candidates or lower --target-count"
        )

    write_manifest(args.run_id, corpus, skipped, args)
    return corpus


def write_manifest(
    run_id: str,
    corpus: list[CorpusChunk],
    skipped: list[dict[str, str]],
    args: argparse.Namespace,
) -> None:
    path = nist_manifest_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    publications: dict[str, dict[str, Any]] = {}
    for chunk in corpus:
        publications.setdefault(
            chunk.publication_id,
            {
                "publication_id": chunk.publication_id,
                "title": chunk.publication_title,
                "year": chunk.publication_year,
                "doi": chunk.doi,
                "doi_url": chunk.doi_url,
                "pdf_url": chunk.pdf_url,
                "source_text_sha256": chunk.source_text_sha256,
                "chunks": 0,
                "selected_chunks": [],
            },
        )
        publications[chunk.publication_id]["chunks"] += 1
        publications[chunk.publication_id]["selected_chunks"].append(
            {
                "chunk_index": chunk.chunk_index,
                "chunk_offset": chunk.chunk_offset,
            }
        )

    included = set(publications)
    evals = [
        query
        for query in EVAL_QUERIES
        if any(normalize_publication_id(pub) in {normalize_publication_id(item) for item in included}
               for pub in query["expected_publications"])
    ]
    manifest = {
        "benchmark": "nist-sp800-corpus",
        "run_id": run_id,
        "run_tag": run_tag(run_id),
        "generated_at": utc_now().isoformat(),
        "source": {
            "catalog": NIST_TECH_PUBS_PAGE_URL,
            "metadata_tree": NIST_TREE_API_URL,
            "metadata_readme": NIST_TECH_PUBS_README_URL,
        },
        "target_count": args.target_count,
        "actual_count": len(corpus),
        "chunks_per_document": args.chunks_per_document,
        "chunk_chars": args.chunk_chars,
        "overlap_chars": args.overlap_chars,
        "relationship_policy": args.relationship_policy,
        "enable_ai_enrichment": args.enable_ai_enrichment,
        "publications": list(publications.values()),
        "skipped_publications": skipped,
        "eval_queries": evals,
        "contrastive_eval_packs": build_contrastive_eval_packs(list(publications.values()), evals),
        "authority_eval_packs": build_authority_eval_packs(
            {
                "run_id": run_id,
                "run_tag": run_tag(run_id),
                "relationship_policy": args.relationship_policy,
                "publications": list(publications.values()),
            }
        ),
    }
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {path}")


def read_manifest(run_id: str) -> dict[str, Any]:
    path = nist_manifest_path(run_id)
    if not path.exists():
        raise SystemExit(f"missing corpus manifest: {path}; run prepare or run with --rebuild-corpus")
    return json.loads(path.read_text(encoding="utf-8"))


def read_corpus_from_manifest(run_id: str) -> list[CorpusChunk]:
    path = nist_manifest_path(run_id)
    if not path.exists():
        raise SystemExit(f"missing corpus manifest: {path}; run prepare first")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    cache_dir = nist_cache_dir()
    corpus: list[CorpusChunk] = []
    index = 0
    for publication_info in manifest.get("publications", []):
        publication = Publication(
            publication_id=publication_info["publication_id"],
            ris_path="",
            raw_ris_url="",
            title=publication_info["title"],
            year=publication_info.get("year"),
            doi=publication_info["doi"],
        )
        text_path = cache_dir / f"{slugify(publication.publication_id)}.txt"
        if not text_path.exists():
            raise SystemExit(f"missing cached extracted text: {text_path}; rerun with --rebuild-corpus")
        cleaned = clean_text(text_path.read_text(encoding="utf-8", errors="replace"))
        source_hash = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
        all_chunks = chunk_text(
            cleaned,
            chunk_chars=int(manifest["chunk_chars"]),
            overlap_chars=int(manifest["overlap_chars"]),
        )
        selected_chunks = select_evenly(all_chunks, int(manifest["chunks_per_document"]))
        for source_chunk_index, (offset, text) in selected_chunks:
            if index >= int(manifest["actual_count"]):
                break
            corpus.append(
                CorpusChunk(
                    index=index,
                    publication_id=publication.publication_id,
                    publication_title=publication.title,
                    publication_year=publication.year,
                    doi=publication.doi,
                    doi_url=publication.doi_url,
                    pdf_url=publication_info["pdf_url"],
                    source_text_sha256=source_hash,
                    chunk_index=source_chunk_index,
                    chunk_offset=offset,
                    chunk_text=text,
                )
            )
            index += 1
    return corpus[: int(manifest["actual_count"])]


def _load_parquet_records(path: Path) -> list[dict[str, Any]]:
    try:
        import pyarrow.parquet as pq  # type: ignore[import-not-found]
    except ImportError as exc:
        raise SystemExit(
            f"reading parquet input requires pyarrow; install it or convert {path} to JSONL"
        ) from exc
    table = pq.read_table(path)
    return table.to_pylist()


def load_hf_nist_rows(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        return _load_parquet_records(path)
    if suffix == ".jsonl":
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            return list(payload["data"])
        raise SystemExit(f"{path} must contain a JSON array or object with a data array")
    raise SystemExit(f"unsupported HF NIST input format for {path}; use .jsonl, .json, or .parquet")


def parse_hf_nist_metadata(value: Any, *, row_id: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"HF NIST row {row_id} metadata is not valid JSON") from exc
        if isinstance(parsed, dict):
            return parsed
    raise SystemExit(f"HF NIST row {row_id} metadata must be an object or JSON object string")


def publication_id_from_hf_source(source: str) -> str | None:
    matches = PUBLICATION_ID_RE.findall(source)
    if not matches:
        return None
    return matches[0]


def source_url_from_hf_metadata(metadata: dict[str, Any]) -> str:
    for key in ("url", "source_url", "doi", "doi_url"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            if key == "doi" and not value.startswith(("http://", "https://")):
                return f"https://doi.org/{value}"
            return value.strip()
    return HF_NIST_DATASET_URL


def _missing_hf_publication_provenance(publication: dict[str, Any]) -> list[str]:
    missing = [
        field
        for field in ("publication_id", "source", "source_url", "chunks", "selected_chunks")
        if publication.get(field) in (None, "", [])
    ]
    selected_chunks = publication.get("selected_chunks")
    if isinstance(selected_chunks, list) and selected_chunks:
        required_chunk_fields = ("split", "row_id", "chunk_index", "chunk_offset", "text_sha256", "token_count")
        for index, chunk in enumerate(selected_chunks):
            if not isinstance(chunk, dict):
                missing.append(f"selected_chunks[{index}]")
                continue
            missing.extend(
                f"selected_chunks[{index}].{field}"
                for field in required_chunk_fields
                if chunk.get(field) in (None, "", [])
            )
    return sorted(set(missing))


def _missing_hf_record_provenance(record: dict[str, Any]) -> list[str]:
    required_fields = (
        "split",
        "row_id",
        "source",
        "source_url",
        "publication_id",
        "chunk_index",
        "chunk_offset",
        "text_sha256",
        "embedding_model",
        "embedding_dimension",
    )
    return [
        field
        for field in required_fields
        if record.get(field) in (None, "", [])
    ]


def summarize_hf_authority_manifest_validation(manifest: dict[str, Any]) -> dict[str, Any]:
    """Summarize HF authority corpus readiness without retrieval or answer enforcement."""
    publications = [
        publication
        for publication in manifest.get("publications", [])
        if isinstance(publication, dict)
    ]
    publications_by_id = {
        normalize_publication_id(str(publication.get("publication_id"))): publication
        for publication in publications
        if publication.get("publication_id")
    }
    records_by_publication: dict[str, list[dict[str, Any]]] = {}
    for record in manifest.get("records", []):
        if not isinstance(record, dict) or not record.get("publication_id"):
            continue
        records_by_publication.setdefault(
            normalize_publication_id(str(record["publication_id"])),
            [],
        ).append(record)

    expected_governing_sources = {
        normalize_publication_id(str(case["expected_governing_source"]))
        for case in AUTHORITY_EVAL_CASES
    }
    covered_governing_sources = sorted(
        source
        for source in expected_governing_sources
        if source in publications_by_id
    )
    missing_governing_sources = sorted(expected_governing_sources - set(covered_governing_sources))

    cases: list[dict[str, Any]] = []
    for case in AUTHORITY_EVAL_CASES:
        case_id = str(case["id"])
        expected_source = normalize_publication_id(str(case["expected_governing_source"]))
        confusing_sources = [
            normalize_publication_id(str(source))
            for source in case.get("confusing_sources_to_demote", [])
            if source
        ]
        publication = publications_by_id.get(expected_source)
        publication_missing = _missing_hf_publication_provenance(publication) if publication else []
        source_records = records_by_publication.get(expected_source, [])
        record_missing = sorted(
            {
                field
                for record in source_records[:5]
                for field in _missing_hf_record_provenance(record)
            }
        )
        support_metadata_ready = bool(publication and source_records and not publication_missing and not record_missing)
        weak_support_state = not support_metadata_ready
        present_confusing_sources = sorted(
            source
            for source in confusing_sources
            if source in publications_by_id
        )
        cases.append(
            {
                "id": case_id,
                "question_type": case["question_type"],
                "status": "pass" if support_metadata_ready else "weak_support",
                "expected_governing_source": case["expected_governing_source"],
                "governing_source_present": publication is not None,
                "support_metadata_ready": support_metadata_ready,
                "weak_support_state": weak_support_state,
                "publication_provenance_missing_fields": publication_missing,
                "record_provenance_missing_fields": record_missing,
                "record_count": len(source_records),
                "confusing_sources_to_demote": case.get("confusing_sources_to_demote", []),
                "confusing_sources_present": present_confusing_sources,
            }
        )

    weak_case_ids = [case["id"] for case in cases if case["weak_support_state"]]
    missing_provenance_case_ids = [
        case["id"]
        for case in cases
        if case["publication_provenance_missing_fields"] or case["record_provenance_missing_fields"]
    ]
    adjacent_cases = [case for case in cases if case["confusing_sources_to_demote"]]
    adjacent_present_cases = [
        case["id"]
        for case in adjacent_cases
        if case["confusing_sources_present"]
    ]
    return {
        "advisory": True,
        "offline_report_only": True,
        "source_plan": AUTHORITY_EVAL_PLAN_PATH,
        "case_count": len(cases),
        "ready_for_answer_support_validation": bool(cases) and not weak_case_ids,
        "governing_source_coverage": {
            "covered": len(covered_governing_sources),
            "expected": len(expected_governing_sources),
            "covered_publication_ids": covered_governing_sources,
            "missing_publication_ids": missing_governing_sources,
        },
        "adjacent_source_confusion": {
            "case_count": len(adjacent_cases),
            "cases_with_confusing_sources_present": len(adjacent_present_cases),
            "case_ids_with_confusing_sources_present": adjacent_present_cases,
        },
        "weak_support": {
            "cases": len(weak_case_ids),
            "weak_support_case_ids": weak_case_ids,
        },
        "provenance": {
            "missing_provenance_cases": len(missing_provenance_case_ids),
            "missing_provenance_case_ids": missing_provenance_case_ids,
            "preserved_fields": (manifest.get("provenance_contract") or {}).get("preserved_fields", []),
        },
        "cases": cases,
    }


def split_hf_nist_input_spec(spec: str) -> tuple[str, Path]:
    if "=" in spec:
        split, raw_path = spec.split("=", 1)
        split = split.strip()
        if not split:
            raise SystemExit(f"invalid --input {spec!r}: split name is empty")
        if split == "valid":
            split = "validation"
        return split, Path(raw_path)
    return "unknown", Path(spec)


def prepare_hf_nist_authority_manifest(
    *,
    run_id: str,
    inputs: list[str],
    sample_limit: int | None,
) -> dict[str, Any]:
    rows_by_split: dict[str, list[dict[str, Any]]] = {}
    for input_spec in inputs:
        split, path = split_hf_nist_input_spec(input_spec)
        if not path.exists():
            raise SystemExit(f"missing HF NIST input file: {path}")
        rows = load_hf_nist_rows(path)
        if sample_limit is not None:
            rows = rows[:sample_limit]
        rows_by_split.setdefault(split, []).extend(rows)

    records: list[dict[str, Any]] = []
    publications: dict[str, dict[str, Any]] = {}
    split_counts: dict[str, int] = {}
    token_counts: dict[str, int] = {}
    embedding_dimensions: set[int] = set()
    type_counts: dict[str, int] = {}

    for split, rows in rows_by_split.items():
        split_counts[split] = len(rows)
        for position, row in enumerate(rows):
            row_id = str(row.get("id") if row.get("id") is not None else f"{split}:{position}")
            text = row.get("text")
            embedding = row.get("embedding")
            if not isinstance(text, str) or not text.strip():
                raise SystemExit(f"HF NIST row {row_id} is missing non-empty text")
            if not isinstance(embedding, list):
                raise SystemExit(f"HF NIST row {row_id} is missing embedding list")
            embedding_dimensions.add(len(embedding))
            if len(embedding) != HF_NIST_EMBEDDING_DIMENSION:
                raise SystemExit(
                    f"HF NIST row {row_id} embedding dimension {len(embedding)} does not match "
                    f"{HF_NIST_EMBEDDING_DIMENSION}"
                )
            metadata = parse_hf_nist_metadata(row.get("metadata"), row_id=row_id)
            source = metadata.get("source")
            if not isinstance(source, str) or not source.strip():
                raise SystemExit(f"HF NIST row {row_id} metadata is missing source")

            chunk_id = metadata.get("chunk_id")
            chunk_index = int(chunk_id) if isinstance(chunk_id, int | str) and str(chunk_id).isdigit() else position
            text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            token_count = len(re.findall(r"\S+", text))
            record_type = str(metadata.get("type") or "unknown")
            publication_id = publication_id_from_hf_source(source) or f"source-{slugify(source)[:80]}"
            source_url = source_url_from_hf_metadata(metadata)

            split_counts[split] = split_counts.get(split, 0)
            token_counts[split] = token_counts.get(split, 0) + token_count
            type_counts[record_type] = type_counts.get(record_type, 0) + 1
            publication = publications.setdefault(
                publication_id,
                {
                    "publication_id": publication_id,
                    "source": source,
                    "source_url": source_url,
                    "dataset_reference": HF_NIST_DATASET_URL,
                    "chunks": 0,
                    "selected_chunks": [],
                    "text_sha256_samples": [],
                    "embedding_model": HF_NIST_EMBEDDING_MODEL,
                    "embedding_dimension": HF_NIST_EMBEDDING_DIMENSION,
                    "splits": [],
                    "types": [],
                },
            )
            publication["chunks"] += 1
            if split not in publication["splits"]:
                publication["splits"].append(split)
            if record_type not in publication["types"]:
                publication["types"].append(record_type)
            if len(publication["selected_chunks"]) < 5:
                publication["selected_chunks"].append(
                    {
                        "split": split,
                        "row_id": row_id,
                        "chunk_index": chunk_index,
                        "chunk_offset": 0,
                        "text_sha256": text_hash,
                        "token_count": token_count,
                    }
                )
            if len(publication["text_sha256_samples"]) < 5:
                publication["text_sha256_samples"].append(text_hash)

            records.append(
                {
                    "split": split,
                    "row_id": row_id,
                    "publication_id": publication_id,
                    "source": source,
                    "source_url": source_url,
                    "type": record_type,
                    "chunk_index": chunk_index,
                    "chunk_offset": 0,
                    "text_sha256": text_hash,
                    "token_count": token_count,
                    "embedding_model": HF_NIST_EMBEDDING_MODEL,
                    "embedding_dimension": len(embedding),
                    "metadata": metadata,
                }
            )

    observed_total = len(records)
    expected_observed_total = sum(split_counts.values())
    if observed_total != expected_observed_total:
        raise SystemExit(f"HF NIST manifest row accounting mismatch: {observed_total} != {expected_observed_total}")
    if sample_limit is None:
        for split, expected_count in HF_NIST_EXPECTED_SPLIT_ROWS.items():
            if split in split_counts and split_counts[split] != expected_count:
                raise SystemExit(
                    f"HF NIST split {split} row count {split_counts[split]} does not match "
                    f"dataset card count {expected_count}"
                )
        if set(HF_NIST_EXPECTED_SPLIT_ROWS).issubset(split_counts) and observed_total != HF_NIST_EXPECTED_TOTAL_ROWS:
            raise SystemExit(
                f"HF NIST total row count {observed_total} does not match "
                f"dataset card count {HF_NIST_EXPECTED_TOTAL_ROWS}"
            )

    manifest = {
        "benchmark": "nist-hf-authority-corpus",
        "run_id": run_id,
        "generated_at": utc_now().isoformat(),
        "source": {
            "dataset_id": HF_NIST_DATASET_ID,
            "dataset_url": HF_NIST_DATASET_URL,
            "dataset_version": HF_NIST_DATASET_VERSION,
            "license": HF_NIST_LICENSE,
            "license_note": "Dataset card states CC0 1.0 Universal and NIST publications are public domain.",
            "expected_total_rows": HF_NIST_EXPECTED_TOTAL_ROWS,
            "expected_split_rows": HF_NIST_EXPECTED_SPLIT_ROWS,
            "expected_documents": HF_NIST_EXPECTED_DOCUMENTS,
        },
        "precomputed_embedding_trust": {
            "trusted": True,
            "model": HF_NIST_EMBEDDING_MODEL,
            "dimension": HF_NIST_EMBEDDING_DIMENSION,
            "openai_embedding_calls": 0,
            "decision": "Use dataset-provided embeddings for offline authority-eval preparation.",
        },
        "observed": {
            "sample_limited": sample_limit is not None,
            "rows": observed_total,
            "split_rows": split_counts,
            "token_count": sum(token_counts.values()),
            "token_count_by_split": token_counts,
            "embedding_dimensions": sorted(embedding_dimensions),
            "type_counts": type_counts,
            "publication_count": len(publications),
        },
        "validation": {
            "expected_row_counts_checked": sample_limit is None,
            "expected_total_rows": HF_NIST_EXPECTED_TOTAL_ROWS,
            "expected_split_rows": HF_NIST_EXPECTED_SPLIT_ROWS,
            "embedding_summary_checked": True,
            "token_count_source": "computed from local input text with whitespace tokenization",
        },
        "provenance_contract": {
            "preserved_fields": [
                "dataset_reference",
                "split",
                "row_id",
                "source",
                "source_url",
                "publication_id",
                "chunk_index",
                "chunk_offset",
                "text_sha256",
                "embedding_model",
                "embedding_dimension",
            ],
            "openai_reembedding_allowed": False,
            "live_ingest_allowed": False,
            "production_data_deletion_allowed": False,
        },
        "publications": sorted(publications.values(), key=lambda item: item["publication_id"]),
        "records": records,
    }
    manifest["authority_report"] = summarize_hf_authority_manifest_validation(manifest)
    return manifest


def cmd_prepare_hf_authority_corpus(args: argparse.Namespace) -> int:
    run_id = validate_base_run_id(args.run_id)
    manifest = prepare_hf_nist_authority_manifest(
        run_id=run_id,
        inputs=args.input,
        sample_limit=args.sample_limit,
    )
    path = Path(args.output) if args.output else nist_hf_manifest_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "run_id": run_id,
                "manifest": str(path),
                "rows": manifest["observed"]["rows"],
                "token_count": manifest["observed"]["token_count"],
                "embedding_model": manifest["precomputed_embedding_trust"]["model"],
                "embedding_dimension": manifest["precomputed_embedding_trust"]["dimension"],
                "openai_embedding_calls": 0,
                "authority_ready_for_answer_support_validation": manifest["authority_report"][
                    "ready_for_answer_support_validation"
                ],
                "authority_weak_support_cases": manifest["authority_report"]["weak_support"]["cases"],
                "authority_missing_provenance_cases": manifest["authority_report"]["provenance"][
                    "missing_provenance_cases"
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def cmd_report_hf_authority_corpus(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        raise SystemExit(f"missing HF NIST manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise SystemExit(f"HF NIST manifest must be a JSON object: {manifest_path}")
    report = {
        "manifest": str(manifest_path),
        "run_id": manifest.get("run_id"),
        "benchmark": manifest.get("benchmark"),
        "source": manifest.get("source"),
        "observed": manifest.get("observed"),
        "precomputed_embedding_trust": manifest.get("precomputed_embedding_trust"),
        "provenance_contract": manifest.get("provenance_contract"),
        "authority_report": summarize_hf_authority_manifest_validation(manifest),
    }
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {output_path}")
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["authority_report"]["ready_for_answer_support_validation"] else 1


def load_or_build_corpus(args: argparse.Namespace) -> list[CorpusChunk]:
    manifest = nist_manifest_path(args.run_id)
    if args.rebuild_corpus or not manifest.exists():
        return build_corpus(args)
    return read_corpus_from_manifest(args.run_id)


def make_entry(
    run_id: str,
    tenant_id: str,
    chunk: CorpusChunk,
    *,
    enable_ai_enrichment: bool,
    relationship_policy: str,
) -> dict[str, Any]:
    sentinel = f"NIST-CORPUS-{run_id}-{chunk.index:04d}"
    publication_tag = f"nist-{slugify(chunk.publication_id)}"
    tags = [
        "benchmark",
        "benchmark-cleanup-ok",
        run_tag(run_id),
        "nist",
        "nist-sp800",
        "nist-corpus",
        "corpus-nist-sp800",
        publication_tag,
        *topic_tags_for(
            Publication(
                publication_id=chunk.publication_id,
                ris_path="",
                raw_ris_url="",
                title=chunk.publication_title,
                year=chunk.publication_year,
                doi=chunk.doi,
            ),
            chunk.chunk_text,
        ),
    ]
    unique_tags = list(dict.fromkeys(tags))
    title = f"NIST SP {chunk.publication_id} - {chunk.publication_title} - chunk {chunk.chunk_index:03d}"
    body = (
        f"{sentinel}\n"
        f"Publication: NIST SP {chunk.publication_id}\n"
        f"Title: {chunk.publication_title}\n"
        f"DOI: {chunk.doi_url}\n"
        f"PDF: {chunk.pdf_url}\n\n"
        f"{chunk.chunk_text}"
    )
    created = utc_now() - timedelta(seconds=chunk.index)
    return {
        "tenant_id": tenant_id,
        "title": title,
        "summary": (
            f"NIST SP {chunk.publication_id} corpus benchmark excerpt from "
            f"{chunk.publication_title}."
        ),
        "body": body,
        "source": "nist-sp800-corpus-benchmark",
        "source_url": f"{chunk.doi_url}#benchmark-chunk-{chunk.chunk_index:03d}",
        "created_at": created.isoformat(),
        "created_by_role": "benchmark-operator",
        "tags": unique_tags,
        "scope": {"type": "tenant_shared", "key": None},
        "metadata": {
            "benchmark": {
                "run_id": run_id,
                "index": chunk.index,
                "cleanup_allowed": True,
                "corpus": "nist-sp800",
            },
            "nist": {
                "publication_id": chunk.publication_id,
                "title": chunk.publication_title,
                "year": chunk.publication_year,
                "doi": chunk.doi,
                "doi_url": chunk.doi_url,
                "pdf_url": chunk.pdf_url,
                "chunk_index": chunk.chunk_index,
                "chunk_offset": chunk.chunk_offset,
                "source_text_sha256": chunk.source_text_sha256,
                "chunk_sha256": hashlib.sha256(chunk.chunk_text.encode("utf-8")).hexdigest(),
            },
        },
        "idempotency_key": build_nist_idempotency_key(run_id, chunk.index),
        "enable_ai_enrichment": enable_ai_enrichment,
        "relationship_policy": relationship_policy,
    }


def cmd_prepare(args: argparse.Namespace) -> int:
    args.run_id = validate_run_id(args.run_id or default_run_id())
    build_corpus(args)
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    client = client_from_args(args)
    args.run_id = validate_run_id(args.run_id or default_run_id())
    tenant_id = resolve_tenant_id(client, args.tenant_id)
    with acquire_run_artifact_lock(args.run_id, namespace="nist-sp800", purpose="NIST SP 800 benchmark ingest"):
        corpus = load_or_build_corpus(args)
        path = nist_artifact_path(args.run_id)
        if path.exists() and not args.resume:
            raise SystemExit(f"{path} already exists; pass --resume or choose a new --run-id")

        existing = {int(row["index"]) for row in read_jsonl(path)} if path.exists() else set()
        chunks = [chunk for chunk in corpus if chunk.index not in existing]
        print(
            f"target={client.base_url} tenant={tenant_id} run_id={args.run_id} "
            f"count={len(corpus)} remaining={len(chunks)} relationship_policy={args.relationship_policy}"
        )
        print(f"run_tag={run_tag(args.run_id)} artifact={path}")
        if args.dry_run:
            sample = make_entry(
                args.run_id,
                tenant_id,
                corpus[0],
                enable_ai_enrichment=args.enable_ai_enrichment,
                relationship_policy=args.relationship_policy,
            )
            print(json.dumps(sample, indent=2))
            return 0

        def submit(chunk: CorpusChunk) -> dict[str, Any]:
            entry = make_entry(
                args.run_id,
                tenant_id,
                chunk,
                enable_ai_enrichment=args.enable_ai_enrichment,
                relationship_policy=args.relationship_policy,
            )
            started = time.monotonic()
            accepted = client_request(
                client,
                "POST",
                "/api/v1/memory/entries",
                body=entry,
                timeout=args.request_timeout,
            )
            latency_ms = int((time.monotonic() - started) * 1000)
            return {
                "run_id": args.run_id,
                "index": chunk.index,
                "job_id": accepted["job_id"],
                "status": accepted.get("status"),
                "accepted_as": accepted.get("accepted_as"),
                "accept_latency_ms": latency_ms,
                "idempotency_key": entry["idempotency_key"],
                "title": entry["title"],
                "publication_id": chunk.publication_id,
                "doi_url": chunk.doi_url,
                "tags": entry["tags"],
                "created_at": utc_now().isoformat(),
            }

        completed = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            future_to_chunk = {pool.submit(submit, chunk): chunk for chunk in chunks}
            for future in concurrent.futures.as_completed(future_to_chunk):
                chunk = future_to_chunk[future]
                try:
                    row = future.result()
                except Exception as exc:
                    print(f"ERROR index={chunk.index} publication={chunk.publication_id}: {exc}", file=sys.stderr)
                    if not args.keep_going:
                        raise
                    continue
                append_jsonl(path, row)
                completed += 1
                if completed % args.progress_every == 0 or completed == len(chunks):
                    print(f"accepted {completed}/{len(chunks)} new entries")

        print(f"done run_id={args.run_id} run_tag={run_tag(args.run_id)} artifact={path}")
    return 0


def cmd_wait(args: argparse.Namespace) -> int:
    client = client_from_args(args)
    run_id = validate_run_id(args.run_id)
    rows = read_jsonl(nist_artifact_path(run_id))
    deadline = time.monotonic() + args.timeout_seconds
    terminal = {"complete", "duplicate", "failed", "cancelled"}
    last_counts: dict[str, int] = {}

    while True:
        counts = nist_job_counts(client, rows)
        if counts != last_counts:
            print(json.dumps({"run_id": run_id, "counts": counts, "checked": len(rows)}, sort_keys=True))
            last_counts = counts
        non_terminal = sum(count for status, count in counts.items() if status not in terminal)
        failed = counts.get("failed", 0) + counts.get("cancelled", 0)
        if non_terminal == 0:
            return 1 if failed and not args.allow_failures else 0
        if time.monotonic() >= deadline:
            print("timed out waiting for NIST corpus memory jobs", file=sys.stderr)
            return 2
        time.sleep(args.interval_seconds)


def nist_job_counts(client: Client, rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        job = client_request(client, "GET", f"/api/v1/memory/jobs/{row['job_id']}", timeout=30)
        status = str(job.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
    return counts


def result_hits_expected(results: list[dict[str, Any]], expected_publications: list[str]) -> bool:
    return result_expected_rank(results, expected_publications) is not None


def result_expected_rank(results: list[dict[str, Any]], expected_publications: list[str]) -> int | None:
    expected = {normalize_publication_id(publication) for publication in expected_publications}
    for index, result in enumerate(results, start=1):
        haystack = " ".join(
            str(result.get(key) or "")
            for key in ("title", "source_url", "chunk_text")
        ).lower()
        normalized_haystack = normalize_publication_id(haystack)
        if any(publication in normalized_haystack for publication in expected):
            return index
    return None


def result_top_publications(results: list[dict[str, Any]], *, limit: int = 5) -> list[list[str]]:
    top_publications: list[list[str]] = []
    for result in results[:limit]:
        haystack = " ".join(
            str(result.get(key) or "")
            for key in ("title", "source_url", "chunk_text")
        )
        publications = [
            normalize_publication_id(publication)
            for publication in PUBLICATION_ID_RE.findall(haystack)
        ]
        top_publications.append(list(dict.fromkeys(publications)))
    return top_publications


def retained_nist_top_rank_failures(eval_reports: list[dict[str, Any]]) -> list[str]:
    reports_by_id = {report["id"]: report for report in eval_reports}
    failures = []
    for eval_id, expected_publications in RETAINED_NIST_TOP_RANK_REQUIREMENTS.items():
        report = reports_by_id.get(eval_id)
        if report is None:
            failures.append(f"required retained NIST eval {eval_id} was not run")
            continue
        if report.get("retrieve_expected_rank") != 1:
            expected = ", ".join(expected_publications)
            failures.append(
                f"retained NIST eval {eval_id} expected {expected} at retrieval rank 1; "
                f"got rank {report.get('retrieve_expected_rank')}"
            )
    return failures


def manifest_eval_queries(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    if manifest.get("eval_queries"):
        return list(manifest["eval_queries"])
    included = {normalize_publication_id(pub["publication_id"]) for pub in manifest.get("publications", [])}
    return [
        query
        for query in EVAL_QUERIES
        if any(normalize_publication_id(pub) in included for pub in query["expected_publications"])
    ]


def manifest_contrastive_eval_packs(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    if manifest.get("contrastive_eval_packs") is not None:
        return list(manifest["contrastive_eval_packs"])
    return build_contrastive_eval_packs(
        list(manifest.get("publications", [])),
        manifest_eval_queries(manifest),
    )


def run_single_eval_query(
    client: Client,
    run_id: str,
    query: dict[str, Any],
    *,
    limit: int,
) -> dict[str, Any]:
    body = {
        "query": query["query"],
        "limit": limit,
        "tags": [run_tag(run_id), "nist-sp800"],
        "tags_mode": "all",
    }
    search = client_request(client, "POST", "/api/v1/search", body=body, timeout=90)
    retrieve = client_request(
        client,
        "POST",
        "/api/v1/memory/retrieve",
        body={**body, "scope": {"type": "tenant_shared", "key": None}},
        timeout=90,
    )
    search_results = search.get("results", [])
    retrieve_results = retrieve.get("results", [])
    search_expected_rank = result_expected_rank(search_results, query["expected_publications"])
    retrieve_expected_rank = result_expected_rank(retrieve_results, query["expected_publications"])
    report = {
        "id": query["id"],
        "query": query["query"],
        "expected_publications": query["expected_publications"],
        "search_total": search.get("total", 0),
        "search_expected_hit": search_expected_rank is not None,
        "search_expected_rank": search_expected_rank,
        "search_top_titles": [row.get("title") for row in search_results[:5]],
        "search_top_publications": result_top_publications(search_results),
        "search_trace": search.get("trace"),
        "retrieve_total": retrieve.get("total", 0),
        "retrieve_expected_hit": retrieve_expected_rank is not None,
        "retrieve_expected_rank": retrieve_expected_rank,
        "retrieve_top_titles": [row.get("title") for row in retrieve_results[:5]],
        "retrieve_top_publications": result_top_publications(retrieve_results),
        "retrieve_trace": retrieve.get("trace"),
    }
    for key in ("pack_id", "source_eval_id", "question_type", "decoy_publications"):
        if key in query:
            report[key] = query[key]
    return report


def run_eval_queries(client: Client, run_id: str, manifest: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
    return [
        run_single_eval_query(client, run_id, query, limit=limit)
        for query in manifest_eval_queries(manifest)
    ]


def summarize_eval_probe_reports(probe_reports: list[dict[str, Any]]) -> dict[str, Any]:
    probe_count = len(probe_reports)
    denominator = probe_count or 1
    search_expected_hits = sum(1 for report in probe_reports if report["search_expected_hit"])
    retrieve_expected_hits = sum(1 for report in probe_reports if report["retrieve_expected_hit"])
    search_top_rank_hits = sum(1 for report in probe_reports if report["search_expected_rank"] == 1)
    retrieve_top_rank_hits = sum(1 for report in probe_reports if report["retrieve_expected_rank"] == 1)
    return {
        "probe_count": probe_count,
        "search_expected_hits": search_expected_hits,
        "search_expected_hit_ratio": search_expected_hits / denominator,
        "search_expected_top_ranks": search_top_rank_hits,
        "search_expected_top_rank_ratio": search_top_rank_hits / denominator,
        "retrieve_expected_hits": retrieve_expected_hits,
        "retrieve_expected_hit_ratio": retrieve_expected_hits / denominator,
        "retrieve_expected_top_ranks": retrieve_top_rank_hits,
        "retrieve_expected_top_rank_ratio": retrieve_top_rank_hits / denominator,
    }


def summarize_graph_relationships(graph: dict[str, Any], tag: str) -> dict[str, Any]:
    benchmark_node_ids = {
        str(node.get("id"))
        for node in graph.get("nodes", [])
        if isinstance(node, dict) and tag in (node.get("tags") or [])
    }
    benchmark_edges = [
        edge
        for edge in graph.get("edges", [])
        if isinstance(edge, dict)
        and (
            str(edge.get("source")) in benchmark_node_ids
            or str(edge.get("target")) in benchmark_node_ids
        )
    ]
    return {
        "tagged_graph_nodes": len(benchmark_node_ids),
        "edges_touching_tagged_nodes": len(benchmark_edges),
        "total_graph_nodes": len(graph.get("nodes", [])),
        "total_graph_edges": len(graph.get("edges", [])),
        "orphaned_ready_items": (graph.get("meta") or {}).get("orphaned_ready_items"),
    }


def run_contrastive_eval_packs(
    client: Client,
    run_id: str,
    manifest: dict[str, Any],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    pack_reports: list[dict[str, Any]] = []
    for pack in manifest_contrastive_eval_packs(manifest):
        probe_reports = [
            run_single_eval_query(
                client,
                run_id,
                {
                    **probe,
                    "pack_id": pack["id"],
                    "source_eval_id": pack["source_eval_id"],
                },
                limit=limit,
            )
            for probe in pack.get("probes", [])
        ]
        pack_reports.append(
            {
                "id": pack["id"],
                "source_eval_id": pack["source_eval_id"],
                "question_types": pack.get("question_types", []),
                "expected_publications": pack.get("expected_publications", []),
                "neighboring_decoys": pack.get("neighboring_decoys", []),
                **summarize_eval_probe_reports(probe_reports),
                "probes": probe_reports,
            }
        )
    return pack_reports


def matrix_run_id(matrix_id: str, target_count: int, relationship_policy: str) -> str:
    policy_slug = {"deferred": "d", "immediate": "i", "skip": "s"}[relationship_policy]
    return validate_run_id(f"{matrix_id}-{target_count}-{policy_slug}")


def build_relationship_matrix_plan(
    *,
    matrix_id: str,
    target_counts: list[int],
    relationship_policies: list[str],
    chunks_per_document: int,
    chunk_chars: int,
    overlap_chars: int,
    source_document_candidates: int,
    enable_ai_enrichment: bool,
    eval_limit: int,
    min_expected_hit_ratio: float,
    relationship_queue_timeout_seconds: int,
) -> dict[str, Any]:
    cells: list[dict[str, Any]] = []
    for target_count in target_counts:
        if target_count < 1:
            raise SystemExit("matrix target counts must be positive")
        for relationship_policy in relationship_policies:
            run_id = matrix_run_id(matrix_id, target_count, relationship_policy)
            cells.append(
                {
                    "run_id": run_id,
                    "run_tag": run_tag(run_id),
                    "target_count": target_count,
                    "relationship_policy": relationship_policy,
                    "manifest_path": str(nist_manifest_path(run_id)),
                    "artifact_path": str(nist_artifact_path(run_id)),
                    "report_path": str(nist_report_path(run_id)),
                    "cleanup_plan_path": str(nist_cleanup_plan_path(run_id)),
                }
            )
    return {
        "benchmark": "nist-sp800-relationship-matrix",
        "matrix_id": matrix_id,
        "generated_at": utc_now().isoformat(),
        "target_counts": target_counts,
        "relationship_policies": relationship_policies,
        "cell_count": len(cells),
        "corpus": {
            "chunks_per_document": chunks_per_document,
            "chunk_chars": chunk_chars,
            "overlap_chars": overlap_chars,
            "source_document_candidates": source_document_candidates,
            "enable_ai_enrichment": enable_ai_enrichment,
        },
        "evaluation": {
            "eval_limit": eval_limit,
            "min_expected_hit_ratio": min_expected_hit_ratio,
            "relationship_queue_timeout_seconds": relationship_queue_timeout_seconds,
        },
        "safety": {
            "cleanup_is_manual": True,
            "live_ingest_requires_dry_run_false": True,
            "do_not_target_hermes": True,
        },
        "cells": cells,
    }


def _read_json_if_exists(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def summarize_accept_latency(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = sorted(
        int(row["accept_latency_ms"])
        for row in rows
        if row.get("accept_latency_ms") is not None
    )
    if not values:
        return {"count": 0, "min_ms": None, "p50_ms": None, "p95_ms": None, "max_ms": None}
    p95_index = min(len(values) - 1, int(round((len(values) - 1) * 0.95)))
    return {
        "count": len(values),
        "min_ms": values[0],
        "p50_ms": values[len(values) // 2],
        "p95_ms": values[p95_index],
        "max_ms": values[-1],
    }


def summarize_relationship_matrix_cell(cell: dict[str, Any]) -> dict[str, Any]:
    run_id = cell["run_id"]
    artifact_path = nist_artifact_path(run_id)
    manifest = _read_json_if_exists(nist_manifest_path(run_id))
    report = _read_json_if_exists(nist_report_path(run_id))
    authority_eval = (
        (report or {}).get("authority_eval")
        if isinstance((report or {}).get("authority_eval"), dict)
        else summarize_authority_eval_pack_readiness(manifest)
    )
    authority_support = authority_support_validation_from_report(report)
    authority_report = summarize_authority_report(authority_eval, authority_support)
    rows = read_jsonl(artifact_path) if artifact_path.exists() else []
    relationship_queue = None
    if report:
        relationship_queues = [
            queue
            for queue in report.get("worker_backpressure", {}).get("queues", [])
            if str(queue.get("key") or "").lower() in RELATIONSHIP_QUEUE_KEYS
            or "relationship" in str(queue.get("label") or "").lower()
        ]
        relationship_queue = relationship_queues[0] if relationship_queues else None
    dogfood_gate = (report or {}).get("dogfood_gate") or {}
    return {
        **cell,
        "manifest_exists": manifest is not None,
        "artifact_exists": artifact_path.exists(),
        "report_exists": report is not None,
        "cleanup_plan_exists": nist_cleanup_plan_path(run_id).exists(),
        "actual_count": (manifest or {}).get("actual_count"),
        "accepted_entries": len(rows),
        "accept_latency": summarize_accept_latency(rows),
        "ready_tagged_items": (report or {}).get("ready_tagged_items"),
        "graph_relationships": (report or {}).get("graph_relationships"),
        "search_expected_hit_ratio": (report or {}).get("search_expected_hit_ratio"),
        "retrieve_expected_hit_ratio": (report or {}).get("retrieve_expected_hit_ratio"),
        "contrastive_eval_pack_count": (report or {}).get("contrastive_eval_pack_count"),
        "authority_eval": authority_eval,
        "authority_eval_pack_count": authority_eval.get("pack_count"),
        "authority_eval_ready_for_answer_support_validation": authority_eval.get(
            "ready_for_answer_support_validation"
        ),
        "authority_support_validation": authority_support,
        "authority_report": authority_report,
        "authority_support_validation_ready": authority_support.get("ready"),
        "authority_support_validation_passed_cases": authority_support.get("passed_cases"),
        "authority_support_validation_weak_support_cases": authority_support.get(
            "weak_support_cases"
        ),
        "dogfood_gate_passed": dogfood_gate.get("passed"),
        "dogfood_failures": dogfood_gate.get("failures", []),
        "relationship_queue": relationship_queue,
        "relationship_queue_drained": (
            relationship_queue is not None and not relationship_queue_needs_wait(relationship_queue)
        ),
        "relationship_recent_avg_latency_seconds": (
            relationship_queue_latency_seconds(relationship_queue) if relationship_queue else None
        ),
    }


def summarize_relationship_matrix(plan: dict[str, Any]) -> dict[str, Any]:
    cells = [summarize_relationship_matrix_cell(cell) for cell in plan["cells"]]
    completed = [cell for cell in cells if cell["report_exists"]]
    return {
        **plan,
        "generated_at": utc_now().isoformat(),
        "completed_cell_count": len(completed),
        "all_completed_cells_passed": bool(completed) and all(cell["dogfood_gate_passed"] for cell in completed),
        "cells": cells,
    }


def write_relationship_matrix_report(report: dict[str, Any]) -> Path:
    path = nist_matrix_report_path(report["matrix_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _read_json_with_warning(path: Path, warnings: list[str]) -> dict[str, Any] | None:
    if not path.exists():
        warnings.append(f"missing artifact: {path}")
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        warnings.append(f"invalid JSON artifact {path}: {exc.msg} at line {exc.lineno}")
        return None
    if not isinstance(data, dict):
        warnings.append(f"unexpected JSON artifact shape for {path}: expected object")
        return None
    return data


def _read_jsonl_with_warning(path: Path, warnings: list[str]) -> list[dict[str, Any]]:
    if not path.exists():
        warnings.append(f"missing artifact: {path}")
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                warnings.append(f"invalid JSONL artifact {path}:{line_number}: {exc.msg}")
                continue
            if isinstance(row, dict):
                rows.append(row)
            else:
                warnings.append(f"unexpected JSONL row shape for {path}:{line_number}: expected object")
    return rows


def _first_present(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if mapping.get(key) is not None:
            return mapping[key]
    return None


def _relationship_queue_from_report(report: dict[str, Any] | None) -> dict[str, Any] | None:
    for queue in (report or {}).get("worker_backpressure", {}).get("queues", []):
        if not isinstance(queue, dict):
            continue
        key = str(queue.get("key") or "").lower()
        label = str(queue.get("label") or "").lower()
        if key in RELATIONSHIP_QUEUE_KEYS or "relationship" in label:
            return queue
    return None


def _queue_is_drained(queue: dict[str, Any] | None) -> bool | None:
    if queue is None:
        return None
    return not relationship_queue_needs_wait(queue)


def _deployment_metadata(report: dict[str, Any] | None) -> dict[str, Any]:
    report = report or {}
    deployment = report.get("deployment") if isinstance(report.get("deployment"), dict) else {}
    chart = _first_present(
        {**report, **deployment},
        ("chart", "chart_version", "deployed_chart", "deployed_chart_version", "helm_chart_version"),
    )
    app_version = _first_present(
        {**report, **deployment},
        ("appVersion", "app_version", "deployed_app_version", "image_sha", "app_sha"),
    )
    return {"chart": chart, "appVersion": app_version}


def _retained_top_rank_failures_if_applicable(report: dict[str, Any] | None) -> list[str]:
    eval_reports = (report or {}).get("evals") or []
    if not isinstance(eval_reports, list):
        return []
    eval_ids = {row.get("id") for row in eval_reports if isinstance(row, dict)}
    if not eval_ids.intersection(RETAINED_NIST_TOP_RANK_REQUIREMENTS):
        return []
    return retained_nist_top_rank_failures([row for row in eval_reports if isinstance(row, dict)])


def summarize_nist_run_artifacts(run_id: str) -> dict[str, Any]:
    run_id = validate_run_id(run_id)
    warnings: list[str] = []
    manifest_path = nist_manifest_path(run_id)
    artifact_path = nist_artifact_path(run_id)
    report_path = nist_report_path(run_id)
    cleanup_path = nist_cleanup_plan_path(run_id)
    manifest = _read_json_with_warning(manifest_path, warnings)
    rows = _read_jsonl_with_warning(artifact_path, warnings)
    report = _read_json_with_warning(report_path, warnings)
    cleanup_plan = _read_json_with_warning(cleanup_path, warnings)
    authority_eval = summarize_authority_eval_pack_readiness(manifest, warnings=warnings)
    authority_support = authority_support_validation_from_report(report)
    authority_report = summarize_authority_report(authority_eval, authority_support)
    relationship_queue = _relationship_queue_from_report(report)
    dogfood_gate = (report or {}).get("dogfood_gate") or {}
    room_artifacts = dogfood_gate.get("room_artifacts") or {}
    wakeup_briefs = dogfood_gate.get("wakeup_briefs") or {}
    graph_relationships = (report or {}).get("graph_relationships") or {}
    cleanup_unsafe_ids = (cleanup_plan or {}).get("unsafe_item_ids") or []

    return {
        "run_id": run_id,
        "run_tag": run_tag(run_id),
        "paths": {
            "manifest": str(manifest_path),
            "artifact": str(artifact_path),
            "report": str(report_path),
            "cleanup_plan": str(cleanup_path),
        },
        "expected_count": (report or {}).get("expected_count") or (manifest or {}).get("actual_count"),
        "artifact_entry_count": len(rows),
        "tagged_items": (report or {}).get("tagged_items") or (cleanup_plan or {}).get("count"),
        "ready_tagged_items": (report or {}).get("ready_tagged_items"),
        "accept_latency": summarize_accept_latency(rows),
        "relationship_queue_drained": _queue_is_drained(relationship_queue),
        "relationship_queue": relationship_queue,
        "relationship_recent_avg_latency_seconds": (
            relationship_queue_latency_seconds(relationship_queue) if relationship_queue else None
        ),
        "graph_relationships": graph_relationships or None,
        "edges_touching_tagged_nodes": graph_relationships.get("edges_touching_tagged_nodes"),
        "orphaned_ready_items": graph_relationships.get("orphaned_ready_items"),
        "search_expected_hit_ratio": (report or {}).get("search_expected_hit_ratio"),
        "retrieve_expected_hit_ratio": (report or {}).get("retrieve_expected_hit_ratio"),
        "contrastive_eval_pack_count": (report or {}).get("contrastive_eval_pack_count"),
        "authority_eval": authority_eval,
        "authority_eval_pack_count": authority_eval["pack_count"],
        "authority_eval_case_ids": authority_eval["case_ids"],
        "authority_eval_ready_for_answer_support_validation": authority_eval[
            "ready_for_answer_support_validation"
        ],
        "authority_support_validation": authority_support,
        "authority_report": authority_report,
        "authority_support_validation_ready": authority_support["ready"],
        "authority_support_validation_case_count": authority_support["case_count"],
        "authority_support_validation_passed_cases": authority_support["passed_cases"],
        "authority_support_validation_weak_support_cases": authority_support["weak_support_cases"],
        "authority_support_validation_confusing_source_only_cases": authority_support[
            "confusing_source_only_cases"
        ],
        "top_rank_failures": _retained_top_rank_failures_if_applicable(report),
        "dogfood_gate_passed": dogfood_gate.get("passed"),
        "dogfood_failures": dogfood_gate.get("failures", []),
        "wakeup_briefs_stale": wakeup_briefs.get("stale"),
        "blocked_rooms": room_artifacts.get("blocked_rooms"),
        "cleanup_plan_exists": cleanup_path.exists(),
        "cleanup_plan_count": (cleanup_plan or {}).get("count"),
        "cleanup_plan_unsafe_item_count": len(cleanup_unsafe_ids) if isinstance(cleanup_unsafe_ids, list) else None,
        "cleanup_delete_confirmation": (cleanup_plan or {}).get("delete_confirmation"),
        "deployment": _deployment_metadata(report),
        "warnings": warnings,
    }


def compare_nist_run_artifacts(run_ids: list[str]) -> dict[str, Any]:
    summaries = [summarize_nist_run_artifacts(run_id) for run_id in run_ids]
    return {
        "benchmark": "nist-sp800-corpus-artifact-comparison",
        "generated_at": utc_now().isoformat(),
        "run_count": len(summaries),
        "warning_count": sum(len(summary["warnings"]) for summary in summaries),
        "runs": summaries,
    }


def _markdown_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value).replace("|", "\\|").replace("\n", " ")


def format_nist_artifact_comparison_markdown(comparison: dict[str, Any]) -> str:
    headers = [
        "run_id",
        "expected",
        "accepted",
        "ready",
        "rel_queue",
        "rel_edges",
        "rel_latency_s",
        "search_hit",
        "retrieve_hit",
        "auth_packs",
        "auth_ready",
        "auth_support",
        "auth_weak",
        "auth_top1",
        "auth_demoted",
        "auth_provenance_missing",
        "top_rank_failures",
        "wakeup_stale",
        "blocked_rooms",
        "cleanup",
        "deployment",
        "warnings",
    ]
    lines = [
        "# NIST Benchmark Artifact Comparison",
        "",
        f"Generated at: `{comparison['generated_at']}`",
        "",
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for run in comparison["runs"]:
        deployment = run["deployment"]
        deployment_label = " / ".join(
            str(value)
            for value in (deployment.get("chart"), deployment.get("appVersion"))
            if value is not None
        )
        cleanup_label = "missing"
        if run["cleanup_plan_exists"]:
            cleanup_label = f"{run['cleanup_plan_count']} items"
            if run["cleanup_plan_unsafe_item_count"]:
                cleanup_label += f", {run['cleanup_plan_unsafe_item_count']} unsafe"
        authority_report = run["authority_report"]
        row = [
            run["run_id"],
            run["expected_count"],
            run["artifact_entry_count"],
            run["ready_tagged_items"],
            run["relationship_queue_drained"],
            run["edges_touching_tagged_nodes"],
            run["relationship_recent_avg_latency_seconds"],
            run["search_expected_hit_ratio"],
            run["retrieve_expected_hit_ratio"],
            run["authority_eval_pack_count"],
            run["authority_eval_ready_for_answer_support_validation"],
            (
                f"{run['authority_support_validation_passed_cases']}/"
                f"{run['authority_support_validation_case_count']}"
            ),
            run["authority_support_validation_weak_support_cases"],
            authority_report["governing_source"]["top_rank_cases"],
            authority_report["adjacent_source_demotion"]["demoted_cases"],
            authority_report["provenance"]["missing_provenance_cases"],
            len(run["top_rank_failures"]),
            run["wakeup_briefs_stale"],
            run["blocked_rooms"],
            cleanup_label,
            deployment_label or "-",
            len(run["warnings"]),
        ]
        lines.append("| " + " | ".join(_markdown_value(value) for value in row) + " |")
    warnings = [
        f"- `{run['run_id']}`: {warning}"
        for run in comparison["runs"]
        for warning in run["warnings"]
    ]
    if warnings:
        lines.extend(["", "## Warnings", *warnings])
    return "\n".join(lines) + "\n"


def _gate_result(name: str, passed: bool | None, detail: str, *, source: str) -> dict[str, Any]:
    status = "unknown" if passed is None else ("pass" if passed else "fail")
    return {"name": name, "status": status, "detail": detail, "source": source}


def _ratio_gate(value: Any, threshold: float, *, label: str, source: str) -> dict[str, Any]:
    if value is None:
        return _gate_result(label, None, "missing ratio", source=source)
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return _gate_result(label, None, f"invalid ratio {value!r}", source=source)
    return _gate_result(
        label,
        ratio >= threshold,
        f"{ratio:.3f} >= {threshold:.3f}",
        source=source,
    )


def _blocking_gate_passed(gate: dict[str, Any]) -> bool:
    return gate["status"] == "pass"


def _advisory_gate_ready(gate: dict[str, Any]) -> bool:
    return gate["status"] == "pass"


def _database_health_from_report(report: dict[str, Any] | None) -> dict[str, Any] | None:
    if report is None:
        return None
    if report.get("mode") and isinstance(report.get("checks"), list):
        return report
    database_health = report.get("database_health")
    return database_health if isinstance(database_health, dict) else None


def _database_health_gate(report: dict[str, Any] | None, *, source: str) -> dict[str, Any]:
    database_health = _database_health_from_report(report)
    if database_health is None:
        return _gate_result("database health", None, "missing database health report", source=source)
    ok = database_health.get("ok")
    checks = database_health.get("checks") if isinstance(database_health.get("checks"), list) else []
    failed = [
        str(check.get("name"))
        for check in checks
        if isinstance(check, dict) and check.get("status") not in {"pass", "ok"}
    ]
    if ok is None:
        ok = not failed if checks else None
    detail = "all checks passed" if ok else "failed checks: " + ", ".join(failed or ["unknown"])
    return _gate_result("database health", bool(ok) if ok is not None else None, detail, source=source)


def _retrieval_replay_gate(report: dict[str, Any] | None, *, source: str) -> dict[str, Any]:
    if report is None:
        return _gate_result("retrieval replay stability", None, "missing replay report", source=source)
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    failures = summary.get("failure_counts") if isinstance(summary.get("failure_counts"), dict) else {}
    matched = summary.get("matched_records")
    detail = f"matched_records={matched}; failures={failures}"
    return _gate_result("retrieval replay stability", not failures, detail, source=source)


def _authority_advisory_gate(run: dict[str, Any]) -> dict[str, Any]:
    report = run["authority_report"]
    ready = bool(report.get("ready"))
    case_count = report.get("case_count")
    weak = (report.get("weak_support") or {}).get("cases")
    return _gate_result(
        "authority support advisory",
        ready,
        f"cases={case_count}; weak_support_cases={weak}",
        source=run["run_id"],
    )


def durable_memory_run_matrix_cell(
    run: dict[str, Any],
    *,
    min_expected_hit_ratio: float,
    database_health_report: dict[str, Any] | None,
    database_health_source: str,
) -> dict[str, Any]:
    expected = run.get("expected_count")
    accepted = run.get("artifact_entry_count")
    ready = run.get("ready_tagged_items")
    fixed_truth_passed = (
        expected is not None
        and accepted is not None
        and ready is not None
        and int(accepted) >= int(expected)
        and int(ready) >= int(expected)
    )
    blocking_gates = [
        _gate_result(
            "fixed NIST truth set",
            fixed_truth_passed,
            f"expected={expected}; accepted={accepted}; ready={ready}",
            source=run["run_id"],
        ),
        _ratio_gate(
            run.get("search_expected_hit_ratio"),
            min_expected_hit_ratio,
            label="search fixed-truth expected-hit ratio",
            source=run["run_id"],
        ),
        _ratio_gate(
            run.get("retrieve_expected_hit_ratio"),
            min_expected_hit_ratio,
            label="memory retrieve fixed-truth expected-hit ratio",
            source=run["run_id"],
        ),
        _gate_result(
            "retained NIST top-rank requirements",
            not run.get("top_rank_failures"),
            f"failures={run.get('top_rank_failures', [])}",
            source=run["run_id"],
        ),
        _gate_result(
            "relationship queue drain",
            run.get("relationship_queue_drained"),
            f"queue={run.get('relationship_queue')}",
            source=run["run_id"],
        ),
        _gate_result(
            "room artifacts",
            not run.get("blocked_rooms"),
            f"blocked_rooms={run.get('blocked_rooms')}",
            source=run["run_id"],
        ),
        _gate_result(
            "wake-up freshness",
            run.get("wakeup_briefs_stale") in (False, 0),
            f"wakeup_briefs_stale={run.get('wakeup_briefs_stale')}",
            source=run["run_id"],
        ),
        _gate_result(
            "dogfood gate",
            run.get("dogfood_gate_passed"),
            f"failures={run.get('dogfood_failures', [])}",
            source=run["run_id"],
        ),
        _database_health_gate(database_health_report, source=database_health_source),
    ]
    advisory_gates = [_authority_advisory_gate(run)]
    return {
        "run_id": run["run_id"],
        "run_tag": run["run_tag"],
        "paths": run["paths"],
        "deployment": run["deployment"],
        "blocking_gates": blocking_gates,
        "advisory_gates": advisory_gates,
        "blocking_passed": all(_blocking_gate_passed(gate) for gate in blocking_gates),
        "advisory_ready": all(_advisory_gate_ready(gate) for gate in advisory_gates),
        "warnings": run["warnings"],
    }


def _load_json_artifact(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    artifact_path = Path(path)
    if not artifact_path.exists():
        raise SystemExit(f"missing JSON artifact: {artifact_path}")
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"unexpected JSON artifact shape for {artifact_path}: expected object")
    return payload


def _run_static_database_health_json() -> dict[str, Any] | None:
    command = [
        sys.executable,
        str(Path(__file__).resolve().parent / "check_database_health.py"),
        "--format",
        "json",
    ]
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "mode": "static",
            "ok": False,
            "checks": [{"name": "database health helper", "status": "fail", "detail": str(exc)}],
        }
    if completed.stdout.strip():
        try:
            report = json.loads(completed.stdout)
        except json.JSONDecodeError:
            report = None
        if isinstance(report, dict):
            return report
    return {
        "mode": "static",
        "ok": False,
        "checks": [
            {
                "name": "database health helper",
                "status": "fail",
                "detail": (completed.stderr or completed.stdout or "no output").strip(),
            }
        ],
    }


def build_durable_memory_matrix_report(
    *,
    matrix_id: str,
    run_ids: list[str],
    min_expected_hit_ratio: float,
    retrieval_replay_report: dict[str, Any] | None,
    database_health_report: dict[str, Any] | None,
    database_health_source: str,
) -> dict[str, Any]:
    runs = [
        durable_memory_run_matrix_cell(
            summarize_nist_run_artifacts(run_id),
            min_expected_hit_ratio=min_expected_hit_ratio,
            database_health_report=database_health_report,
            database_health_source=database_health_source,
        )
        for run_id in run_ids
    ]
    replay_gate = _retrieval_replay_gate(retrieval_replay_report, source="retrieval replay report")
    advisory_gates: list[dict[str, Any]] = []
    blocking_failures = [
        {"run_id": run["run_id"], "gate": gate}
        for run in runs
        for gate in run["blocking_gates"]
        if gate["status"] != "pass"
    ]
    if replay_gate["status"] != "pass":
        blocking_failures.insert(0, {"run_id": None, "gate": replay_gate})
    advisory_failures = [
        {"run_id": run["run_id"], "gate": gate}
        for run in runs
        for gate in run["advisory_gates"]
        if gate["status"] != "pass"
    ] + [
        {"run_id": None, "gate": gate}
        for gate in advisory_gates
        if gate["status"] != "pass"
    ]
    return {
        "benchmark": "nist-sp800-durable-memory-matrix",
        "matrix_id": matrix_id,
        "generated_at": utc_now().isoformat(),
        "run_ids": run_ids,
        "min_expected_hit_ratio": min_expected_hit_ratio,
        "blocking_passed": not blocking_failures,
        "advisory_ready": not advisory_failures,
        "blocking_failure_count": len(blocking_failures),
        "advisory_failure_count": len(advisory_failures),
        "blocking_failures": blocking_failures,
        "advisory_failures": advisory_failures,
        "advisory_gates": advisory_gates,
        "blocking_gates": [replay_gate],
        "database_health": database_health_report,
        "retrieval_replay": retrieval_replay_report,
        "runs": runs,
    }


def write_durable_memory_matrix_report(report: dict[str, Any]) -> Path:
    path = nist_durable_matrix_report_path(report["matrix_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def format_durable_memory_matrix_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# NIST Durable Memory Matrix",
        "",
        f"Generated at: `{report['generated_at']}`",
        f"Blocking passed: `{report['blocking_passed']}`",
        f"Advisory ready: `{report['advisory_ready']}`",
        "",
        "| run_id | blocking | advisory | failed blocking gates | advisory gaps |",
        "| --- | --- | --- | --- | --- |",
    ]
    for run in report["runs"]:
        failed_blocking = [
            gate["name"] for gate in run["blocking_gates"] if gate["status"] != "pass"
        ]
        advisory_gaps = [
            gate["name"] for gate in run["advisory_gates"] if gate["status"] != "pass"
        ]
        lines.append(
            "| "
            + " | ".join(
                _markdown_value(value)
                for value in (
                    run["run_id"],
                    run["blocking_passed"],
                    run["advisory_ready"],
                    ", ".join(failed_blocking) or "-",
                    ", ".join(advisory_gaps) or "-",
                )
            )
            + " |"
        )
    global_advisory_gaps = [
        gate["name"] for gate in report["advisory_gates"] if gate["status"] != "pass"
    ]
    if global_advisory_gaps:
        lines.extend(["", "## Advisory Gaps", *[f"- {gap}" for gap in global_advisory_gaps]])
    if report["blocking_failures"]:
        lines.append("")
        lines.append("## Blocking Failures")
        lines.extend(
            f"- `{failure['run_id']}` {failure['gate']['name']}: {failure['gate']['detail']}"
            for failure in report["blocking_failures"]
        )
    return "\n".join(lines) + "\n"


def cmd_compare(args: argparse.Namespace) -> int:
    comparison = compare_nist_run_artifacts(args.run_ids)
    if args.format == "json":
        output = json.dumps(comparison, indent=2, sort_keys=True) + "\n"
    else:
        output = format_nist_artifact_comparison_markdown(comparison)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output, encoding="utf-8")
        print(f"wrote {path}")
    else:
        print(output, end="")
    return 0


def cmd_durable_matrix(args: argparse.Namespace) -> int:
    matrix_id = validate_base_run_id(args.matrix_id or default_matrix_id())
    retrieval_replay_report = _load_json_artifact(args.retrieval_replay_report)
    if args.database_health_report:
        database_health_report = _load_json_artifact(args.database_health_report)
        database_health_source = args.database_health_report
    elif args.skip_static_database_health:
        database_health_report = None
        database_health_source = "not supplied"
    else:
        database_health_report = _run_static_database_health_json()
        database_health_source = "static database health"

    report = build_durable_memory_matrix_report(
        matrix_id=matrix_id,
        run_ids=args.run_ids,
        min_expected_hit_ratio=args.min_expected_hit_ratio,
        retrieval_replay_report=retrieval_replay_report,
        database_health_report=database_health_report,
        database_health_source=database_health_source,
    )
    path = write_durable_memory_matrix_report(report)
    if args.format == "json":
        output = json.dumps(report, indent=2, sort_keys=True) + "\n"
    else:
        output = format_durable_memory_matrix_markdown(report)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output, encoding="utf-8")
        print(f"wrote {output_path}")
    else:
        print(output, end="")
    print(f"wrote {path}")
    return 0 if report["blocking_passed"] else 1


def cmd_verify(args: argparse.Namespace) -> int:
    client = client_from_args(args)
    run_id = validate_run_id(args.run_id)
    tag = run_tag(run_id)
    manifest = read_manifest(run_id)
    authority_eval = summarize_authority_eval_pack_readiness(manifest)
    expect_count = args.expect_count or int(manifest["actual_count"])
    items = list_tagged_items(client, tag)
    stats = client_request(client, "GET", "/api/v1/stats")
    palace = client_request(client, "GET", "/api/v1/palace")
    control_tower = client_request(client, "GET", "/api/v1/palace/control-tower")
    graph = client_request(client, "GET", "/api/v1/graph", timeout=90)

    exact_query = f"NIST-CORPUS-{run_id}-0000"
    exact_body = {
        "query": exact_query,
        "limit": 10,
        "tags": [tag, "nist-sp800"],
        "tags_mode": "all",
    }
    exact_search = client_request(client, "POST", "/api/v1/search", body=exact_body, timeout=90)
    exact_retrieve = client_request(
        client,
        "POST",
        "/api/v1/memory/retrieve",
        body={**exact_body, "scope": {"type": "tenant_shared", "key": None}},
        timeout=90,
    )
    eval_reports = run_eval_queries(client, run_id, manifest, limit=args.eval_limit)
    contrastive_pack_reports = run_contrastive_eval_packs(client, run_id, manifest, limit=args.eval_limit)
    authority_support_validation = run_authority_support_validation(
        client,
        run_id,
        manifest,
        limit=args.eval_limit,
    )
    authority_report = summarize_authority_report(authority_eval, authority_support_validation)
    search_expected_hits = sum(1 for report in eval_reports if report["search_expected_hit"])
    retrieve_expected_hits = sum(1 for report in eval_reports if report["retrieve_expected_hit"])
    eval_count = len(eval_reports) or 1
    worker_backpressure = control_tower.get("worker_backpressure") or {}
    dogfood_gate = build_dogfood_gate_report(
        palace=palace,
        control_tower=control_tower,
        retrieval_checks=[
            {
                "name": "NIST exact retrieval",
                "total": exact_retrieve.get("total", 0),
                "trace": exact_retrieve.get("trace"),
                "results": exact_retrieve.get("results", []),
                "required_tags": [tag, "nist-sp800"],
            },
            *[
                {
                    "name": f"NIST eval retrieval: {report['id']}",
                    "total": report["retrieve_total"],
                    "trace": report["retrieve_trace"],
                    "results": [],
                    "required_tags": [],
                    "expected_hit": report["retrieve_expected_hit"],
                }
                for report in eval_reports
            ],
        ],
        hit_ratios={
            "search_expected_hit": search_expected_hits / eval_count,
            "retrieve_expected_hit": retrieve_expected_hits / eval_count,
        },
        min_hit_ratio=args.min_expected_hit_ratio,
    )

    report = {
        "benchmark": "nist-sp800-corpus",
        "run_id": run_id,
        "run_tag": tag,
        "generated_at": utc_now().isoformat(),
        "tagged_items": len(items),
        "ready_tagged_items": sum(1 for item in items if item.get("status") == "ready"),
        "expected_count": expect_count,
        "stats": stats,
        "palace_generations": {
            "dirty_generation": palace.get("dirty_generation"),
            "indexed_generation": palace.get("indexed_generation"),
            "backlog_generation": palace.get("backlog_generation"),
            "active_palace_run": palace.get("active_palace_run"),
        },
        "worker_backpressure": {
            "generated_at": worker_backpressure.get("generated_at"),
            "queues": [
                {
                    "key": queue.get("key"),
                    "label": queue.get("label"),
                    "queued_depth": queue.get("queued_depth"),
                    "deferred_depth": queue.get("deferred_depth"),
                    "oldest_queued_age_seconds": queue.get("oldest_queued_age_seconds"),
                    "worker_concurrency": queue.get("worker_concurrency"),
                    "worker_queue_depth": queue.get("worker_queue_depth"),
                    "recent_avg_latency_seconds": queue.get("recent_avg_latency_seconds"),
                    "recent_failed": queue.get("recent_failed"),
                    "telemetry_error": queue.get("telemetry_error"),
                }
                for queue in worker_backpressure.get("queues", [])
            ],
        },
        "graph_relationships": summarize_graph_relationships(graph, tag),
        "exact_search_total": exact_search.get("total", 0),
        "exact_search_titles": [row.get("title") for row in exact_search.get("results", [])[:5]],
        "exact_retrieve_total": exact_retrieve.get("total", 0),
        "exact_retrieve_titles": [row.get("title") for row in exact_retrieve.get("results", [])[:5]],
        "eval_count": len(eval_reports),
        "search_expected_hits": search_expected_hits,
        "search_expected_hit_ratio": search_expected_hits / eval_count,
        "retrieve_expected_hits": retrieve_expected_hits,
        "retrieve_expected_hit_ratio": retrieve_expected_hits / eval_count,
        "dogfood_gate": dogfood_gate,
        "evals": eval_reports,
        "contrastive_eval_pack_count": len(contrastive_pack_reports),
        "contrastive_eval_packs": contrastive_pack_reports,
        "authority_eval": authority_eval,
        "authority_eval_pack_count": authority_eval["pack_count"],
        "authority_eval_case_ids": authority_eval["case_ids"],
        "authority_eval_ready_for_answer_support_validation": authority_eval[
            "ready_for_answer_support_validation"
        ],
        "authority_support_validation": authority_support_validation,
        "authority_report": authority_report,
        "authority_support_validation_ready": authority_support_validation["ready"],
        "authority_support_validation_case_count": authority_support_validation["case_count"],
        "authority_support_validation_passed_cases": authority_support_validation["passed_cases"],
        "authority_support_validation_weak_support_cases": authority_support_validation[
            "weak_support_cases"
        ],
        "authority_support_validation_confusing_source_only_cases": authority_support_validation[
            "confusing_source_only_cases"
        ],
        "frontend_url": args.frontend_url,
    }
    report_path = nist_report_path(run_id)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    print(f"wrote {report_path}")

    failures: list[str] = []
    if len(items) < expect_count:
        failures.append(f"expected at least {expect_count} tagged items, found {len(items)}")
    if report["ready_tagged_items"] < expect_count:
        failures.append(f"expected at least {expect_count} ready tagged items, found {report['ready_tagged_items']}")
    if exact_search.get("total", 0) < 1 or exact_retrieve.get("total", 0) < 1:
        failures.append("expected exact sentinel search and retrieval to return at least one result")
    if report["search_expected_hit_ratio"] < args.min_expected_hit_ratio:
        failures.append(
            f"search expected-hit ratio {report['search_expected_hit_ratio']:.2f} "
            f"is below {args.min_expected_hit_ratio:.2f}"
        )
    if report["retrieve_expected_hit_ratio"] < args.min_expected_hit_ratio:
        failures.append(
            f"retrieve expected-hit ratio {report['retrieve_expected_hit_ratio']:.2f} "
            f"is below {args.min_expected_hit_ratio:.2f}"
        )
    if args.require_retained_nist_top_ranks:
        failures.extend(retained_nist_top_rank_failures(eval_reports))
    failures.extend(dogfood_gate["failures"])

    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1
    return 0


def cmd_append_eval_packs(args: argparse.Namespace) -> int:
    run_id = validate_run_id(args.run_id)
    path = nist_manifest_path(run_id)
    manifest = read_manifest(run_id)
    packs = build_contrastive_eval_packs(
        list(manifest.get("publications", [])),
        manifest_eval_queries(manifest),
    )
    manifest["contrastive_eval_packs"] = packs
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "run_id": run_id,
                "manifest": str(path),
                "contrastive_eval_pack_count": len(packs),
                "contrastive_eval_probe_count": sum(len(pack.get("probes", [])) for pack in packs),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def cmd_append_authority_eval_packs(args: argparse.Namespace) -> int:
    run_id = validate_run_id(args.run_id)
    path = nist_manifest_path(run_id)
    manifest = read_manifest(run_id)
    packs = build_authority_eval_packs(manifest)
    manifest["authority_eval_packs"] = packs
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "run_id": run_id,
                "manifest": str(path),
                "authority_eval_pack_count": len(packs),
                "authority_eval_case_ids": [pack["id"] for pack in packs],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def cmd_cleanup_plan(args: argparse.Namespace) -> int:
    client = client_from_args(args)
    run_id = validate_run_id(args.run_id)
    tag = run_tag(run_id)
    items = list_tagged_items(client, tag)
    unsafe = [
        item["id"]
        for item in items
        if tag not in item.get("tags", []) or "benchmark-cleanup-ok" not in item.get("tags", [])
    ]
    plan = {
        "run_id": run_id,
        "run_tag": tag,
        "generated_at": utc_now().isoformat(),
        "count": len(items),
        "unsafe_item_ids": unsafe,
        "items": [
            {
                "id": item["id"],
                "title": item.get("title"),
                "status": item.get("status"),
                "tags": item.get("tags", []),
                "created_at": item.get("created_at"),
            }
            for item in items
        ],
        "delete_confirmation": f"BENCHMARK-RUN-{run_id}",
    }
    plan_path = nist_cleanup_plan_path(run_id)
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: plan[k] for k in ("run_id", "run_tag", "count", "delete_confirmation")}, indent=2))
    print(f"wrote {plan_path}")
    return 1 if unsafe else 0


def cmd_cleanup_delete(args: argparse.Namespace) -> int:
    client = client_from_args(args)
    run_id = validate_run_id(args.run_id)
    expected = f"BENCHMARK-RUN-{run_id}"
    if args.confirm_delete != expected:
        raise SystemExit(f"refusing delete; pass --confirm-delete {expected}")
    tag = run_tag(run_id)
    items = list_tagged_items(client, tag)
    if not items:
        print("no tagged items found")
        return 0
    unsafe = [
        item for item in items
        if tag not in item.get("tags", []) or "benchmark-cleanup-ok" not in item.get("tags", [])
    ]
    if unsafe:
        raise SystemExit(f"refusing delete; {len(unsafe)} items are missing expected benchmark cleanup tags")

    print(f"deleting {len(items)} NIST corpus benchmark items tagged {tag}")
    if args.dry_run:
        print("dry run only; no items removed")
        return 0
    deleted = delete_benchmark_items(
        client,
        items,
        method=args.method,
        batch_size=args.batch_size,
    )
    if deleted != len(items):
        raise SystemExit(f"removed {deleted}/{len(items)} items before cleanup stopped")
    print("cleanup remove complete; run a manual Palace rebuild if room state should refresh immediately")
    return 0


def _connection_args(args: argparse.Namespace, **overrides: Any) -> argparse.Namespace:
    payload = vars(args).copy()
    payload.update(overrides)
    return argparse.Namespace(**payload)


def cmd_run(args: argparse.Namespace) -> int:
    if "hermes" in args.api_base_url.lower() or "hermes" in args.frontend_url.lower():
        raise SystemExit("refusing to benchmark a Hermes host; use the standalone Palace of Truth staging host")

    client = client_from_args(args)
    args.run_id = validate_run_id(args.run_id or default_run_id())
    tenant_id = resolve_tenant_id(client, args.tenant_id)
    print(
        json.dumps(
            {
                "benchmark": "nist-sp800-corpus",
                "api_base_url": args.api_base_url,
                "frontend_url": args.frontend_url,
                "tenant_id": tenant_id,
                "run_id": args.run_id,
                "run_tag": run_tag(args.run_id),
                "target_count": args.target_count,
                "relationship_policy": args.relationship_policy,
            },
            indent=2,
            sort_keys=True,
        )
    )

    if args.dry_run:
        return cmd_ingest(_connection_args(args, tenant_id=tenant_id, dry_run=True))

    print("\n[1/7] prepare NIST SP 800 corpus")
    corpus = load_or_build_corpus(args)
    if len(corpus) < args.target_count:
        raise SystemExit(f"prepared {len(corpus)} chunks but target is {args.target_count}")

    print("\n[2/7] ingest corpus memories")
    rc = cmd_ingest(_connection_args(args, tenant_id=tenant_id, dry_run=False))
    if rc != 0:
        return rc

    print("\n[3/7] wait for embedding jobs")
    rc = cmd_wait(
        _connection_args(
            args,
            interval_seconds=args.job_interval_seconds,
            timeout_seconds=args.job_timeout_seconds,
            allow_failures=False,
        )
    )
    if rc != 0:
        return rc

    print("\n[4/7] trigger Palace rebuild")
    run = client_request(client, "POST", "/api/v1/palace/runs", timeout=60)
    print(json.dumps(run, indent=2, sort_keys=True))
    if not args.no_palace_wait:
        rc = wait_for_palace_fresh(
            client,
            timeout_seconds=args.palace_timeout_seconds,
            interval_seconds=args.palace_interval_seconds,
        )
        if rc != 0:
            if not args.allow_palace_timeout:
                return rc
            print("continuing after Palace timeout because --allow-palace-timeout was set", file=sys.stderr)

    if not args.no_relationship_queue_wait:
        print("\n[5/7] wait for relationship queue drain")
        rc = wait_for_relationship_queue_drained(
            client,
            timeout_seconds=args.relationship_queue_timeout_seconds,
            interval_seconds=args.relationship_queue_interval_seconds,
        )
        if rc != 0:
            return rc
    else:
        print("\n[5/7] skip relationship queue drain wait")

    print("\n[6/7] verify exact, semantic, and Palace-scoped retrieval")
    rc = cmd_verify(_connection_args(args, expect_count=args.target_count))
    if rc != 0:
        return rc

    print("\n[7/7] write cleanup review plan")
    rc = cmd_cleanup_plan(args)
    if rc != 0:
        return rc

    print(
        "\nDONE. Cleanup is not automatic. Review the cleanup plan, then run:\n"
        f"python3 scripts/benchmark_nist_sp800_staging.py cleanup-delete --run-id {args.run_id} "
        f"--confirm-delete BENCHMARK-RUN-{args.run_id} --dry-run\n"
        "Remove --dry-run only after the item list is exactly what you intend to remove."
    )
    return 0


def cmd_matrix(args: argparse.Namespace) -> int:
    matrix_id = validate_base_run_id(args.matrix_id or default_matrix_id())
    plan = build_relationship_matrix_plan(
        matrix_id=matrix_id,
        target_counts=args.target_counts,
        relationship_policies=args.relationship_policies,
        chunks_per_document=args.chunks_per_document,
        chunk_chars=args.chunk_chars,
        overlap_chars=args.overlap_chars,
        source_document_candidates=args.source_document_candidates,
        enable_ai_enrichment=args.enable_ai_enrichment,
        eval_limit=args.eval_limit,
        min_expected_hit_ratio=args.min_expected_hit_ratio,
        relationship_queue_timeout_seconds=args.relationship_queue_timeout_seconds,
    )

    if args.dry_run or args.report_only:
        report = summarize_relationship_matrix(plan)
        path = write_relationship_matrix_report(report)
        print(json.dumps(report, indent=2, sort_keys=True))
        print(f"wrote {path}")
        return 0

    for cell in plan["cells"]:
        print(
            "\n"
            f"=== NIST relationship matrix cell run_id={cell['run_id']} "
            f"target_count={cell['target_count']} relationship_policy={cell['relationship_policy']} ==="
        )
        rc = cmd_run(
            _connection_args(
                args,
                run_id=cell["run_id"],
                target_count=cell["target_count"],
                relationship_policy=cell["relationship_policy"],
                dry_run=False,
            )
        )
        if rc != 0:
            report = summarize_relationship_matrix(plan)
            path = write_relationship_matrix_report(report)
            print(f"matrix cell failed; wrote partial matrix report to {path}", file=sys.stderr)
            return rc

    report = summarize_relationship_matrix(plan)
    path = write_relationship_matrix_report(report)
    print(json.dumps(report, indent=2, sort_keys=True))
    print(f"wrote {path}")
    return 0


def add_corpus_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--target-count", type=int, default=250)
    parser.add_argument("--chunks-per-document", type=int, default=10)
    parser.add_argument("--chunk-chars", type=int, default=3600)
    parser.add_argument("--overlap-chars", type=int, default=250)
    parser.add_argument("--source-document-candidates", type=int, default=35)
    parser.add_argument("--source-timeout", type=float, default=90.0)
    parser.add_argument("--relationship-policy", choices=["immediate", "deferred", "skip"], default="deferred")
    parser.add_argument("--enable-ai-enrichment", action="store_true")
    parser.add_argument("--rebuild-corpus", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-base-url",
        default=os.getenv("PALACEOFTRUTH_API_BASE_URL", DEFAULT_API_BASE_URL),
    )
    parser.add_argument(
        "--frontend-url",
        default=os.getenv("PALACEOFTRUTH_FRONTEND_URL", DEFAULT_FRONTEND_URL),
    )
    parser.add_argument("--api-key", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="Download/extract NIST PDFs and write the corpus manifest.")
    add_corpus_args(prepare)
    prepare.set_defaults(func=cmd_prepare)

    ingest = sub.add_parser("ingest", help="Create NIST corpus benchmark memory entries.")
    add_corpus_args(ingest)
    ingest.add_argument("--tenant-id", default=None)
    ingest.add_argument("--concurrency", type=int, default=4)
    ingest.add_argument("--request-timeout", type=float, default=90.0)
    ingest.add_argument("--progress-every", type=int, default=25)
    ingest.add_argument("--resume", action="store_true")
    ingest.add_argument("--dry-run", action="store_true")
    ingest.add_argument("--keep-going", action="store_true")
    ingest.set_defaults(func=cmd_ingest)

    wait = sub.add_parser("wait", help="Poll NIST corpus memory jobs from the local run artifact.")
    wait.add_argument("--run-id", required=True)
    wait.add_argument("--interval-seconds", type=int, default=30)
    wait.add_argument("--timeout-seconds", type=int, default=7200)
    wait.add_argument("--allow-failures", action="store_true")
    wait.set_defaults(func=cmd_wait)

    verify = sub.add_parser("verify", help="Verify tagged items, stats, exact search, and eval queries.")
    verify.add_argument("--run-id", required=True)
    verify.add_argument("--expect-count", type=int, default=None)
    verify.add_argument("--eval-limit", type=int, default=10)
    verify.add_argument("--min-expected-hit-ratio", type=float, default=0.50)
    verify.add_argument("--require-retained-nist-top-ranks", action="store_true")
    verify.set_defaults(func=cmd_verify)

    append_eval_packs = sub.add_parser(
        "append-eval-packs",
        help="Append deterministic contrastive eval packs to an existing local corpus manifest.",
    )
    append_eval_packs.add_argument("--run-id", required=True)
    append_eval_packs.set_defaults(func=cmd_append_eval_packs)

    append_authority_eval_packs = sub.add_parser(
        "append-authority-eval-packs",
        help="Append local-only authority grounding eval packs to an existing local corpus manifest.",
    )
    append_authority_eval_packs.add_argument("--run-id", required=True)
    append_authority_eval_packs.set_defaults(func=cmd_append_authority_eval_packs)

    prepare_hf_authority = sub.add_parser(
        "prepare-hf-authority-corpus",
        help=(
            "Build an offline authority-eval manifest from local "
            "ethanolivertroy/nist-cybersecurity-training rows without embedding API calls."
        ),
    )
    prepare_hf_authority.add_argument("--run-id", required=True)
    prepare_hf_authority.add_argument(
        "--input",
        action="append",
        required=True,
        help="Local dataset file as [split=]path. Supports .jsonl, .json, and .parquet.",
    )
    prepare_hf_authority.add_argument(
        "--sample-limit",
        type=int,
        default=None,
        help="Optional per-input row limit for fixture/smoke manifests.",
    )
    prepare_hf_authority.add_argument("--output", default=None, help="Optional manifest output path.")
    prepare_hf_authority.set_defaults(func=cmd_prepare_hf_authority_corpus)

    report_hf_authority = sub.add_parser(
        "report-hf-authority-corpus",
        help="Validate an offline HF authority manifest and write a report without API calls.",
    )
    report_hf_authority.add_argument("--manifest", required=True, help="Path to a NIST HF corpus manifest.")
    report_hf_authority.add_argument("--output", default=None, help="Optional report output path.")
    report_hf_authority.set_defaults(func=cmd_report_hf_authority_corpus)

    compare = sub.add_parser(
        "compare",
        help="Compare existing local NIST benchmark artifacts without API calls.",
    )
    compare.add_argument("--run-ids", nargs="+", required=True)
    compare.add_argument("--format", choices=["markdown", "json"], default="markdown")
    compare.add_argument("--output", default=None, help="Optional output path for the comparison report.")
    compare.set_defaults(func=cmd_compare)

    durable_matrix = sub.add_parser(
        "durable-matrix",
        help=(
            "Build a report-only durable-memory benchmark matrix from existing NIST, "
            "retrieval replay, and database health artifacts."
        ),
    )
    durable_matrix.add_argument("--matrix-id", default=None)
    durable_matrix.add_argument("--run-ids", nargs="+", required=True)
    durable_matrix.add_argument("--min-expected-hit-ratio", type=float, default=0.50)
    durable_matrix.add_argument(
        "--retrieval-replay-report",
        default=None,
        help="Optional JSON report from scripts/replay_retrieval_capture.py gate/compare.",
    )
    durable_matrix.add_argument(
        "--database-health-report",
        default=None,
        help="Optional JSON report from scripts/check_database_health.py --format json.",
    )
    durable_matrix.add_argument(
        "--skip-static-database-health",
        action="store_true",
        help="Do not run static database health when --database-health-report is omitted.",
    )
    durable_matrix.add_argument("--format", choices=["markdown", "json"], default="markdown")
    durable_matrix.add_argument("--output", default=None, help="Optional output path for rendered report.")
    durable_matrix.set_defaults(func=cmd_durable_matrix)

    cleanup_plan = sub.add_parser("cleanup-plan", help="Write a deletion review file for tagged NIST items.")
    cleanup_plan.add_argument("--run-id", required=True)
    cleanup_plan.set_defaults(func=cmd_cleanup_plan)

    cleanup_delete = sub.add_parser("cleanup-delete", help="Remove tagged NIST items after human review.")
    cleanup_delete.add_argument("--run-id", required=True)
    cleanup_delete.add_argument("--confirm-delete", required=True)
    cleanup_delete.add_argument("--batch-size", type=int, default=100)
    cleanup_delete.add_argument("--method", choices=["auto", "batch", "individual"], default="auto")
    cleanup_delete.add_argument("--dry-run", action="store_true")
    cleanup_delete.set_defaults(func=cmd_cleanup_delete)

    run = sub.add_parser("run", help="One-command NIST corpus benchmark.")
    add_corpus_args(run)
    run.add_argument("--tenant-id", default=None)
    run.add_argument("--concurrency", type=int, default=4)
    run.add_argument("--request-timeout", type=float, default=90.0)
    run.add_argument("--progress-every", type=int, default=25)
    run.add_argument("--job-interval-seconds", type=int, default=30)
    run.add_argument("--job-timeout-seconds", type=int, default=7200)
    run.add_argument("--palace-interval-seconds", type=int, default=20)
    run.add_argument("--palace-timeout-seconds", type=int, default=1800)
    run.add_argument("--relationship-queue-interval-seconds", type=int, default=30)
    run.add_argument("--relationship-queue-timeout-seconds", type=int, default=3600)
    run.add_argument("--eval-limit", type=int, default=10)
    run.add_argument("--min-expected-hit-ratio", type=float, default=0.50)
    run.add_argument("--require-retained-nist-top-ranks", action="store_true")
    run.add_argument("--resume", action="store_true")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--keep-going", action="store_true")
    run.add_argument("--no-palace-wait", action="store_true")
    run.add_argument("--no-relationship-queue-wait", action="store_true")
    run.add_argument("--allow-palace-timeout", action="store_true")
    run.set_defaults(func=cmd_run)

    matrix = sub.add_parser(
        "matrix",
        help="Plan or execute a realistic NIST relationship-policy benchmark matrix.",
    )
    matrix.add_argument("--matrix-id", default=None)
    matrix.add_argument("--target-counts", type=int, nargs="+", default=[500, 1000])
    matrix.add_argument(
        "--relationship-policies",
        choices=["deferred", "immediate", "skip"],
        nargs="+",
        default=["deferred", "immediate"],
    )
    matrix.add_argument("--chunks-per-document", type=int, default=10)
    matrix.add_argument("--chunk-chars", type=int, default=3600)
    matrix.add_argument("--overlap-chars", type=int, default=250)
    matrix.add_argument("--source-document-candidates", type=int, default=80)
    matrix.add_argument("--tenant-id", default=None)
    matrix.add_argument("--concurrency", type=int, default=4)
    matrix.add_argument("--request-timeout", type=float, default=90.0)
    matrix.add_argument("--progress-every", type=int, default=25)
    matrix.add_argument("--job-interval-seconds", type=int, default=30)
    matrix.add_argument("--job-timeout-seconds", type=int, default=7200)
    matrix.add_argument("--palace-interval-seconds", type=int, default=20)
    matrix.add_argument("--palace-timeout-seconds", type=int, default=1800)
    matrix.add_argument("--relationship-queue-interval-seconds", type=int, default=30)
    matrix.add_argument("--relationship-queue-timeout-seconds", type=int, default=5400)
    matrix.add_argument("--eval-limit", type=int, default=10)
    matrix.add_argument("--min-expected-hit-ratio", type=float, default=0.50)
    matrix.add_argument("--require-retained-nist-top-ranks", action="store_true")
    matrix.add_argument("--enable-ai-enrichment", action="store_true")
    matrix.add_argument("--rebuild-corpus", action="store_true")
    matrix.add_argument("--resume", action="store_true")
    matrix.add_argument("--keep-going", action="store_true")
    matrix.add_argument("--no-palace-wait", action="store_true")
    matrix.add_argument("--no-relationship-queue-wait", action="store_true")
    matrix.add_argument("--allow-palace-timeout", action="store_true")
    matrix.add_argument("--dry-run", action="store_true", help="Write the matrix plan/report without API calls.")
    matrix.add_argument(
        "--report-only",
        action="store_true",
        help="Summarize existing local matrix artifacts without ingesting, waiting, or deleting data.",
    )
    matrix.set_defaults(func=cmd_matrix)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if hasattr(args, "run_id") and args.run_id:
        args.run_id = validate_run_id(args.run_id)
    if hasattr(args, "target_count") and args.target_count < 1:
        parser.error("--target-count must be positive")
    if hasattr(args, "target_counts") and any(target_count < 1 for target_count in args.target_counts):
        parser.error("--target-counts values must be positive")
    if hasattr(args, "chunks_per_document") and args.chunks_per_document < 1:
        parser.error("--chunks-per-document must be positive")
    if hasattr(args, "source_document_candidates") and args.source_document_candidates < 1:
        parser.error("--source-document-candidates must be positive")
    if hasattr(args, "chunk_chars") and args.chunk_chars < 1000:
        parser.error("--chunk-chars must be at least 1000")
    if hasattr(args, "overlap_chars") and args.overlap_chars < 0:
        parser.error("--overlap-chars must be non-negative")
    if hasattr(args, "sample_limit") and args.sample_limit is not None and args.sample_limit < 1:
        parser.error("--sample-limit must be positive when provided")
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
