import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.schemas.search import SearchRequest
from app.embedding_profile import resolve_embedding_profile
from app.services.search import SearchService, classify_query_intent


class _FakeEmbedder:
    profile = resolve_embedding_profile()

    async def embed_single(self, _query: str) -> list[float]:
        return [0.1] * self.profile.dimensions


class _FakeResult:
    def __init__(self, rows=None) -> None:
        self._rows = list(rows or [])

    def fetchall(self) -> list:
        return self._rows

    def scalars(self):
        return self

    def all(self) -> list:
        return self._rows


class _FakeDB:
    def __init__(self, rows=None, hint_rows=None, context_rows=None, graph_rows=None) -> None:
        self.last_params = None
        self.graph_params = None
        self.rows = list(rows or [])
        self.hint_rows = list(hint_rows or [])
        self.context_rows = list(context_rows or [])
        self.graph_rows = list(graph_rows or [])
        self.last_sql = None

    async def execute(self, sql, params=None):
        self.last_sql = str(sql)
        if params is None:
            return _FakeResult(self.hint_rows)
        if "min_chunk" in params:
            return _FakeResult(self.context_rows)
        if "seed_item_ids" in params:
            self.graph_params = params
            return _FakeResult(self.graph_rows)
        self.last_params = params
        return _FakeResult(self.rows)


class _FakeLocalEmbedder:
    profile = resolve_embedding_profile(
        provider="local-http",
        model="Alibaba-NLP/gte-modernbert-base",
        dimensions=768,
        profile_name="local-http-gte-modernbert-base",
    )

    async def embed_single(self, _query: str) -> list[float]:
        return [0.1] * self.profile.dimensions


def test_search_request_accepts_top_k_alias() -> None:
    body = SearchRequest.model_validate(
        {
            "query": "Instrument the main CTA so founder can see clicks in the morning review.",
            "top_k": 5,
            "candidate_limit": 32,
            "source_type": "note",
            "tags": ["agent-retrospective"],
            "min_score": 0.75,
        }
    )

    assert body.limit == 5
    assert body.candidate_limit == 32
    assert body.tags == ["agent-retrospective"]


def test_classify_query_intent_modes() -> None:
    cases = {
        "risk management framework categorize select implement assess authorize monitor": "canonical_factual",
        "latest status for transcript ingestion": "latest_status",
        "catch me up on the Palace retrieval work": "catch_up_summary",
        "timeline since 2026 for benchmark imports": "temporal",
        "find anything about room routing ideas": "exploratory",
        "show startup context wake-up brief": "startup_context",
    }

    for query, expected_intent in cases.items():
        assert classify_query_intent(query).name == expected_intent


def test_vector_search_passes_tags_as_array_param() -> None:
    db = _FakeDB()
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    asyncio.run(
        service.vector_search(
            query="test",
            tags=["agent-retrospective"],
            source_type="note",
            min_score=0.75,
        )
    )

    assert db.last_params is not None
    assert db.last_params["tags"] == ["agent-retrospective"]
    assert db.last_params["candidate_limit"] == 40


def test_vector_search_accepts_candidate_limit_separate_from_display_limit() -> None:
    db = _FakeDB()
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    asyncio.run(
        service.vector_search(
            query="test",
            limit=3,
            candidate_limit=80,
        )
    )

    assert db.last_params is not None
    assert db.last_params["candidate_limit"] == 80
    assert service.last_ranking_trace is not None
    assert service.last_ranking_trace["display_limit"] == 3
    assert service.last_ranking_trace["candidate_limit"] == 80


def test_vector_search_uses_side_profile_storage_for_non_default_profile() -> None:
    db = _FakeDB()
    service = SearchService(db, _FakeLocalEmbedder(), tenant_id="default")

    asyncio.run(service.vector_search(query="test"))

    assert db.last_sql is not None
    assert "FROM embedding_profile_vectors e" in db.last_sql
    assert "embedding_half_768" in db.last_sql
    assert "halfvec(768)" in db.last_sql
    assert db.last_params is not None
    assert db.last_params["embedding_profile_name"] == "local-http-gte-modernbert-base"
    assert db.last_params["embedding_dimensions"] == 768
    assert service.last_ranking_trace is not None
    assert service.last_ranking_trace["embedding_profile"]["storage"] == "embedding_profile_vectors"


def test_vector_search_hydrates_first_middle_and_last_neighbor_chunks() -> None:
    now = datetime.now(timezone.utc)
    first_id = uuid.uuid4()
    middle_id = uuid.uuid4()
    last_id = uuid.uuid4()
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=first_id,
                title="First chunk item",
                summary=None,
                source_type="note",
                source_url=None,
                tags=[],
                created_at=now,
                chunk_text="first matched",
                chunk_index=0,
                score=0.9,
                item_metadata={},
            ),
            SimpleNamespace(
                item_id=middle_id,
                title="Middle chunk item",
                summary=None,
                source_type="note",
                source_url=None,
                tags=[],
                created_at=now,
                chunk_text="middle matched",
                chunk_index=1,
                score=0.8,
                item_metadata={},
            ),
            SimpleNamespace(
                item_id=last_id,
                title="Last chunk item",
                summary=None,
                source_type="note",
                source_url=None,
                tags=[],
                created_at=now,
                chunk_text="last matched",
                chunk_index=2,
                score=0.7,
                item_metadata={},
            ),
        ],
        context_rows=[
            SimpleNamespace(item_id=first_id, chunk_index=0, chunk_text="first matched"),
            SimpleNamespace(item_id=first_id, chunk_index=1, chunk_text="first next"),
            SimpleNamespace(item_id=middle_id, chunk_index=0, chunk_text="middle previous"),
            SimpleNamespace(item_id=middle_id, chunk_index=1, chunk_text="middle matched"),
            SimpleNamespace(item_id=middle_id, chunk_index=2, chunk_text="middle next"),
            SimpleNamespace(item_id=last_id, chunk_index=1, chunk_text="last previous"),
            SimpleNamespace(item_id=last_id, chunk_index=2, chunk_text="last matched"),
        ],
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    results = asyncio.run(
        service.vector_search(
            query="chunk context",
            limit=3,
            include_neighbor_chunks=True,
            neighbor_chunk_window=1,
        )
    )

    assert [chunk.relation for chunk in results[0].context_chunks or []] == ["matched", "next"]
    assert [chunk.relation for chunk in results[1].context_chunks or []] == [
        "previous",
        "matched",
        "next",
    ]
    assert [chunk.relation for chunk in results[2].context_chunks or []] == ["previous", "matched"]
    assert service.last_ranking_trace is not None
    assert service.last_ranking_trace["include_neighbor_chunks"] is True
    assert service.last_ranking_trace["context_budget_truncated"] is False


def test_vector_search_exposes_source_project_from_agent_workspace() -> None:
    now = datetime.now(timezone.utc)
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=uuid.uuid4(),
                title="Hermes memory",
                summary=None,
                source_type="note",
                source_url=None,
                tags=["hermes-memory-tool"],
                created_at=now,
                chunk_text="Project-specific memory.",
                chunk_index=0,
                score=0.9,
                item_metadata={
                    "memory_entry": {
                        "scope": {"type": "workspace", "key": "palaceoftruth"},
                        "metadata": {"agent_workspace": "Palace Of Truth"},
                    }
                },
            )
        ],
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="tenant-a")

    results = asyncio.run(service.vector_search(query="project memory", limit=1))

    assert results[0].source_project == "palace-of-truth"
    assert results[0].retrieved_scope_type == "workspace"
    assert results[0].retrieved_scope_key == "palaceoftruth"
    assert results[0].retrieved_scope_label == "workspace/palaceoftruth"
    assert service.last_ranking_trace["results"][0]["source_project"] == "palace-of-truth"
    assert service.last_ranking_trace["results"][0]["retrieved_scope_label"] == "workspace/palaceoftruth"


def test_vector_search_does_not_infer_source_project_from_legacy_metadata() -> None:
    now = datetime.now(timezone.utc)
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=uuid.uuid4(),
                title="Legacy memory",
                summary=None,
                source_type="note",
                source_url=None,
                tags=["agent-retrospective"],
                created_at=now,
                chunk_text="Legacy project memory.",
                chunk_index=0,
                score=0.9,
                item_metadata={
                    "memory_entry": {
                        "legacy_kind": "task_retrospective",
                        "scope": {"type": "workspace", "key": "palaceoftruth"},
                    },
                    "memory_contract": {"project_id": "palaceoftruth"},
                },
            )
        ],
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="tenant-a")

    results = asyncio.run(service.vector_search(query="legacy memory", limit=1))

    assert results[0].source_project is None
    assert service.last_ranking_trace["results"][0]["source_project"] is None


def test_vector_search_exposes_ocr_image_analysis_provenance() -> None:
    now = datetime.now(timezone.utc)
    item_id = uuid.uuid4()
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=item_id,
                title="Receipt image",
                summary=None,
                source_type="ocr_text",
                source_url="https://example.test/receipt",
                tags=["visual"],
                created_at=now,
                chunk_text="TOTAL $42.10",
                chunk_index=0,
                score=0.91,
                item_metadata={
                    "image_analysis": {
                        "caption": "Receipt on a table.",
                        "visible_text": ["TOTAL $42.10"],
                        "byte_hash": "a" * 64,
                        "artifact": {
                            "filename": "receipt.png",
                            "media_type": "image/png",
                            "storage_path": "/tmp/palaceoftruth/upload-artifacts/receipt.png",
                        },
                        "vision": {
                            "provider": "openai",
                            "model": "gpt-4o-mini",
                            "confidence": 0.84,
                        },
                    }
                },
            )
        ],
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    results = asyncio.run(service.vector_search(query="receipt total", limit=1))

    provenance = results[0].retrieval_provenance
    assert provenance is not None
    assert provenance.modality == "ocr_text"
    assert provenance.candidate_source == "image_analysis.visible_text"
    assert provenance.support_level == "strong"
    assert provenance.original_artifact_url == f"/api/v1/items/{item_id}/artifact"
    assert provenance.byte_hash == "a" * 64
    assert results[0].artifact_citation is not None
    assert results[0].artifact_citation.original_artifact_label == "/tmp/palaceoftruth/upload-artifacts/receipt.png"
    trace_row = service.last_ranking_trace["results"][0]
    assert trace_row["candidate_modality"] == "ocr_text"
    assert trace_row["candidate_source"] == "image_analysis.visible_text"
    assert trace_row["candidate_provenance"]["original_artifact_url"] == f"/api/v1/items/{item_id}/artifact"


def test_vector_search_exposes_browser_image_native_weak_support() -> None:
    now = datetime.now(timezone.utc)
    item_id = uuid.uuid4()
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=item_id,
                title="Architecture diagram",
                summary=None,
                source_type="image_candidate",
                source_url=None,
                tags=["visual"],
                created_at=now,
                chunk_text="Architecture diagram",
                chunk_index=0,
                score=0.86,
                item_metadata={
                    "browser_capture_image": {
                        "source_post_url": "https://x.com/example/status/123",
                        "candidate_url": "https://pbs.twimg.com/media/diagram.jpg",
                        "final_url": "https://pbs.twimg.com/media/diagram-large.jpg",
                        "media_type": "image/jpeg",
                        "byte_hash": "b" * 64,
                    }
                },
            )
        ],
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    results = asyncio.run(service.vector_search(query="architecture diagram", limit=1))

    provenance = results[0].retrieval_provenance
    assert provenance is not None
    assert provenance.modality == "image_native"
    assert provenance.candidate_source == "browser_capture_image"
    assert provenance.support_level == "weak"
    assert provenance.source_url == "https://x.com/example/status/123"
    assert provenance.original_artifact_url == "https://pbs.twimg.com/media/diagram-large.jpg"
    assert provenance.notes == ["image-native evidence has no supporting OCR/caption text"]
    trace_row = service.last_ranking_trace["results"][0]
    assert trace_row["candidate_modality"] == "image_native"
    assert trace_row["support_level"] == "weak"
    assert trace_row["candidate_provenance"]["source_url"] == "https://x.com/example/status/123"


def test_vector_search_hydrated_neighbor_chunks_respect_context_budget() -> None:
    now = datetime.now(timezone.utc)
    item_id = uuid.uuid4()
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=item_id,
                title="Budgeted item",
                summary=None,
                source_type="note",
                source_url=None,
                tags=[],
                created_at=now,
                chunk_text="x" * 100,
                chunk_index=1,
                score=0.9,
                item_metadata={},
            )
        ],
        context_rows=[
            SimpleNamespace(item_id=item_id, chunk_index=0, chunk_text="p" * 100),
            SimpleNamespace(item_id=item_id, chunk_index=1, chunk_text="m" * 100),
            SimpleNamespace(item_id=item_id, chunk_index=2, chunk_text="n" * 100),
        ],
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    results = asyncio.run(
        service.vector_search(
            query="chunk context",
            limit=1,
            include_neighbor_chunks=True,
            context_budget_chars=220,
        )
    )

    assert [chunk.relation for chunk in results[0].context_chunks or []] == ["previous", "matched"]
    assert service.last_ranking_trace is not None
    assert service.last_ranking_trace["context_budget_chars"] == 220
    assert service.last_ranking_trace["context_budget_truncated"] is True


def test_vector_search_reranks_curated_media_above_negative_self_memory() -> None:
    media_id = uuid.uuid4()
    note_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=note_id,
                title="default: [Andrew] what do you know about Henry Intelligent Machines just based on memory",
                summary="Nothing — I don't have any stored knowledge about Henry Intelligent Machines in Palace of Truth yet.",
                source_type="note",
                source_url=None,
                tags=["scope-agent", "agent-orchestrator"],
                created_at=now,
                chunk_text="I don't have any stored knowledge about Henry Intelligent Machines in memory yet.",
                chunk_index=0,
                score=0.5069930310245876,
                item_metadata={
                    "memory_entry": {
                        "source": "hermes-agent",
                        "created_by_role": "assistant",
                        "scope": {"type": "agent", "key": "orchestrator"},
                    }
                },
            ),
            SimpleNamespace(
                item_id=media_id,
                title="Alex Finn on X",
                summary="Henry Intelligent Machines is an AI employee that can run a full team end to end.",
                source_type="media",
                source_url="https://x.com/AlexFinn/status/2041267605747712370",
                tags=["ai-agents", "workflow-automation"],
                created_at=now,
                chunk_text="Henry Intelligent Machines can autonomously execute full business workflows.",
                chunk_index=0,
                score=0.44260085134075483,
                item_metadata={},
            ),
        ]
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    results = asyncio.run(
        service.vector_search(
            query="Henry Intelligent Machines",
            limit=2,
            min_score=0.3,
            scope_type="agent",
            scope_key="orchestrator",
        )
    )

    assert [result.item_id for result in results] == [media_id]
    assert results[0].source_type == "media"
    assert results[0].score > 0.5


def test_vector_search_keeps_positive_agent_memory_when_not_a_negative_miss() -> None:
    now = datetime.now(timezone.utc)
    note_id = uuid.uuid4()
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=note_id,
                title="default: [Andrew] Henry rollout memory",
                summary="Henry Intelligent Machines is the automation platform Alex Finn highlighted.",
                source_type="note",
                source_url=None,
                tags=["scope-agent", "agent-orchestrator"],
                created_at=now,
                chunk_text="Henry Intelligent Machines is the automation platform Alex Finn highlighted in the shared library.",
                chunk_index=0,
                score=0.41,
                item_metadata={
                    "memory_entry": {
                        "source": "hermes-agent",
                        "created_by_role": "assistant",
                        "scope": {"type": "agent", "key": "orchestrator"},
                    }
                },
            ),
        ]
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    results = asyncio.run(
        service.vector_search(
            query="Henry Intelligent Machines",
            limit=1,
            min_score=0.3,
            scope_type="agent",
            scope_key="orchestrator",
        )
    )

    assert len(results) == 1
    assert results[0].item_id == note_id
    assert results[0].score == 0.36


def test_vector_search_boosts_exact_identifier_matches() -> None:
    identifier_id = uuid.uuid4()
    generic_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=generic_id,
                title="Embedding reliability notes",
                summary="Memory reliability issues around retrieval drift and stale context.",
                source_type="doc",
                source_url=None,
                tags=["memory", "retrieval"],
                created_at=now,
                chunk_text="The retrieval layer can drift when identifiers are only weak semantic matches.",
                chunk_index=0,
                score=0.59,
                item_metadata={},
            ),
            SimpleNamespace(
                item_id=identifier_id,
                title="HBX-2042 incident runbook",
                summary="Exact remediation steps for HBX-2042.",
                source_type="doc",
                source_url="https://example.com/runbooks/hbx-2042",
                tags=["ops", "incident"],
                created_at=now,
                chunk_text="HBX-2042 means the worker lost its sync cursor and needs a checkpoint reset.",
                chunk_index=0,
                score=0.47,
                item_metadata={},
            ),
        ]
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    results = asyncio.run(
        service.vector_search(
            query="HBX-2042",
            limit=2,
            min_score=0.3,
        )
    )

    assert [result.item_id for result in results] == [identifier_id, generic_id]
    assert results[0].score > results[1].score


def test_vector_search_reranks_nist_publication_with_stronger_title_body_coverage() -> None:
    expected_id = uuid.uuid4()
    adjacent_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=adjacent_id,
                title="NIST SP 800-39 - Managing Information Security Risk",
                summary="Enterprise risk guidance for organization mission and business process.",
                source_type="note",
                source_url="https://doi.org/10.6028/NIST.SP.800-39",
                tags=["nist-sp800", "risk-management"],
                created_at=now,
                chunk_text="Organization-wide information security risk management guidance.",
                chunk_index=0,
                score=0.60,
                item_metadata={},
            ),
            SimpleNamespace(
                item_id=expected_id,
                title="NIST SP 800-37r2 - Risk Management Framework",
                summary="Framework for categorizing systems, selecting controls, assessing controls, authorizing systems, and monitoring.",
                source_type="note",
                source_url="https://doi.org/10.6028/NIST.SP.800-37r2",
                tags=["nist-sp800", "risk-management"],
                created_at=now,
                chunk_text="The Risk Management Framework steps are categorize, select, implement, assess, authorize, and monitor.",
                chunk_index=0,
                score=0.42,
                item_metadata={},
            ),
        ]
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    results = asyncio.run(
        service.vector_search(
            query="risk management framework categorize select implement assess authorize monitor",
            limit=2,
            tags=["nist-sp800"],
            min_score=0.3,
        )
    )

    assert [result.item_id for result in results] == [expected_id, adjacent_id]
    assert results[0].score > results[1].score


def test_vector_search_reranks_short_nist_title_phrase_above_adjacent_risk_doc() -> None:
    expected_id = uuid.uuid4()
    adjacent_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=adjacent_id,
                title="NIST SP 800-39 - Managing Information Security Risk",
                summary="Guidance for organization-wide risk management programs.",
                source_type="note",
                source_url="https://doi.org/10.6028/NIST.SP.800-39",
                tags=["nist-sp800", "risk-management"],
                created_at=now,
                chunk_text="Frame, assess, respond to, and monitor information security risk.",
                chunk_index=0,
                score=0.62,
                item_metadata={},
            ),
            SimpleNamespace(
                item_id=expected_id,
                title="NIST SP 800-37r2 - Risk Management Framework",
                summary="Guide for applying the Risk Management Framework.",
                source_type="note",
                source_url="https://doi.org/10.6028/NIST.SP.800-37r2",
                tags=["nist-sp800", "risk-management"],
                created_at=now,
                chunk_text="Categorize systems, select controls, implement controls, assess controls, authorize systems, and monitor.",
                chunk_index=0,
                score=0.38,
                item_metadata={},
            ),
        ]
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    results = asyncio.run(
        service.vector_search(
            query="risk-management-framework",
            limit=2,
            tags=["nist-sp800"],
            min_score=0.3,
        )
    )

    assert [result.item_id for result in results] == [expected_id, adjacent_id]
    assert results[0].score > results[1].score


def test_vector_search_reranks_live_retained_rmf_title_above_adjacent_risk_doc() -> None:
    expected_id = uuid.uuid4()
    adjacent_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=adjacent_id,
                title="NIST SP 800-39 - Managing information security risk : organization, mission, and information system view - chunk 011",
                summary="NIST SP 800-39 corpus benchmark excerpt from Managing information security risk : organization, mission, and information system view.",
                source_type="note",
                source_url="https://doi.org/10.6028/NIST.SP.800-39#benchmark-chunk-011",
                tags=["nist-sp800", "risk-management"],
                created_at=now,
                chunk_text=(
                    "Risk management is a comprehensive process that requires organizations "
                    "to frame risk, assess risk, respond to risk once determined, and monitor "
                    "risk on an ongoing basis."
                ),
                chunk_index=0,
                score=0.7412558024448257,
                item_metadata={},
            ),
            SimpleNamespace(
                item_id=expected_id,
                title="NIST SP 800-37r2 - Risk management framework for information systems and organizations: a system life cycle approach for security and privacy - chunk 081",
                summary="NIST SP 800-37r2 corpus benchmark excerpt from Risk management framework for information systems and organizations: a system life cycle approach for security and privacy.",
                source_type="note",
                source_url="https://doi.org/10.6028/NIST.SP.800-37r2#benchmark-chunk-081",
                tags=["nist-sp800", "risk-management"],
                created_at=now,
                chunk_text=(
                    "Publication: NIST SP 800-37r2\n"
                    "Title: Risk management framework for information systems and organizations: "
                    "a system life cycle approach for security and privacy\n\n"
                    "Documenting as-implemented control information is essential to determine "
                    "whether changes are authorized and the impact on the security and privacy posture."
                ),
                chunk_index=0,
                score=0.6547133227604995,
                item_metadata={},
            ),
        ]
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    results = asyncio.run(
        service.vector_search(
            query="risk management framework categorize select implement assess authorize monitor",
            limit=2,
            tags=["nist-sp800"],
            min_score=0.3,
        )
    )

    assert [result.item_id for result in results] == [expected_id, adjacent_id]
    assert results[0].score > results[1].score


def test_vector_search_source_ranking_is_disabled_by_default(monkeypatch) -> None:
    monkeypatch.setattr("app.services.search.settings.retrieval_source_ranking_enabled", False)
    official_id = uuid.uuid4()
    log_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=log_id,
                title="Risk management framework chat log",
                summary="Transcript notes mention the risk management framework.",
                source_type="transcript",
                source_url=None,
                tags=["raw-transcript"],
                created_at=now,
                chunk_text="The chat log discusses risk management framework guidance.",
                chunk_index=0,
                score=0.76,
                item_metadata={},
            ),
            SimpleNamespace(
                item_id=official_id,
                title="NIST SP 800-37r2 - Risk Management Framework",
                summary="Official NIST risk management framework publication.",
                source_type="pdf",
                source_url="https://doi.org/10.6028/NIST.SP.800-37r2",
                tags=["nist-sp800"],
                created_at=now,
                chunk_text="The Risk Management Framework describes system security and privacy controls.",
                chunk_index=0,
                score=0.60,
                item_metadata={"authority": "official"},
            ),
        ]
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    results = asyncio.run(service.vector_search(query="risk management framework", limit=2))

    assert [result.item_id for result in results] == [log_id, official_id]
    assert service.last_ranking_trace is not None
    assert service.last_ranking_trace["query_intent"] == "canonical_factual"
    assert service.last_ranking_trace["source_ranking_enabled"] is False
    assert service.last_ranking_trace["ranking_feature_flags"]["strict_source_boosts"] is False


def test_vector_search_second_stage_reranker_is_disabled_by_default() -> None:
    target_id = uuid.uuid4()
    decoy_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=decoy_id,
                title="Unrelated note",
                summary=None,
                source_type="note",
                source_url=None,
                tags=[],
                created_at=now,
                chunk_text="miscellaneous content",
                chunk_index=0,
                score=0.90,
                item_metadata={},
            ),
            SimpleNamespace(
                item_id=target_id,
                title="Alpha reference",
                summary=None,
                source_type="note",
                source_url=None,
                tags=[],
                created_at=now,
                chunk_text="alpha",
                chunk_index=0,
                score=0.62,
                item_metadata={},
            ),
        ]
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    results = asyncio.run(service.vector_search(query="alpha", limit=2))

    assert [result.item_id for result in results] == [decoy_id, target_id]
    assert service.last_ranking_trace is not None
    assert service.last_ranking_trace["ranking_feature_flags"]["second_stage_reranker"] is False
    assert service.last_ranking_trace["second_stage_reranker"] == {
        "enabled": False,
        "provider": None,
        "status": "disabled",
        "candidate_limit": None,
        "candidate_count": 0,
        "latency_ms": None,
        "changed_top_k": False,
        "top_k_before": [],
        "top_k_after": [],
    }


def test_vector_search_second_stage_reranker_uses_deterministic_adapter_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr("app.services.search.settings.retrieval_second_stage_reranker_enabled", True)
    monkeypatch.setattr("app.services.search.settings.retrieval_second_stage_reranker_provider", "lexical-overlap")
    monkeypatch.setattr("app.services.search.settings.retrieval_second_stage_reranker_max_bonus", 0.4)
    target_id = uuid.uuid4()
    decoy_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=decoy_id,
                title="Unrelated note",
                summary=None,
                source_type="note",
                source_url=None,
                tags=[],
                created_at=now,
                chunk_text="miscellaneous content",
                chunk_index=0,
                score=0.90,
                item_metadata={},
            ),
            SimpleNamespace(
                item_id=target_id,
                title="Alpha reference",
                summary=None,
                source_type="note",
                source_url=None,
                tags=[],
                created_at=now,
                chunk_text="alpha",
                chunk_index=0,
                score=0.62,
                item_metadata={},
            ),
        ]
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    results = asyncio.run(service.vector_search(query="alpha", limit=2))

    assert [result.item_id for result in results] == [target_id, decoy_id]
    assert service.last_ranking_trace is not None
    reranker_trace = service.last_ranking_trace["second_stage_reranker"]
    assert reranker_trace["enabled"] is True
    assert reranker_trace["provider"] == "lexical-overlap"
    assert reranker_trace["status"] == "applied"
    assert reranker_trace["changed_top_k"] is True
    target_trace = next(
        row for row in service.last_ranking_trace["results"] if row["item_id"] == str(target_id)
    )
    assert target_trace["reranker_provider"] == "lexical-overlap"
    assert target_trace["reranker_reason"] == "query_token_overlap"
    assert target_trace["reranker_score"] == 1.0
    assert target_trace["adjustments"]["second_stage_reranker"] == 0.4


def test_vector_search_second_stage_reranker_respects_candidate_limit(monkeypatch) -> None:
    monkeypatch.setattr("app.services.search.settings.retrieval_second_stage_reranker_enabled", True)
    monkeypatch.setattr("app.services.search.settings.retrieval_second_stage_reranker_provider", "lexical-overlap")
    monkeypatch.setattr("app.services.search.settings.retrieval_second_stage_reranker_candidate_limit", 1)
    first_id = uuid.uuid4()
    second_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=first_id,
                title="First alpha",
                summary=None,
                source_type="note",
                source_url=None,
                tags=[],
                created_at=now,
                chunk_text="alpha",
                chunk_index=0,
                score=0.90,
                item_metadata={},
            ),
            SimpleNamespace(
                item_id=second_id,
                title="Second alpha",
                summary=None,
                source_type="note",
                source_url=None,
                tags=[],
                created_at=now,
                chunk_text="alpha",
                chunk_index=0,
                score=0.90,
                item_metadata={},
            ),
        ]
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    asyncio.run(service.vector_search(query="alpha", limit=2, candidate_limit=10))

    assert service.last_ranking_trace is not None
    assert service.last_ranking_trace["second_stage_reranker"]["candidate_count"] == 1
    first_trace = next(
        row for row in service.last_ranking_trace["results"] if row["item_id"] == str(first_id)
    )
    second_trace = next(
        row for row in service.last_ranking_trace["results"] if row["item_id"] == str(second_id)
    )
    assert first_trace["reranker_score"] == 1.0
    assert second_trace["reranker_score"] is None


def test_vector_search_second_stage_reranker_falls_back_on_error(monkeypatch) -> None:
    class BrokenReranker:
        name = "broken"

        def rerank(self, *, query, candidates):
            raise RuntimeError("provider unavailable with secret prompt")

    monkeypatch.setattr("app.services.search.settings.retrieval_second_stage_reranker_enabled", True)
    monkeypatch.setattr("app.services.search.settings.retrieval_second_stage_reranker_provider", "broken")
    monkeypatch.setattr("app.services.search._runtime_reranker_from_settings", lambda: BrokenReranker())
    first_id = uuid.uuid4()
    second_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=first_id,
                title="First",
                summary=None,
                source_type="note",
                source_url=None,
                tags=[],
                created_at=now,
                chunk_text="first",
                chunk_index=0,
                score=0.90,
                item_metadata={},
            ),
            SimpleNamespace(
                item_id=second_id,
                title="Second",
                summary=None,
                source_type="note",
                source_url=None,
                tags=[],
                created_at=now,
                chunk_text="second",
                chunk_index=0,
                score=0.62,
                item_metadata={},
            ),
        ]
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    results = asyncio.run(service.vector_search(query="alpha", limit=2))

    assert [result.item_id for result in results] == [first_id, second_id]
    assert service.last_ranking_trace is not None
    trace = service.last_ranking_trace["second_stage_reranker"]
    assert trace["status"] == "fallback_error"
    assert trace["error_class"] == "RuntimeError"
    assert "provider unavailable" not in trace.values()


def test_vector_search_second_stage_reranker_falls_back_on_timeout(monkeypatch) -> None:
    monkeypatch.setattr("app.services.search.settings.retrieval_second_stage_reranker_enabled", True)
    monkeypatch.setattr("app.services.search.settings.retrieval_second_stage_reranker_provider", "lexical-overlap")
    monkeypatch.setattr("app.services.search.settings.retrieval_second_stage_reranker_timeout_ms", 1)
    perf_values = iter([0.0, 1.0])
    monkeypatch.setattr("app.services.search.time.perf_counter", lambda: next(perf_values))
    first_id = uuid.uuid4()
    second_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=first_id,
                title="First",
                summary=None,
                source_type="note",
                source_url=None,
                tags=[],
                created_at=now,
                chunk_text="first",
                chunk_index=0,
                score=0.80,
                item_metadata={},
            ),
            SimpleNamespace(
                item_id=second_id,
                title="Alpha",
                summary=None,
                source_type="note",
                source_url=None,
                tags=[],
                created_at=now,
                chunk_text="alpha",
                chunk_index=0,
                score=0.62,
                item_metadata={},
            ),
        ]
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    results = asyncio.run(service.vector_search(query="alpha", limit=2))

    assert [result.item_id for result in results] == [first_id, second_id]
    assert service.last_ranking_trace is not None
    trace = service.last_ranking_trace["second_stage_reranker"]
    assert trace["status"] == "fallback_timeout"
    assert trace["latency_ms"] == 1000.0


def test_vector_search_source_ranking_boosts_authority_and_dampens_low_signal(monkeypatch) -> None:
    monkeypatch.setattr("app.services.search.settings.retrieval_source_ranking_enabled", True)
    official_id = uuid.uuid4()
    log_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=log_id,
                title="Risk management framework chat log",
                summary="Transcript notes mention the risk management framework.",
                source_type="transcript",
                source_url=None,
                tags=["raw-transcript"],
                created_at=now,
                chunk_text="The chat log discusses risk management framework guidance.",
                chunk_index=0,
                score=0.76,
                item_metadata={},
            ),
            SimpleNamespace(
                item_id=official_id,
                title="NIST SP 800-37r2 - Risk Management Framework",
                summary="Official NIST risk management framework publication.",
                source_type="pdf",
                source_url="https://doi.org/10.6028/NIST.SP.800-37r2",
                tags=["nist-sp800"],
                created_at=now,
                chunk_text="The Risk Management Framework describes system security and privacy controls.",
                chunk_index=0,
                score=0.60,
                item_metadata={"authority": "official"},
            ),
        ]
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    results = asyncio.run(service.vector_search(query="risk management framework", limit=2))

    assert [result.item_id for result in results] == [official_id, log_id]
    assert service.last_ranking_trace is not None
    assert service.last_ranking_trace["query_intent"] == "canonical_factual"
    assert service.last_ranking_trace["source_ranking_enabled"] is True
    assert service.last_ranking_trace["ranking_feature_flags"]["strict_source_boosts"] is True
    official_trace = next(
        row for row in service.last_ranking_trace["results"] if row["item_id"] == str(official_id)
    )
    log_trace = next(row for row in service.last_ranking_trace["results"] if row["item_id"] == str(log_id))
    assert official_trace["adjustments"]["authority_url"] > 0
    assert official_trace["adjustments"]["authority_metadata"] > 0
    assert set(official_trace["source_ranking_contributors"]) == {
        "authority_metadata",
        "authority_url",
        "nist_source_role_match",
    }
    assert log_trace["adjustments"]["low_signal_source"] < 0
    assert log_trace["adjustments"]["low_signal_tags"] < 0
    assert log_trace["source_ranking_contributors"] == ["low_signal_source", "low_signal_tags"]


def test_vector_search_demotes_nist_source_role_decoys(monkeypatch) -> None:
    monkeypatch.setattr("app.services.search.settings.retrieval_source_ranking_enabled", True)
    expected_id = uuid.uuid4()
    decoy_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=decoy_id,
                title="NIST SP 800-53B - Control Baselines for Information Systems and Organizations",
                summary="Baseline control selections for low, moderate, and high impact systems.",
                source_type="note",
                source_url="https://doi.org/10.6028/NIST.SP.800-53B",
                tags=["nist-sp800", "controls"],
                created_at=now,
                chunk_text="Baselines tailor control selections.",
                chunk_index=0,
                score=0.72,
                item_metadata={"nist": {"publication_id": "800-53B"}},
            ),
            SimpleNamespace(
                item_id=expected_id,
                title="NIST SP 800-53Ar5 - Assessing Security and Privacy Controls",
                summary="Assessment procedures for controls.",
                source_type="note",
                source_url="https://doi.org/10.6028/NIST.SP.800-53Ar5",
                tags=["nist-sp800", "controls"],
                created_at=now,
                chunk_text="Assessment procedure material for determining control effectiveness.",
                chunk_index=0,
                score=0.62,
                item_metadata={"nist": {"publication_id": "800-53Ar5"}},
            ),
        ]
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    results = asyncio.run(
        service.vector_search(
            query="Answer cites assessment procedure material and avoids substituting baseline or catalog text.",
            limit=2,
            tags=["nist-sp800"],
            min_score=0.3,
        )
    )

    assert [result.item_id for result in results] == [expected_id, decoy_id]
    assert service.last_ranking_trace is not None
    expected_trace = next(
        row for row in service.last_ranking_trace["results"] if row["item_id"] == str(expected_id)
    )
    decoy_trace = next(
        row for row in service.last_ranking_trace["results"] if row["item_id"] == str(decoy_id)
    )
    assert expected_trace["query_source_role"] == "control-assessment"
    assert expected_trace["source_publication_id"] == "80053ar5"
    assert expected_trace["source_role"] == "control-assessment"
    assert expected_trace["adjustments"]["nist_source_role_match"] > 0
    assert decoy_trace["source_role"] == "control-baseline"
    assert decoy_trace["adjustments"]["nist_source_role_decoy"] < 0


def test_vector_search_uses_first_nist_role_phrase_for_adjacent_authority_cases(monkeypatch) -> None:
    monkeypatch.setattr("app.services.search.settings.retrieval_source_ranking_enabled", True)
    engineering_id = uuid.uuid4()
    resiliency_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=resiliency_id,
                title="NIST SP 800-160v2r1 - Developing Cyber-Resilient Systems",
                summary="Cyber-resiliency techniques for adversary disruption.",
                source_type="note",
                source_url="https://doi.org/10.6028/NIST.SP.800-160v2r1",
                tags=["nist-sp800"],
                created_at=now,
                chunk_text="Cyber-resiliency techniques and approaches.",
                chunk_index=0,
                score=0.74,
                item_metadata={"publication_id": "800-160v2r1"},
            ),
            SimpleNamespace(
                item_id=engineering_id,
                title="NIST SP 800-160v1r1 - Systems Security Engineering",
                summary="Engineering trustworthy secure resilient systems.",
                source_type="note",
                source_url="https://doi.org/10.6028/NIST.SP.800-160v1r1",
                tags=["nist-sp800"],
                created_at=now,
                chunk_text="Systems security engineering for trustworthy secure resilient systems.",
                chunk_index=0,
                score=0.60,
                item_metadata={"publication_id": "800-160v1r1"},
            ),
        ]
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    results = asyncio.run(
        service.vector_search(
            query="Answer distinguishes systems security engineering from cyber-resiliency techniques.",
            limit=2,
            tags=["nist-sp800"],
            min_score=0.3,
        )
    )

    assert [result.item_id for result in results] == [engineering_id, resiliency_id]
    assert service.last_ranking_trace is not None
    expected_trace = next(
        row for row in service.last_ranking_trace["results"] if row["item_id"] == str(engineering_id)
    )
    decoy_trace = next(
        row for row in service.last_ranking_trace["results"] if row["item_id"] == str(resiliency_id)
    )
    assert expected_trace["query_source_role"] == "systems-security-engineering"
    assert expected_trace["adjustments"]["nist_source_role_match"] > 0
    assert decoy_trace["adjustments"]["nist_source_role_decoy"] < 0


def test_vector_search_reranks_secure_software_framework_above_supply_chain_decoy(monkeypatch) -> None:
    monkeypatch.setattr("app.services.search.settings.retrieval_source_ranking_enabled", True)
    ssdf_id = uuid.uuid4()
    supply_chain_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=supply_chain_id,
                title="NIST SP 800-161r1 - Cybersecurity Supply Chain Risk Management Practices",
                summary="Supply-chain risk management guidance for suppliers and acquirers.",
                source_type="note",
                source_url="https://doi.org/10.6028/NIST.SP.800-161r1",
                tags=["nist-sp800"],
                created_at=now,
                chunk_text="Supply-chain risk management practices for federal systems.",
                chunk_index=0,
                score=0.73,
                item_metadata={"publication_id": "800-161r1"},
            ),
            SimpleNamespace(
                item_id=ssdf_id,
                title="NIST SP 800-218 - Secure Software Development Framework",
                summary="SSDF practices, tasks, and implementation examples.",
                source_type="note",
                source_url="https://doi.org/10.6028/NIST.SP.800-218",
                tags=["nist-sp800"],
                created_at=now,
                chunk_text="Secure software development framework practices and tasks.",
                chunk_index=0,
                score=0.59,
                item_metadata={"publication_id": "800-218"},
            ),
        ]
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    results = asyncio.run(
        service.vector_search(
            query="Answer cites SSDF practices/tasks and avoids replacing them with supply-chain text.",
            limit=2,
            tags=["nist-sp800"],
            min_score=0.3,
        )
    )

    assert [result.item_id for result in results] == [ssdf_id, supply_chain_id]
    assert service.last_ranking_trace is not None
    expected_trace = next(
        row for row in service.last_ranking_trace["results"] if row["item_id"] == str(ssdf_id)
    )
    decoy_trace = next(
        row for row in service.last_ranking_trace["results"] if row["item_id"] == str(supply_chain_id)
    )
    assert expected_trace["query_source_role"] == "secure-software-framework"
    assert expected_trace["source_role"] == "secure-software-framework"
    assert expected_trace["adjustments"]["nist_source_role_match"] > 0
    assert decoy_trace["source_role"] == "supply-chain-risk"
    assert decoy_trace["adjustments"]["nist_source_role_decoy"] < 0


def test_vector_search_skips_nist_source_role_adjustment_without_specific_role_query(monkeypatch) -> None:
    monkeypatch.setattr("app.services.search.settings.retrieval_source_ranking_enabled", True)
    catalog_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=catalog_id,
                title="NIST SP 800-53r5 - Security and Privacy Controls",
                summary="Control catalog guidance.",
                source_type="note",
                source_url="https://doi.org/10.6028/NIST.SP.800-53r5",
                tags=["nist-sp800"],
                created_at=now,
                chunk_text="Controls for systems and organizations.",
                chunk_index=0,
                score=0.62,
                item_metadata={"publication_id": "800-53r5"},
            )
        ]
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    asyncio.run(service.vector_search(query="NIST source overview", limit=1, tags=["nist-sp800"]))

    assert service.last_ranking_trace is not None
    trace = service.last_ranking_trace["results"][0]
    assert trace["query_source_role"] is None
    assert trace["source_role"] == "control-catalog"
    assert "nist_source_role_match" not in trace["adjustments"]
    assert "nist_source_role_decoy" not in trace["adjustments"]


def test_vector_search_relationship_graph_expansion_is_disabled_by_default(monkeypatch) -> None:
    monkeypatch.setattr("app.services.search.settings.retrieval_relationship_expansion_enabled", False)
    seed_id = uuid.uuid4()
    related_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=seed_id,
                title="Room routing ideas",
                summary="Seed document for related room routing notes.",
                source_type="note",
                source_url=None,
                tags=["retrieval"],
                created_at=now,
                chunk_text="Find anything about room routing ideas.",
                chunk_index=0,
                score=0.65,
                item_metadata={},
            )
        ],
        graph_rows=[
            SimpleNamespace(
                item_id=related_id,
                title="Neighbor graph note",
                summary="Related graph expansion candidate.",
                source_type="note",
                source_url=None,
                tags=["retrieval"],
                created_at=now,
                chunk_text="A relationship candidate that should not be queried.",
                chunk_index=0,
                score=0.62,
                item_metadata={},
                confidence=0.95,
            )
        ],
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    results = asyncio.run(service.vector_search(query="find anything about room routing ideas", limit=3))

    assert [result.item_id for result in results] == [seed_id]
    assert db.graph_params is None
    assert service.last_ranking_trace is not None
    assert service.last_ranking_trace["relationship_graph_expansion_enabled"] is False
    assert service.last_ranking_trace["relationship_graph_candidate_count"] == 0


def test_vector_search_relationship_graph_expansion_adds_bounded_related_candidates(monkeypatch) -> None:
    monkeypatch.setattr("app.services.search.settings.retrieval_relationship_expansion_enabled", True)
    monkeypatch.setattr("app.services.search.settings.retrieval_relationship_min_confidence", 0.8)
    monkeypatch.setattr("app.services.search.settings.retrieval_relationship_fanout_limit", 2)
    monkeypatch.setattr("app.services.search.settings.retrieval_relationship_hop_decay", 0.5)
    monkeypatch.setattr("app.services.search.settings.retrieval_relationship_max_bonus", 0.1)
    seed_id = uuid.uuid4()
    related_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=seed_id,
                title="Room routing ideas",
                summary="Seed document for related room routing notes.",
                source_type="note",
                source_url=None,
                tags=["retrieval"],
                created_at=now,
                chunk_text="Find anything about room routing ideas.",
                chunk_index=0,
                score=0.65,
                item_metadata={},
            )
        ],
        graph_rows=[
            SimpleNamespace(
                item_id=related_id,
                title="Neighbor graph note",
                summary="Related graph expansion candidate.",
                source_type="note",
                source_url=None,
                tags=["retrieval"],
                created_at=now,
                chunk_text="Related item connected through the Palace graph.",
                chunk_index=0,
                score=0.9,
                item_metadata={},
                confidence=0.9,
            )
        ],
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    results = asyncio.run(service.vector_search(query="find anything about room routing ideas", limit=3))

    assert [result.item_id for result in results] == [related_id, seed_id]
    assert db.graph_params is not None
    assert db.graph_params["seed_item_ids"] == [seed_id]
    assert db.graph_params["min_confidence"] == 0.8
    assert db.graph_params["fanout_limit"] == 2
    assert service.last_ranking_trace is not None
    assert service.last_ranking_trace["ranking_features_version"] == 2
    assert service.last_ranking_trace["relationship_graph_expansion_enabled"] is True
    assert service.last_ranking_trace["relationship_graph_candidate_count"] == 1
    related_trace = next(
        row for row in service.last_ranking_trace["results"] if row["item_id"] == str(related_id)
    )
    assert related_trace["relationship_graph_score"] == 0.45
    assert related_trace["adjustments"]["relationship_graph"] == 0.045


def test_vector_search_opt_in_retrieval_lens_enables_advisory_graph_signal(monkeypatch) -> None:
    monkeypatch.setattr("app.services.search.settings.retrieval_relationship_expansion_enabled", False)
    monkeypatch.setattr("app.services.search.settings.retrieval_relationship_min_confidence", 0.8)
    monkeypatch.setattr("app.services.search.settings.retrieval_relationship_fanout_limit", 2)
    monkeypatch.setattr("app.services.search.settings.retrieval_relationship_hop_decay", 0.5)
    monkeypatch.setattr("app.services.search.settings.retrieval_relationship_max_bonus", 0.1)
    seed_id = uuid.uuid4()
    related_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=seed_id,
                title="Engineering room routing ideas",
                summary="Seed document for related implementation notes.",
                source_type="note",
                source_url=None,
                tags=["retrieval"],
                created_at=now,
                chunk_text="Find anything about room routing ideas.",
                chunk_index=0,
                score=0.65,
                item_metadata={},
            )
        ],
        graph_rows=[
            SimpleNamespace(
                item_id=related_id,
                title="Related implementation note",
                summary="Graph-expanded implementation context.",
                source_type="note",
                source_url=None,
                tags=["retrieval"],
                created_at=now,
                chunk_text="Related implementation note connected through the graph.",
                chunk_index=0,
                score=0.9,
                item_metadata={},
                confidence=0.9,
            )
        ],
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    results = asyncio.run(
        service.vector_search(
            query="find anything about room routing ideas",
            limit=3,
            retrieval_lens="engineering",
        )
    )

    assert [result.item_id for result in results] == [related_id, seed_id]
    assert service.last_ranking_trace is not None
    assert service.last_ranking_trace["retrieval_lens"] == "engineering"
    assert service.last_ranking_trace["retrieval_lens_profile"]["trace_label"] == "engineering-context"
    assert service.last_ranking_trace["ranking_feature_flags"]["retrieval_lens"] is True
    assert service.last_ranking_trace["relationship_graph_expansion_enabled"] is True
    related_trace = next(
        row for row in service.last_ranking_trace["results"] if row["item_id"] == str(related_id)
    )
    assert related_trace["relationship_graph_score"] == 0.45
    assert related_trace["adjustments"]["relationship_graph"] == 0.0495


def test_vector_search_relationship_graph_expansion_preserves_search_filters(monkeypatch) -> None:
    monkeypatch.setattr("app.services.search.settings.retrieval_relationship_expansion_enabled", True)
    seed_id = uuid.uuid4()
    room_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=seed_id,
                title="Explore tagged workspace graph",
                summary="Seed document for filter propagation.",
                source_type="note",
                source_url=None,
                tags=["retrieval", "workspace"],
                created_at=now,
                chunk_text="Find anything related to the workspace graph.",
                chunk_index=0,
                score=0.65,
                item_metadata={"memory_entry": {"scope": {"type": "workspace", "key": "palaceoftruth"}}},
            )
        ],
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="tenant-a")

    asyncio.run(
        service.vector_search(
            query="find anything related to the workspace graph",
            limit=2,
            source_type="note",
            room_ids=[room_id],
            scope_type="workspace",
            scope_key="palaceoftruth",
            tags=["retrieval"],
            tags_mode="all",
            date_from=now - timedelta(days=1),
            date_to=now + timedelta(days=1),
            exclude_private_memory_scopes=True,
        )
    )

    assert db.graph_params is not None
    assert db.graph_params["tenant_id"] == "tenant-a"
    assert db.graph_params["source_type"] == "note"
    assert db.graph_params["room_ids"] == [room_id]
    assert db.graph_params["scope_type"] == "workspace"
    assert db.graph_params["scope_key"] == "palaceoftruth"
    assert db.graph_params["tags"] == ["retrieval"]
    assert db.graph_params["tags_mode"] == "all"
    assert db.graph_params["exclude_private_memory_scopes"] is True


def test_vector_search_latest_status_intent_applies_recency_not_source_boosts(monkeypatch) -> None:
    monkeypatch.setattr("app.services.search.settings.retrieval_source_ranking_enabled", True)
    older_official_id = uuid.uuid4()
    recent_note_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=older_official_id,
                title="Incident recovery status",
                summary="Official historical status report.",
                source_type="note",
                source_url="https://example.gov/status-report",
                tags=["official"],
                created_at=now - timedelta(days=120),
                chunk_text="Incident recovery status from an older official report.",
                chunk_index=0,
                score=0.62,
                item_metadata={"authority": "official"},
            ),
            SimpleNamespace(
                item_id=recent_note_id,
                title="Incident recovery status",
                summary="Recent operational status note.",
                source_type="note",
                source_url=None,
                tags=["ops"],
                created_at=now - timedelta(hours=2),
                chunk_text="Latest status says recovery is waiting on replay validation.",
                chunk_index=0,
                score=0.52,
                item_metadata={},
            ),
        ]
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    results = asyncio.run(service.vector_search(query="latest status incident recovery", limit=2))

    assert [result.item_id for result in results] == [recent_note_id, older_official_id]
    assert service.last_ranking_trace is not None
    assert service.last_ranking_trace["query_intent"] == "latest_status"
    assert service.last_ranking_trace["source_ranking_enabled"] is False
    assert service.last_ranking_trace["ranking_feature_flags"]["recency"] is True
    recent_trace = next(
        row for row in service.last_ranking_trace["results"] if row["item_id"] == str(recent_note_id)
    )
    older_trace = next(
        row for row in service.last_ranking_trace["results"] if row["item_id"] == str(older_official_id)
    )
    assert recent_trace["adjustments"]["intent_recency"] > older_trace["adjustments"]["intent_recency"]
    assert "authority_url" not in older_trace["adjustments"]


def test_vector_search_demotes_generated_artifacts_for_ordinary_source_queries() -> None:
    canonical_id = uuid.uuid4()
    brief_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=brief_id,
                title="Wake-up brief for risk management framework",
                summary="Generated startup context about risk management framework sources.",
                source_type="note",
                source_url="memory://wakeup-brief/tenant/default/2026-05-08",
                tags=["scope-tenant_shared", "memory-wakeup"],
                created_at=now,
                chunk_text="Wake-up brief summarizing risk management framework notes.",
                chunk_index=0,
                score=0.82,
                item_metadata={"wakeup_brief": {"scope_type": "tenant", "generation": 4}},
            ),
            SimpleNamespace(
                item_id=canonical_id,
                title="NIST SP 800-37r2 - Risk Management Framework",
                summary="Canonical source for the risk management framework.",
                source_type="pdf",
                source_url="https://doi.org/10.6028/NIST.SP.800-37r2",
                tags=["nist-sp800", "risk-management"],
                created_at=now,
                chunk_text="The Risk Management Framework covers categorize, select, implement, assess, authorize, and monitor.",
                chunk_index=0,
                score=0.48,
                item_metadata={},
            ),
        ]
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    results = asyncio.run(service.vector_search(query="risk management framework", limit=2))

    assert [result.item_id for result in results] == [canonical_id, brief_id]
    brief_trace = next(row for row in service.last_ranking_trace["results"] if row["item_id"] == str(brief_id))
    assert brief_trace["adjustments"]["derived_artifact"] == -0.45
    assert brief_trace["derived_artifact_keys"] == ["wakeup_brief"]
    assert brief_trace["artifact_provenance_type"] == "wakeup_brief"
    assert brief_trace["artifact_provenance_label"] == "Wake-up brief"
    assert service.last_ranking_trace["query_allows_derived_artifacts"] is False


def test_vector_search_explicitly_includes_generated_artifacts_when_requested() -> None:
    canonical_id = uuid.uuid4()
    dream_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=dream_id,
                title="Memory dream about risk management framework",
                summary="Generated synthesis about risk management framework source notes.",
                source_type="note",
                source_url="memory://dream/palace-dream-summary/tenant/default/2026-05-08",
                tags=["memory-dream", "risk-management"],
                created_at=now,
                chunk_text="Generated artifact summarizes risk management framework source notes.",
                chunk_index=0,
                score=0.82,
                item_metadata={
                    "memory_dream": {
                        "artifact_type": "palace-dream-summary",
                        "claims_need_source": True,
                    }
                },
            ),
            SimpleNamespace(
                item_id=canonical_id,
                title="Risk management framework source note",
                summary="Canonical source note for the risk management framework.",
                source_type="note",
                source_url="memory://entry/workspace/nist",
                tags=["risk-management"],
                created_at=now,
                chunk_text="Source note for risk management framework retrieval.",
                chunk_index=0,
                score=0.48,
                item_metadata={"memory_entry": {"scope": {"type": "workspace", "key": "nist"}}},
            ),
        ]
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    results = asyncio.run(
        service.vector_search(
            query="risk management framework",
            limit=2,
            include_derived_artifacts=True,
        )
    )

    assert [result.item_id for result in results] == [dream_id, canonical_id]
    dream_trace = next(row for row in service.last_ranking_trace["results"] if row["item_id"] == str(dream_id))
    assert "derived_artifact" not in dream_trace["adjustments"]
    assert service.last_ranking_trace["query_allows_derived_artifacts"] is True
    assert service.last_ranking_trace["ranking_feature_flags"]["derived_artifacts_explicit"] is True
    assert service.last_ranking_trace["ranking_feature_flags"]["derived_artifacts_explicit"] is True


def test_vector_search_serializes_conversation_fact_source_span() -> None:
    source_item_id = uuid.uuid4()
    fact_item_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=fact_item_id,
                title="Conversation fact: Andrew said",
                summary="Andrew said: latest status is PR ready",
                source_type="note",
                source_url="memory://session/demo",
                tags=["conversation-fact", "derived-memory", "scope-agent", "agent-codex"],
                created_at=now,
                chunk_text="Subject: Andrew\nPredicate: said\nObject: latest status is PR ready",
                chunk_index=0,
                score=0.91,
                item_metadata={
                    "memory_entry": {
                        "scope": {"type": "agent", "key": "codex"},
                        "metadata": {
                            "conversation_fact": {
                                "source_item_id": str(source_item_id),
                                "source_span": {
                                    "source_item_id": str(source_item_id),
                                    "chunk_index": 2,
                                    "line_start": 8,
                                    "line_end": 9,
                                    "turn_index": 3,
                                },
                            },
                        },
                    },
                },
            )
        ]
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    results = asyncio.run(
        service.vector_search(
            query="latest status",
            limit=1,
            include_derived_artifacts=True,
        )
    )

    assert results[0].source_item_id == source_item_id
    assert results[0].source_span == {
        "source_item_id": str(source_item_id),
        "chunk_index": 2,
        "line_start": 8,
        "line_end": 9,
        "turn_index": 3,
    }
    trace_row = service.last_ranking_trace["results"][0]
    assert trace_row["derived_artifact_keys"] == ["conversation_fact"]
    assert trace_row["artifact_provenance_label"] == "Conversation fact"
    assert trace_row["source_item_id"] == str(source_item_id)


def test_vector_search_demotes_generated_artifacts_without_metadata() -> None:
    canonical_id = uuid.uuid4()
    rollup_id = uuid.uuid4()
    manifest_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=rollup_id,
                title="Diary rollup about risk management framework",
                summary="Generated diary rollup mentioning risk management framework.",
                source_type="note",
                source_url="memory://diary-rollup/workspace/nist/2026-05-08",
                tags=["diary-rollup"],
                created_at=now,
                chunk_text="Generated diary rollup mentions risk management framework.",
                chunk_index=0,
                score=0.86,
                item_metadata={},
            ),
            SimpleNamespace(
                item_id=manifest_id,
                title="Routing manifest for risk management framework",
                summary="Generated routing manifest mentioning risk management framework.",
                source_type="note",
                source_url=None,
                tags=["palace-routing-manifest"],
                created_at=now,
                chunk_text="Generated routing manifest mentions risk management framework.",
                chunk_index=0,
                score=0.84,
                item_metadata={},
            ),
            SimpleNamespace(
                item_id=canonical_id,
                title="Risk management framework source note",
                summary="Canonical source note for risk management framework.",
                source_type="note",
                source_url="memory://entry/workspace/nist",
                tags=["risk-management"],
                created_at=now,
                chunk_text="Source note for risk management framework.",
                chunk_index=0,
                score=0.48,
                item_metadata={"memory_entry": {"scope": {"type": "workspace", "key": "nist"}}},
            ),
        ]
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    results = asyncio.run(service.vector_search(query="risk management framework", limit=3))

    assert results[0].item_id == canonical_id
    traces = {row["item_id"]: row for row in service.last_ranking_trace["results"]}
    assert traces[str(rollup_id)]["adjustments"]["derived_artifact"] == -0.45
    assert traces[str(rollup_id)]["derived_artifact_keys"] == ["diary_rollup"]
    assert traces[str(manifest_id)]["adjustments"]["derived_artifact"] == -0.45
    assert traces[str(manifest_id)]["derived_artifact_keys"] == ["memory_dream"]


def test_vector_search_allows_explicit_derived_tag_opt_in() -> None:
    canonical_id = uuid.uuid4()
    dream_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=dream_id,
                title="Memory dream about risk management framework",
                summary="Generated dream mentioning risk management framework.",
                source_type="note",
                source_url="memory://dream/palace-dream-summary/workspace/nist/2026-05-08",
                tags=["memory-dream"],
                created_at=now,
                chunk_text="Generated dream mentions risk management framework.",
                chunk_index=0,
                score=0.86,
                item_metadata={},
            ),
            SimpleNamespace(
                item_id=canonical_id,
                title="Risk management framework source note",
                summary="Canonical source note for risk management framework.",
                source_type="note",
                source_url="memory://entry/workspace/nist",
                tags=["risk-management"],
                created_at=now,
                chunk_text="Source note for risk management framework.",
                chunk_index=0,
                score=0.48,
                item_metadata={"memory_entry": {"scope": {"type": "workspace", "key": "nist"}}},
            ),
        ]
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    results = asyncio.run(service.vector_search(query="risk management framework", limit=2, tags=["memory-dream"]))

    assert [result.item_id for result in results] == [dream_id, canonical_id]
    dream_trace = next(row for row in service.last_ranking_trace["results"] if row["item_id"] == str(dream_id))
    assert "derived_artifact" not in dream_trace["adjustments"]
    assert service.last_ranking_trace["query_allows_derived_artifacts"] is True


def test_vector_search_allows_generated_artifacts_for_startup_context_queries() -> None:
    canonical_id = uuid.uuid4()
    brief_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=brief_id,
                title="Tenant wake-up brief",
                summary="Generated startup context for today's session.",
                source_type="note",
                source_url="memory://wakeup-brief/tenant/default/2026-05-08",
                tags=["scope-tenant_shared", "memory-wakeup"],
                created_at=now,
                chunk_text="Wake-up brief with current room and retrieval health context.",
                chunk_index=0,
                score=0.70,
                item_metadata={"wakeup_brief": {"scope_type": "tenant", "generation": 4}},
            ),
            SimpleNamespace(
                item_id=canonical_id,
                title="Session startup checklist",
                summary="Source note for opening a Codex session.",
                source_type="doc",
                source_url=None,
                tags=["runbook"],
                created_at=now,
                chunk_text="Check the central task pool before implementation.",
                chunk_index=0,
                score=0.62,
                item_metadata={},
            ),
        ]
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    results = asyncio.run(service.vector_search(query="show startup context wake-up brief", limit=2))

    assert [result.item_id for result in results] == [brief_id, canonical_id]
    brief_trace = next(row for row in service.last_ranking_trace["results"] if row["item_id"] == str(brief_id))
    assert "derived_artifact" not in brief_trace["adjustments"]
    assert service.last_ranking_trace["query_allows_derived_artifacts"] is True


def test_vector_search_keeps_retained_nist_probe_above_memory_dream_artifact() -> None:
    nist_id = uuid.uuid4()
    dream_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=dream_id,
                title="Memory dream about retained NIST material",
                summary="Generated memory hygiene notes mention the risk management framework.",
                source_type="note",
                source_url="memory://memory-dream/tenant/default/2026-05-08",
                tags=["memory-dream", "nist-sp800"],
                created_at=now,
                chunk_text="Generated artifact says retained NIST sources include risk management framework references.",
                chunk_index=0,
                score=0.74,
                item_metadata={
                    "memory_dream": {
                        "artifact_type": "palace-hygiene-report",
                        "claims_need_source": True,
                    }
                },
            ),
            SimpleNamespace(
                item_id=nist_id,
                title="NIST SP 800-37r2 - Risk Management Framework",
                summary="Framework for categorizing systems and selecting, assessing, authorizing, and monitoring controls.",
                source_type="pdf",
                source_url="https://doi.org/10.6028/NIST.SP.800-37r2",
                tags=["nist-sp800", "risk-management"],
                created_at=now,
                chunk_text="The Risk Management Framework steps are categorize, select, implement, assess, authorize, and monitor.",
                chunk_index=0,
                score=0.46,
                item_metadata={},
            ),
        ]
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    results = asyncio.run(
        service.vector_search(
            query="risk management framework categorize select implement assess authorize monitor",
            limit=2,
            tags=["nist-sp800"],
            min_score=0.3,
        )
    )

    assert [result.item_id for result in results] == [nist_id, dream_id]
    dream_trace = next(row for row in service.last_ranking_trace["results"] if row["item_id"] == str(dream_id))
    assert dream_trace["adjustments"]["derived_artifact"] == -0.45
    assert dream_trace["derived_artifact_keys"] == ["memory_dream"]


def test_vector_search_hint_ranking_is_opt_in_and_bounded(monkeypatch) -> None:
    monkeypatch.setattr("app.services.search.settings.retrieval_hint_ranking_enabled", True)
    monkeypatch.setattr("app.services.search.settings.retrieval_hint_ranking_max_bonus", 0.06)
    hinted_id = uuid.uuid4()
    adjacent_id = uuid.uuid4()
    room_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=adjacent_id,
                title="Adjacent risk management source",
                summary="Risk management appears here.",
                source_type="pdf",
                source_url=None,
                tags=["nist-sp800"],
                created_at=now,
                chunk_text="Risk management discussion without the framework phrasing.",
                chunk_index=0,
                score=0.55,
                item_metadata={},
            ),
            SimpleNamespace(
                item_id=hinted_id,
                title="NIST SP 800-37r2",
                summary="Risk Management Framework source.",
                source_type="pdf",
                source_url=None,
                tags=["nist-sp800"],
                created_at=now,
                chunk_text="Categorize select implement assess authorize monitor.",
                chunk_index=0,
                score=0.50,
                item_metadata={},
            ),
        ],
        hint_rows=[
            SimpleNamespace(
                source_item_id=hinted_id,
                room_id=room_id,
                source_chunk_index=0,
                generation=3,
                hint_text="find anything about risk management framework",
            )
        ],
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    results = asyncio.run(
        service.vector_search(
            query="find anything about risk management framework",
            limit=2,
        )
    )

    assert [result.item_id for result in results] == [hinted_id, adjacent_id]
    trace = next(row for row in service.last_ranking_trace["results"] if row["item_id"] == str(hinted_id))
    assert trace["adjustments"]["retrieval_hint"] == 0.06
    assert trace["retrieval_hint_score"] == 1.0
    assert service.last_ranking_trace["retrieval_hint_ranking_enabled"] is True


def test_vector_search_uses_effective_date_for_latest_status_recency() -> None:
    older_id = uuid.uuid4()
    newer_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=older_id,
                title="Project status from older source",
                summary="A strong semantic match for project status.",
                source_type="note",
                source_url=None,
                tags=["status"],
                created_at=now,
                effective_date=now - timedelta(days=90),
                effective_date_source="published_metadata",
                effective_date_quality="high",
                chunk_text="The project status is from an older event date.",
                chunk_index=0,
                score=0.62,
                item_metadata={},
            ),
            SimpleNamespace(
                item_id=newer_id,
                title="Project status from current source",
                summary="A slightly weaker semantic match for project status.",
                source_type="note",
                source_url=None,
                tags=["status"],
                created_at=now - timedelta(days=90),
                effective_date=now,
                effective_date_source="published_metadata",
                effective_date_quality="high",
                chunk_text="The project status is current.",
                chunk_index=0,
                score=0.55,
                item_metadata={},
            ),
        ]
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    results = asyncio.run(service.vector_search(query="latest project status", limit=2))

    assert [result.item_id for result in results] == [newer_id, older_id]
    assert service.last_ranking_trace["query_intent"] == "latest_status"
    newer_trace = next(row for row in service.last_ranking_trace["results"] if row["item_id"] == str(newer_id))
    assert newer_trace["adjustments"]["intent_recency"] > 0
    assert newer_trace["effective_date_source"] == "published_metadata"


def test_vector_search_does_not_recency_boost_canonical_queries() -> None:
    older_id = uuid.uuid4()
    newer_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    db = _FakeDB(
        rows=[
            SimpleNamespace(
                item_id=older_id,
                title="NIST SP 800-37r2 - Risk Management Framework",
                summary="Official Risk Management Framework source.",
                source_type="note",
                source_url=None,
                tags=["nist-sp800"],
                created_at=now - timedelta(days=365),
                effective_date=now - timedelta(days=365),
                effective_date_source="published_metadata",
                effective_date_quality="high",
                chunk_text="Risk management framework categorize select implement assess authorize monitor.",
                chunk_index=0,
                score=0.64,
                item_metadata={},
            ),
            SimpleNamespace(
                item_id=newer_id,
                title="Recent risk management notes",
                summary="A newer but less authoritative source.",
                source_type="note",
                source_url=None,
                tags=["nist-sp800"],
                created_at=now,
                effective_date=now,
                effective_date_source="published_metadata",
                effective_date_quality="high",
                chunk_text="Recent note mentions risk management framework.",
                chunk_index=0,
                score=0.58,
                item_metadata={},
            ),
        ]
    )
    service = SearchService(db, _FakeEmbedder(), tenant_id="default")

    results = asyncio.run(service.vector_search(query="risk management framework", limit=2))

    assert [result.item_id for result in results] == [older_id, newer_id]
    assert service.last_ranking_trace["query_intent"] == "canonical_factual"
    assert all("intent_recency" not in row["adjustments"] for row in service.last_ranking_trace["results"])
