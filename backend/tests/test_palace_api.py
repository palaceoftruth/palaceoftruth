from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.palace import router
from app.auth import verify_api_key
from app.database import get_db
from app.models.item import Item
from app.models.palace import PalaceRoomEvent, PalaceRun, Room, SyncSource
from app.schemas.palace import (
    PalaceControlTower,
    PalaceDiaryRollupStatus,
    PalaceDiaryRollupSummary,
    PalaceFactRegistrySummary,
    PalaceMemoryHealthSummary,
    PalaceMemoryJobSummary,
    PalaceMcpActivityEvent,
    PalaceMcpActivitySummary,
    PalaceOverview,
    PalaceMemoryJobScope,
    PalaceRetrieveResponse,
    PalaceRetrieveTrace,
    PalaceRoomArtifactHealthSummary,
    PalaceRoomDetail,
    PalaceRoomSummary,
    PalaceRoomUpdate,
    PalaceRunSummary,
    PalaceSectionFreshness,
    SyncSourceSummary,
    PalaceTemporalFactSummary,
    PalaceWebhookHealthSummary,
    PalaceWebhookJobSummary,
    PalaceWakeupBriefStatus,
    PalaceWakeupBriefSummary,
    SyncRunSummary,
)
from app.schemas.search import SearchResult
from app.services.source_compiler import ItemSourceSummary, SourceChunkSummary, SourceRecordSummary
from app.services.source_compiler import (
    AnswerAuditItem,
    AnswerAuditReport,
    AnswerAuditSourceSummary,
    ClaimSourceSupportSummary,
    ClaimSupportReport,
    ClaimSupportSummary,
)
from app.workers.queues import PALACE_WORKER_QUEUE


class _FakeScalarResult:
    def __init__(self, rows) -> None:
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows

    def mappings(self):
        return self

    def one(self):
        if len(self.rows) != 1:
            raise AssertionError(f"Expected exactly one row, got {len(self.rows)}")
        return self.rows[0]

    def one_or_none(self):
        return self.rows[0] if self.rows else None


class FakeSession:
    def __init__(self, objects=None, *, scalar_results=None, execute_results=None) -> None:
        self.objects = objects or {}
        self.added = []
        self.scalar_results = list(scalar_results or [])
        self.execute_results = list(execute_results or [])

    async def get(self, model, key):
        return self.objects.get((model, key))

    async def scalar(self, _statement):
        if self.scalar_results:
            return self.scalar_results.pop(0)
        return None

    async def execute(self, _statement, _params=None):
        rows = self.execute_results.pop(0) if self.execute_results else []
        return _FakeScalarResult(rows)

    async def commit(self):
        return None

    async def refresh(self, _obj):
        return None

    def add(self, obj):
        self.added.append(obj)
        key = getattr(obj, "id", uuid.uuid4())
        self.objects[(type(obj), key)] = obj


class FakeArqPool:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict]] = []

    async def enqueue_job(self, name: str, **kwargs) -> None:
        self.enqueued.append((name, kwargs))


def _build_app(session: FakeSession, *, tenant_id: str = "tenant-a") -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.arq_pool = FakeArqPool()
    app.state.embedder = object()

    async def override_get_db():
        yield session

    async def override_verify(request: Request):
        request.state.tenant_id = tenant_id
        request.state.key_hash = "key-hash"
        return "raw-key"

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[verify_api_key] = override_verify
    return TestClient(app)


def _freshness(status: str = "fresh") -> PalaceSectionFreshness:
    return PalaceSectionFreshness(
        status=status,
        generation=3,
        target_generation=3,
        message="ok",
    )


def test_palace_overview_endpoint_returns_reviewed_shape(monkeypatch) -> None:
    client = _build_app(FakeSession())

    async def fake_build_overview(db, tenant_id: str):
        return PalaceOverview(
            tenant_id=tenant_id,
            dirty_generation=3,
            indexed_generation=2,
            backlog_generation=1,
            wings=[
                {
                    "id": uuid.uuid4(),
                    "slug": "product-growth",
                    "name": "Product / Growth",
                    "room_count": 1,
                    "item_count": 4,
                    "rooms": [
                        PalaceRoomSummary(
                            id=uuid.uuid4(),
                            wing_id=uuid.uuid4(),
                            name="Pricing Narrative",
                            stable_key="product-growth:pricing-narrative",
                            state="active",
                            item_count=4,
                            summary="Founder pricing notes live here.",
                            membership_status=_freshness(),
                            snapshot_status=_freshness("stale"),
                            tunnel_status=_freshness(),
                            redirect_room_id=None,
                        )
                    ],
                }
            ],
        )

    monkeypatch.setattr("app.api.palace.build_overview", fake_build_overview)

    response = client.get("/api/v1/palace")

    assert response.status_code == 200
    assert response.json()["tenant_id"] == "tenant-a"
    assert response.json()["wings"][0]["rooms"][0]["name"] == "Pricing Narrative"


def test_post_sync_source_returns_credential_metadata_without_secret(monkeypatch) -> None:
    client = _build_app(FakeSession())
    source_id = uuid.uuid4()

    async def fake_create_sync_source(db, *, tenant_id: str, body):
        assert tenant_id == "tenant-a"
        assert body.source_kind == "repo"
        assert body.credential_type == "github_pat"
        assert body.github_pat == "github_pat_123"
        return SyncSource(
            id=source_id,
            tenant_id=tenant_id,
            name=body.name,
            root_path=body.root_path,
            source_kind=body.source_kind,
            credential_type="github_pat",
            credential_ciphertext="encrypted-value",
            status="active",
            scan_interval_seconds=body.scan_interval_seconds,
            allowed_extensions=body.allowed_extensions,
        )

    monkeypatch.setattr("app.api.palace.create_sync_source", fake_create_sync_source)

    response = client.post(
        "/api/v1/palace/sync-sources",
        json={
            "name": "Private repo",
            "source_kind": "repo",
            "root_path": "https://github.com/palaceoftruth/palaceoftruth",
            "credential_type": "github_pat",
            "github_pat": "github_pat_123",
            "scan_interval_seconds": 900,
            "allowed_extensions": [".md"],
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["credential_type"] == "github_pat"
    assert payload["has_stored_credential"] is True


def test_get_palace_item_sources_is_tenant_bounded(monkeypatch) -> None:
    client = _build_app(FakeSession(), tenant_id="tenant-a")
    item_id = uuid.uuid4()
    record_id = uuid.uuid4()
    chunk_id = uuid.uuid4()

    async def fake_get_item_source_summary(db, *, tenant_id: str, item_id: uuid.UUID):
        assert tenant_id == "tenant-a"
        return ItemSourceSummary(
            tenant_id=tenant_id,
            item_id=item_id,
            source_records=(
                SourceRecordSummary(
                    id=record_id,
                    item_id=item_id,
                    source_kind="note",
                    source_uri="https://example.test/source",
                    source_version="version",
                    content_hash="hash",
                    status="active",
                    failure_reason=None,
                    metadata={"item_status": "ready"},
                    chunk_count=1,
                    chunks=(
                        SourceChunkSummary(
                            id=chunk_id,
                            chunk_index=0,
                            chunk_digest="digest",
                            token_count=5,
                            preview="chunk preview",
                        ),
                    ),
                ),
            ),
        )

    monkeypatch.setattr("app.api.palace.get_item_source_summary", fake_get_item_source_summary)

    response = client.get(f"/api/v1/palace/sources/{item_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["tenant_id"] == "tenant-a"
    assert payload["item_id"] == str(item_id)
    assert payload["source_records"][0]["chunks"][0]["preview"] == "chunk preview"
    assert "github_pat" not in payload


def test_get_palace_claim_support_is_tenant_bounded(monkeypatch) -> None:
    client = _build_app(FakeSession(), tenant_id="tenant-a")
    claim_id = uuid.uuid4()
    claim_source_id = uuid.uuid4()
    source_record_id = uuid.uuid4()
    source_item_id = uuid.uuid4()

    async def fake_get_claim_support_report(db, *, tenant_id: str, status: str | None, limit: int):
        assert tenant_id == "tenant-a"
        assert status == "conflicted"
        assert limit == 25
        return ClaimSupportReport(
            tenant_id=tenant_id,
            claims=(
                ClaimSupportSummary(
                    id=claim_id,
                    claim_key="decision:honor-source-backed-wakeup",
                    claim_text="Honor source-backed wakeup before generated summaries",
                    claim_type="decision",
                    confidence=0.8,
                    status="conflicted",
                    support_state="conflicted",
                    warning="claim_status_conflicted",
                    metadata={"temporal_fact_status": "active"},
                    sources=(
                        ClaimSourceSupportSummary(
                            id=claim_source_id,
                            source_record_id=source_record_id,
                            source_chunk_id=None,
                            source_item_id=source_item_id,
                            source_record_status="active",
                            support_role="supports",
                            status="current",
                            source_digest="source-fingerprint",
                            source_span={"temporal_fact_id": str(uuid.uuid4())},
                        ),
                    ),
                ),
            ),
        )

    monkeypatch.setattr("app.api.palace.get_claim_support_report", fake_get_claim_support_report)

    response = client.get("/api/v1/palace/claims/support?status=conflicted&limit=25")

    assert response.status_code == 200
    payload = response.json()
    assert payload["tenant_id"] == "tenant-a"
    assert payload["claims"][0]["claim_type"] == "decision"
    assert payload["claims"][0]["status"] == "conflicted"
    assert payload["claims"][0]["support_state"] == "conflicted"
    assert payload["claims"][0]["warning"] == "claim_status_conflicted"
    assert payload["claims"][0]["sources"][0]["status"] == "current"
    assert payload["claims"][0]["sources"][0]["source_record_status"] == "active"
    assert payload["claims"][0]["sources"][0]["source_item_id"] == str(source_item_id)
    assert "github_pat" not in payload


def test_get_palace_claim_support_ignores_non_decision_claim_type_query(monkeypatch) -> None:
    client = _build_app(FakeSession(), tenant_id="tenant-a")
    captured = {}

    async def fake_get_claim_support_report(db, **kwargs):
        captured.update(kwargs)
        return ClaimSupportReport(tenant_id="tenant-a", claims=())

    monkeypatch.setattr("app.api.palace.get_claim_support_report", fake_get_claim_support_report)

    response = client.get("/api/v1/palace/claims/support?claim_type=fact&limit=10")

    assert response.status_code == 200
    assert captured == {"tenant_id": "tenant-a", "status": None, "limit": 10}


def test_get_palace_answer_audit_is_tenant_bounded_and_redacted(monkeypatch) -> None:
    client = _build_app(FakeSession(), tenant_id="tenant-a")
    claim_id = uuid.uuid4()
    source_record_id = uuid.uuid4()
    source_chunk_id = uuid.uuid4()
    source_item_id = uuid.uuid4()
    captured = {}

    async def fake_get_answer_audit_report(db, **kwargs):
        captured.update(kwargs)
        return AnswerAuditReport(
            tenant_id="tenant-a",
            audit_scope="decision_claims",
            items=(
                AnswerAuditItem(
                    object_type="decision_claim",
                    object_id=claim_id,
                    object_key="decision:source-backed-answer-audit",
                    object_text="Audit answers through source-backed decision claims",
                    claim_type="decision",
                    claim_status="active",
                    support_state="source_backed",
                    audit_state="curated",
                    warning=None,
                    promotion_status="promoted",
                    source_count=1,
                    sources=(
                        AnswerAuditSourceSummary(
                            source_record_id=source_record_id,
                            source_chunk_id=source_chunk_id,
                            source_item_id=source_item_id,
                            source_record_status="active",
                            support_role="supports",
                            support_status="current",
                            source_digest="digest-a",
                            source_span={"source_chunk_digest": "digest-a"},
                        ),
                    ),
                    metadata={
                        "review_action": "promote",
                        "policy_limited": True,
                        "policy_reason": "workspace scope only",
                    },
                ),
            ),
        )

    monkeypatch.setattr("app.api.palace.get_answer_audit_report", fake_get_answer_audit_report)

    response = client.get(f"/api/v1/palace/answers/audit?claim_id={claim_id}&status=active&limit=25")

    assert response.status_code == 200
    assert captured == {"tenant_id": "tenant-a", "claim_id": claim_id, "status": "active", "limit": 25}
    payload = response.json()
    assert payload["tenant_id"] == "tenant-a"
    assert payload["audit_scope"] == "decision_claims"
    assert payload["items"][0]["audit_state"] == "curated"
    assert payload["items"][0]["promotion_status"] == "promoted"
    assert payload["items"][0]["sources"][0]["source_record_id"] == str(source_record_id)
    assert payload["items"][0]["metadata"]["policy_reason"] == "workspace scope only"
    assert "chunk_text" not in json.dumps(payload)
    assert "raw body" not in json.dumps(payload)


def test_review_palace_decision_claim_records_operator_action(monkeypatch) -> None:
    client = _build_app(FakeSession(), tenant_id="tenant-a")
    claim_id = uuid.uuid4()
    captured = {}

    async def fake_review_decision_claim(db, **kwargs):
        captured.update(kwargs)
        return ClaimSupportSummary(
            id=claim_id,
            claim_key="decision:promotion",
            claim_text="Use source-backed decisions at wakeup",
            claim_type="decision",
            confidence=0.91,
            status="active",
            support_state="source_backed",
            warning=None,
            metadata={
                "reviewed_by": "operator-a",
                "review_role": "operator",
                "review_action": "promote",
            },
            sources=(),
        )

    monkeypatch.setattr("app.api.palace.review_decision_claim", fake_review_decision_claim)

    response = client.post(
        f"/api/v1/palace/claims/{claim_id}/review",
        json={
            "action": "promote",
            "reviewed_by": "operator-a",
            "rationale": "Source support was inspected.",
        },
    )

    assert response.status_code == 200
    assert captured == {
        "tenant_id": "tenant-a",
        "claim_id": claim_id,
        "action": "promote",
        "reviewed_by": "operator-a",
        "review_role": "operator",
        "rationale": "Source support was inspected.",
    }
    payload = response.json()
    assert payload["status"] == "active"
    assert payload["support_state"] == "source_backed"
    assert payload["metadata"]["review_action"] == "promote"


def test_review_palace_decision_claim_reports_support_gate(monkeypatch) -> None:
    client = _build_app(FakeSession(), tenant_id="tenant-a")
    claim_id = uuid.uuid4()

    async def fake_review_decision_claim(db, **kwargs):
        from app.services.source_compiler import ClaimReviewError

        raise ClaimReviewError("source_support_required", "Decision claims require current exact source support before promotion.")

    monkeypatch.setattr("app.api.palace.review_decision_claim", fake_review_decision_claim)

    response = client.post(
        f"/api/v1/palace/claims/{claim_id}/review",
        json={"action": "promote", "reviewed_by": "operator-a"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "source_support_required"


def test_review_palace_decision_claim_404s_for_missing_decision_claim(monkeypatch) -> None:
    client = _build_app(FakeSession(), tenant_id="tenant-a")
    claim_id = uuid.uuid4()

    async def fake_review_decision_claim(db, **kwargs):
        return None

    monkeypatch.setattr("app.api.palace.review_decision_claim", fake_review_decision_claim)

    response = client.post(
        f"/api/v1/palace/claims/{claim_id}/review",
        json={"action": "reject", "reviewed_by": "operator-a"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Decision claim not found"


def test_palace_control_tower_includes_memory_health(monkeypatch) -> None:
    client = _build_app(FakeSession())

    async def fake_build_control_tower(db, tenant_id: str, arq_pool=None):
        return PalaceControlTower(
            tenant_id=tenant_id,
            dirty_generation=3,
            indexed_generation=2,
            backlog_generation=1,
            room_artifacts=PalaceRoomArtifactHealthSummary(
                target_generation=2,
                active_rooms=3,
                blocked_rooms=1,
                closets={"fresh": 1, "stale": 1},
                snapshots={"fresh": 2, "stale": 0},
                tunnels={"fresh": 1, "stale": 1},
            ),
            memory_health=PalaceMemoryHealthSummary(
                queued=1,
                processing=0,
                failed=1,
                retryable=1,
                recent_jobs=[
                    PalaceMemoryJobSummary(
                        job_id=uuid.uuid4(),
                        title="Shared launch brief",
                        status="failed",
                        scope=PalaceMemoryJobScope(type="workspace", key="launch-pad"),
                        accepted_as="canonical",
                        retriable=True,
                        source="hermes",
                        error_message="embedding timeout",
                        created_at=datetime.now(timezone.utc),
                        completed_at=datetime.now(timezone.utc),
                    )
                ],
            ),
            mcp_activity=PalaceMcpActivitySummary(
                registered_clients=1,
                recent_success=2,
                recent_error=0,
                recent_denied=1,
                recent_events=[
                    PalaceMcpActivityEvent(
                        id=uuid.uuid4(),
                        client_name="Codex local MCP",
                        client_key="codex-local",
                        operation="create_memory_entry",
                        required_scope="write",
                        status="success",
                        latency_ms=42,
                        params_summary={"body": {"redacted": True, "present": True}},
                        created_at=datetime.now(timezone.utc),
                    )
                ],
            ),
            webhook_health=PalaceWebhookHealthSummary(
                configured=2,
                pending=1,
                terminal=1,
                failed_jobs=1,
                retryable_jobs=1,
                recent_jobs=[
                    PalaceWebhookJobSummary(
                        job_id=uuid.uuid4(),
                        title="Webhook launch note",
                        job_type="note",
                        status="failed",
                        terminal=True,
                        error_message="receiver returned 500",
                        created_at=datetime.now(timezone.utc),
                        completed_at=datetime.now(timezone.utc),
                    )
                ],
            ),
            fact_registry=PalaceFactRegistrySummary(
                active=2,
                superseded=1,
                distinct_sources=2,
                last_extracted_at=datetime.now(timezone.utc),
                recent_facts=[
                    PalaceTemporalFactSummary(
                        id=uuid.uuid4(),
                        source_item_id=uuid.uuid4(),
                        source_item_title="Investor notes",
                        subject="Launch plan",
                        predicate="targets",
                        object_text="May 2026 rollout",
                        confidence=1.0,
                        status="active",
                        extracted_at=datetime.now(timezone.utc),
                    )
                ],
            ),
            diary_rollups=PalaceDiaryRollupSummary(
                fresh=1,
                stale=1,
                expected_through_day=datetime(2026, 4, 22, tzinfo=timezone.utc).date(),
                last_refreshed_at=datetime.now(timezone.utc),
                recent_rollups=[
                    PalaceDiaryRollupStatus(
                        title="Diary Rollup 2026-04-22 [workspace:launch-pad]",
                        scope_type="workspace",
                        scope_key="launch-pad",
                        day=datetime(2026, 4, 22, tzinfo=timezone.utc).date(),
                        updated_at=datetime.now(timezone.utc),
                        source_count=3,
                        stale=False,
                    )
                ],
            ),
            wakeup_briefs=PalaceWakeupBriefSummary(
                fresh=2,
                stale=1,
                generated_for_day=datetime(2026, 4, 23, tzinfo=timezone.utc).date(),
                last_refreshed_at=datetime.now(timezone.utc),
                recent_briefs=[
                    PalaceWakeupBriefStatus(
                        title="Wake-up Brief 2026-04-23 [tenant]",
                        scope_type="tenant",
                        generation=7,
                        updated_at=datetime.now(timezone.utc),
                        room_count=3,
                        diary_count=2,
                        fact_count=4,
                        stale=False,
                    )
                ],
            ),
            sync_sources=[],
            sync_runs=[],
            palace_runs=[],
        )

    monkeypatch.setattr("app.api.palace.build_control_tower", fake_build_control_tower)

    response = client.get("/api/v1/palace/control-tower")

    assert response.status_code == 200
    payload = response.json()
    assert payload["room_artifacts"]["target_generation"] == 2
    assert payload["room_artifacts"]["closets"] == {"fresh": 1, "stale": 1}
    assert payload["room_artifacts"]["blocked_rooms"] == 1
    assert payload["mcp_activity"]["registered_clients"] == 1
    assert payload["mcp_activity"]["recent_denied"] == 1
    assert payload["mcp_activity"]["recent_events"][0]["params_summary"]["body"]["redacted"] is True
    assert payload["memory_health"]["retryable"] == 1
    assert payload["webhook_health"]["configured"] == 2
    assert payload["webhook_health"]["recent_jobs"][0]["terminal"] is True
    assert payload["fact_registry"]["active"] == 2
    assert payload["diary_rollups"]["fresh"] == 1
    assert payload["diary_rollups"]["recent_rollups"][0]["scope_type"] == "workspace"
    assert payload["wakeup_briefs"]["fresh"] == 2
    assert payload["wakeup_briefs"]["recent_briefs"][0]["scope_type"] == "tenant"
    assert payload["memory_health"]["recent_jobs"][0]["scope"] == {"type": "workspace", "key": "launch-pad"}


def test_list_palace_mcp_clients_returns_counts_and_secret_safe_config() -> None:
    client_id = uuid.uuid4()
    session = FakeSession(
        execute_results=[
            [
                {
                    "id": client_id,
                    "tenant_id": "tenant-a",
                    "client_key": "codex-remote",
                    "display_name": "Codex remote MCP",
                    "allowed_scopes": ["read", "write"],
                    "metadata": {"owner": "codex"},
                    "oauth_revoked_at": None,
                    "oauth_token_ttl_seconds": 3600,
                    "created_at": datetime.now(timezone.utc),
                    "last_seen_at": datetime.now(timezone.utc),
                    "request_count": 3,
                    "success_count": 2,
                    "denied_count": 1,
                    "error_count": 0,
                    "last_request_at": datetime.now(timezone.utc),
                }
            ]
        ]
    )
    client = _build_app(session)

    response = client.get("/api/v1/palace/mcp-clients")

    assert response.status_code == 200
    payload = response.json()
    assert payload["tenant_id"] == "tenant-a"
    assert payload["clients"][0]["client_key"] == "codex-remote"
    assert payload["clients"][0]["request_count"] == 3
    assert payload["clients"][0]["denied_count"] == 1
    assert "client_secret" not in payload["config_snippets"]["http_oauth_toml"]
    assert "read -rsp" in payload["config_snippets"]["oauth_token_command"]


def test_register_palace_mcp_client_returns_secret_once_and_config() -> None:
    client_id = uuid.uuid4()
    session = FakeSession(
        execute_results=[
            [
                {
                    "id": client_id,
                    "tenant_id": "tenant-a",
                    "client_key": "codex-remote",
                    "display_name": "Codex remote MCP",
                    "allowed_scopes": ["read", "write"],
                    "metadata": {},
                    "oauth_revoked_at": None,
                    "oauth_token_ttl_seconds": 1800,
                    "created_at": datetime.now(timezone.utc),
                    "last_seen_at": None,
                }
            ]
        ]
    )
    client = _build_app(session)

    response = client.post(
        "/api/v1/palace/mcp-clients/register",
        json={
            "client_key": "codex-remote",
            "display_name": "Codex remote MCP",
            "allowed_scopes": ["read", "write"],
            "token_ttl_seconds": 1800,
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["client"]["client_key"] == "codex-remote"
    assert isinstance(payload["client_secret"], str)
    assert payload["client_secret"] not in payload["config_snippets"]["http_oauth_toml"]
    assert "PALACEOFTRUTH_MCP_BEARER_TOKEN" in payload["config_snippets"]["http_oauth_toml"]


def test_issue_browser_extension_token_returns_scoped_public_token() -> None:
    client_id = uuid.uuid4()
    session = FakeSession(execute_results=[[{"id": client_id}], [], []])
    client = _build_app(session)

    response = client.post(
        "/api/v1/palace/browser-extension-tokens",
        json={
            "display_name": "Palace Capture Extension",
            "extension_version": "0.1.0",
            "token_ttl_seconds": 3600,
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["token_type"] == "Bearer"
    assert payload["scope"] == "capture:write capture:job:read"
    assert payload["tenant_id"] == "tenant-a"
    assert payload["client_key"].startswith("browser-extension:")
    assert isinstance(payload["access_token"], str)


def test_revoke_palace_mcp_client_scopes_to_current_tenant() -> None:
    client_id = uuid.uuid4()
    revoked_at = datetime.now(timezone.utc)
    session = FakeSession(
        execute_results=[
            [
                {
                    "id": client_id,
                    "tenant_id": "tenant-a",
                    "client_key": "codex-remote",
                    "display_name": "Codex remote MCP",
                    "allowed_scopes": ["read"],
                    "metadata": {},
                    "oauth_revoked_at": revoked_at,
                    "oauth_token_ttl_seconds": 3600,
                    "created_at": datetime.now(timezone.utc),
                    "last_seen_at": None,
                }
            ],
            [],
        ]
    )
    client = _build_app(session)

    response = client.post(f"/api/v1/palace/mcp-clients/{client_id}/revoke")

    assert response.status_code == 200
    payload = response.json()
    assert payload["revoked"] is True
    assert payload["client"]["revoked_at"] is not None


def test_palace_facts_endpoint_returns_fact_registry_rows(monkeypatch) -> None:
    client = _build_app(FakeSession())
    fact_id = uuid.uuid4()
    source_item_id = uuid.uuid4()

    async def fake_list_temporal_facts(db, *, tenant_id: str, current_only: bool, limit: int):
        assert tenant_id == "tenant-a"
        assert current_only is True
        assert limit == 10
        return [
            {
                "id": fact_id,
                "source_item_id": source_item_id,
                "source_item_title": "Investor notes",
                "subject": "Launch plan",
                "predicate": "targets",
                "object_text": "May 2026 rollout",
                "confidence": 1.0,
                "status": "active",
                "valid_from": None,
                "valid_to": None,
                "extracted_at": datetime.now(timezone.utc),
                "superseded_at": None,
            }
        ]

    monkeypatch.setattr("app.api.palace.list_temporal_facts", fake_list_temporal_facts)

    response = client.get("/api/v1/palace/facts?limit=10")

    assert response.status_code == 200
    payload = response.json()
    assert payload[0]["subject"] == "Launch plan"
    assert payload[0]["source_item_title"] == "Investor notes"


def test_patch_sync_source_rotates_credential_without_returning_secret(monkeypatch) -> None:
    source_id = uuid.uuid4()
    session = FakeSession(
        objects={
            (SyncSource, source_id): SyncSource(
                id=source_id,
                tenant_id="tenant-a",
                name="Private repo",
                root_path="https://github.com/palaceoftruth/palaceoftruth",
                source_kind="repo",
                credential_type="github_pat",
                credential_ciphertext="encrypted-old",
                status="active",
                scan_interval_seconds=900,
                allowed_extensions=[".md"],
            )
        }
    )
    client = _build_app(session)

    async def fake_update_sync_source(db, *, tenant_id: str, source, body):
        assert tenant_id == "tenant-a"
        assert source.id == source_id
        assert body.credential_type == "deployment_github_pat"
        assert body.github_pat is None
        source.credential_type = "deployment_github_pat"
        source.credential_ciphertext = None
        source.scan_interval_seconds = 1800
        return source

    monkeypatch.setattr("app.api.palace.update_sync_source", fake_update_sync_source)

    response = client.patch(
        f"/api/v1/palace/sync-sources/{source_id}",
        json={
            "credential_type": "deployment_github_pat",
            "scan_interval_seconds": 1800,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["credential_type"] == "deployment_github_pat"
    assert payload["has_stored_credential"] is False
    assert "github_pat" not in payload


def test_delete_sync_source_enqueues_palace_cleanup_when_items_are_deactivated(monkeypatch) -> None:
    source_id = uuid.uuid4()
    palace_run_id = uuid.uuid4()
    session = FakeSession(
        objects={
            (SyncSource, source_id): SyncSource(
                id=source_id,
                tenant_id="tenant-a",
                name="Repo",
                root_path="https://github.com/palaceoftruth/palaceoftruth",
                source_kind="repo",
                status="active",
                scan_interval_seconds=900,
            )
        }
    )
    client = _build_app(session)

    async def fake_delete_sync_source(db, *, tenant_id: str, source, actor_type: str, actor_id: str | None):
        assert tenant_id == "tenant-a"
        assert source.id == source_id
        assert actor_type == "api_key"
        assert actor_id == "key-hash"
        source.status = "disabled"
        return 3

    async def fake_create_or_get_palace_run(db, *, tenant_id: str, triggered_by: str, source_sync_run_id=None):
        assert tenant_id == "tenant-a"
        assert triggered_by == "source-delete"
        return (
            PalaceRun(
                id=palace_run_id,
                tenant_id=tenant_id,
                status="queued",
                requested_generation=7,
                applied_generation=0,
                attempt=1,
                started_at=datetime.now(timezone.utc),
            ),
            True,
        )

    monkeypatch.setattr("app.api.palace.delete_sync_source", fake_delete_sync_source)
    monkeypatch.setattr("app.api.palace.create_or_get_palace_run", fake_create_or_get_palace_run)

    response = client.delete(f"/api/v1/palace/sync-sources/{source_id}")

    assert response.status_code == 200
    assert response.json() == {
        "deleted": True,
        "items_deactivated": 3,
        "sync_source_id": str(source_id),
        "sync_source_name": "Repo",
        "status": "disabled",
    }
    assert client.app.state.arq_pool.enqueued == [
        ("palace_run_build", {"_queue_name": PALACE_WORKER_QUEUE, "palace_run_id": str(palace_run_id)})
    ]


def test_delete_sync_source_disables_source_and_records_audit_event(monkeypatch) -> None:
    from app.services import palace as palace_service

    source_id = uuid.uuid4()
    item_id = uuid.uuid4()
    source = SyncSource(
        id=source_id,
        tenant_id="tenant-a",
        name="Repo",
        root_path="https://github.com/palaceoftruth/palaceoftruth",
        source_kind="repo",
        status="active",
        scan_interval_seconds=900,
    )
    item = Item(
        id=item_id,
        tenant_id="tenant-a",
        source_type="note",
        source_url="https://example.test/doc",
        title="Synced doc",
        raw_content="content",
        summary=None,
        tags=[],
        categories=[],
        status="ready",
        metadata_={"sync_source_id": str(source_id), "sync_active": True},
    )
    session = FakeSession(
        objects={(SyncSource, source_id): source, (Item, item_id): item},
        execute_results=[[item]],
    )
    dirty_items: list[uuid.UUID] = []

    async def fake_mark_item_dirty(db, *, tenant_id: str, item_id: uuid.UUID, reason: str, sync_source_id: uuid.UUID):
        assert tenant_id == "tenant-a"
        assert reason == "sync-source-delete"
        assert sync_source_id == source_id
        dirty_items.append(item_id)

    monkeypatch.setattr(palace_service, "mark_item_dirty", fake_mark_item_dirty)

    count = asyncio.run(
        palace_service.delete_sync_source(
            session,
            tenant_id="tenant-a",
            source=source,
            actor_type="api_key",
            actor_id="key-hash",
        )
    )

    event = next(obj for obj in session.added if isinstance(obj, PalaceRoomEvent))
    assert count == 1
    assert source.status == "disabled"
    assert source.disabled_at is not None
    assert source.disabled_by == "key-hash"
    assert source.disabled_reason == "sync-source-removal"
    assert item.status == "failed"
    assert item.metadata_["sync_active"] is False
    assert item.metadata_["sync_source_deleted"] is True
    assert dirty_items == [item_id]
    assert event.event_type == "sync-source-disabled"
    assert event.payload["sync_source_id"] == str(source_id)
    assert event.payload["items_deactivated"] == 1
    assert event.payload["actor_type"] == "api_key"
    assert event.payload["actor_id"] == "key-hash"


def test_get_sync_sources_excludes_disabled_sources_unless_requested(monkeypatch) -> None:
    calls: list[bool] = []
    source_id = uuid.uuid4()

    async def fake_list_sync_sources(db, tenant_id: str, *, include_disabled: bool = False):
        calls.append(include_disabled)
        return [
            SyncSourceSummary(
                id=source_id,
                name="Repo",
                root_path="/tmp/repo",
                source_kind="repo",
                status="active",
                scan_interval_seconds=900,
            )
        ]

    monkeypatch.setattr("app.api.palace.list_sync_sources", fake_list_sync_sources)
    client = _build_app(FakeSession())

    response = client.get("/api/v1/palace/sync-sources")
    assert response.status_code == 200
    assert calls[-1] is False

    response = client.get("/api/v1/palace/sync-sources?include_disabled=true")
    assert response.status_code == 200
    assert calls[-1] is True


def test_restore_sync_source_reactivates_source_and_records_event() -> None:
    from app.services import palace as palace_service

    source_id = uuid.uuid4()
    source = SyncSource(
        id=source_id,
        tenant_id="tenant-a",
        name="Repo",
        root_path="/tmp/repo",
        source_kind="repo",
        status="disabled",
        disabled_at=datetime.now(timezone.utc),
        disabled_by="old-key",
        disabled_reason="sync-source-removal",
        last_error="Sync source disabled",
        scan_interval_seconds=900,
    )
    session = FakeSession(objects={(SyncSource, source_id): source})

    restored = asyncio.run(
        palace_service.restore_sync_source(
            session,
            tenant_id="tenant-a",
            source=source,
            actor_type="api_key",
            actor_id="key-hash",
        )
    )

    event = next(obj for obj in session.added if isinstance(obj, PalaceRoomEvent))
    assert restored is source
    assert source.status == "active"
    assert source.disabled_at is None
    assert source.disabled_by is None
    assert source.disabled_reason is None
    assert source.last_error is None
    assert event.event_type == "sync-source-restored"
    assert event.payload["sync_source_id"] == str(source_id)
    assert event.payload["actor_type"] == "api_key"
    assert event.payload["actor_id"] == "key-hash"


def test_start_palace_run_coalesces_existing_run(monkeypatch) -> None:
    client = _build_app(FakeSession())
    run_id = uuid.uuid4()

    async def fake_create_or_get_palace_run(db, *, tenant_id: str, triggered_by: str):
      return PalaceRun(id=run_id, tenant_id=tenant_id, status="queued", requested_generation=4, applied_generation=0, attempt=1, started_at=datetime.now(timezone.utc)), False

    async def fake_list_palace_runs(db, tenant_id: str, *, limit: int = 20):
        return [
            PalaceRunSummary(
                id=run_id,
                status="queued",
                triggered_by="manual",
                requested_generation=4,
                applied_generation=0,
                attempt=1,
                started_at=datetime.now(timezone.utc),
                completed_at=None,
            )
        ]

    monkeypatch.setattr("app.api.palace.create_or_get_palace_run", fake_create_or_get_palace_run)
    monkeypatch.setattr("app.api.palace.list_palace_runs", fake_list_palace_runs)

    response = client.post("/api/v1/palace/runs")

    assert response.status_code == 202
    assert response.json()["id"] == str(run_id)
    assert client.app.state.arq_pool.enqueued == []


def test_start_sync_source_enqueues_new_run(monkeypatch) -> None:
    source_id = uuid.uuid4()
    session = FakeSession(
        objects={
            (SyncSource, source_id): SyncSource(
                id=source_id,
                tenant_id="tenant-a",
                name="Repo",
                root_path="/tmp/repo",
                source_kind="repo",
                status="active",
                scan_interval_seconds=900,
            )
        }
    )
    client = _build_app(session)
    run_id = uuid.uuid4()

    async def fake_create_or_get_sync_run(db, *, tenant_id: str, source, triggered_by: str):
        return (
            SimpleNamespace(
                id=run_id,
                sync_source_id=source.id,
            ),
            True,
        )

    async def fake_list_sync_runs(db, tenant_id: str, *, limit: int = 20):
        return [
            SyncRunSummary(
                id=run_id,
                sync_source_id=source_id,
                sync_source_name="Repo",
                status="queued",
                triggered_by="manual",
                files_seen=0,
                files_changed=0,
                files_skipped=0,
                items_created=0,
                items_updated=0,
                items_failed=0,
                generation=0,
                started_at=datetime.now(timezone.utc),
                completed_at=None,
            )
        ]

    monkeypatch.setattr("app.api.palace.create_or_get_sync_run", fake_create_or_get_sync_run)
    monkeypatch.setattr("app.api.palace.list_sync_runs", fake_list_sync_runs)

    response = client.post(f"/api/v1/palace/sync-sources/{source_id}/sync")

    assert response.status_code == 202
    assert client.app.state.arq_pool.enqueued == [
        ("run_sync_source", {"_queue_name": PALACE_WORKER_QUEUE, "sync_run_id": str(run_id)})
    ]


def test_start_sync_source_rejects_disabled_source() -> None:
    source_id = uuid.uuid4()
    session = FakeSession(
        objects={
            (SyncSource, source_id): SyncSource(
                id=source_id,
                tenant_id="tenant-a",
                name="Repo",
                root_path="/tmp/repo",
                source_kind="repo",
                status="disabled",
                scan_interval_seconds=900,
            )
        }
    )
    client = _build_app(session)

    response = client.post(f"/api/v1/palace/sync-sources/{source_id}/sync")

    assert response.status_code == 409
    assert response.json()["detail"] == "Sync source is disabled"
    assert client.app.state.arq_pool.enqueued == []


def test_start_sync_source_can_run_inline(monkeypatch) -> None:
    source_id = uuid.uuid4()
    session = FakeSession(
        objects={
            (SyncSource, source_id): SyncSource(
                id=source_id,
                tenant_id="tenant-a",
                name="Vault",
                root_path="/mnt/palace-sync",
                source_kind="folder",
                status="active",
                scan_interval_seconds=900,
            )
        }
    )
    client = _build_app(session)
    run_id = uuid.uuid4()
    scheduled: list[str] = []

    async def fake_create_or_get_sync_run(db, *, tenant_id: str, source, triggered_by: str):
        return (SimpleNamespace(id=run_id, sync_source_id=source.id), True)

    async def fake_list_sync_runs(db, tenant_id: str, *, limit: int = 20):
        return [
            SyncRunSummary(
                id=run_id,
                sync_source_id=source_id,
                sync_source_name="Vault",
                status="queued",
                triggered_by="manual",
                files_seen=0,
                files_changed=0,
                files_skipped=0,
                items_created=0,
                items_updated=0,
                items_failed=0,
                generation=0,
                started_at=datetime.now(timezone.utc),
                completed_at=None,
            )
        ]

    def fake_add_task(_self, fn, app, run_id):
        scheduled.append("created")
        assert fn.__name__ == "_run_sync_inline"
        assert str(run_id) == str(run_id)

    monkeypatch.setattr("app.api.palace.create_or_get_sync_run", fake_create_or_get_sync_run)
    monkeypatch.setattr("app.api.palace.list_sync_runs", fake_list_sync_runs)
    monkeypatch.setattr("app.api.palace.BackgroundTasks.add_task", fake_add_task)

    response = client.post(f"/api/v1/palace/sync-sources/{source_id}/sync?run_inline=true")

    assert response.status_code == 202
    assert scheduled == ["created"]
    assert client.app.state.arq_pool.enqueued == []


def test_start_sync_source_can_inline_existing_queued_run(monkeypatch) -> None:
    source_id = uuid.uuid4()
    run_id = uuid.uuid4()
    session = FakeSession(
        objects={
            (SyncSource, source_id): SyncSource(
                id=source_id,
                tenant_id="tenant-a",
                name="Vault",
                root_path="/mnt/palace-sync",
                source_kind="folder",
                status="active",
                scan_interval_seconds=900,
            )
        }
    )
    client = _build_app(session)
    scheduled: list[str] = []

    async def fake_create_or_get_sync_run(db, *, tenant_id: str, source, triggered_by: str):
        return (
            SimpleNamespace(
                id=run_id,
                sync_source_id=source.id,
                status="queued",
            ),
            False,
        )

    async def fake_list_sync_runs(db, tenant_id: str, *, limit: int = 20):
        return [
            SyncRunSummary(
                id=run_id,
                sync_source_id=source_id,
                sync_source_name="Vault",
                status="queued",
                triggered_by="manual",
                files_seen=0,
                files_changed=0,
                files_skipped=0,
                items_created=0,
                items_updated=0,
                items_failed=0,
                generation=0,
                started_at=datetime.now(timezone.utc),
                completed_at=None,
            )
        ]

    def fake_add_task(_self, fn, app, scheduled_run_id):
        scheduled.append("created")
        assert fn.__name__ == "_run_sync_inline"
        assert scheduled_run_id == run_id

    monkeypatch.setattr("app.api.palace.create_or_get_sync_run", fake_create_or_get_sync_run)
    monkeypatch.setattr("app.api.palace.list_sync_runs", fake_list_sync_runs)
    monkeypatch.setattr("app.api.palace.BackgroundTasks.add_task", fake_add_task)

    response = client.post(f"/api/v1/palace/sync-sources/{source_id}/sync?run_inline=true")

    assert response.status_code == 202
    assert scheduled == ["created"]
    assert client.app.state.arq_pool.enqueued == []


def test_palace_retrieve_accepts_visible_scope_fields(monkeypatch) -> None:
    client = _build_app(FakeSession())

    async def fake_retrieve_palace(db, *, tenant_id: str, embedder, body, query_vector=None):
        assert tenant_id == "tenant-a"
        assert body.scope_type == "workspace"
        assert body.scope_key == "launch-pad"
        return PalaceRetrieveResponse(
            routed_room_id=None,
            redirected_from_room_id=None,
            trace=PalaceRetrieveTrace(
                requested_scope_type="workspace",
                requested_scope_key="launch-pad",
                selected_wing="Product / Growth",
                candidate_rooms=["Launch Briefs"],
                expanded_rooms=[],
                fallback_used=False,
                completeness_warning=None,
                steps=[],
            ),
            results=[
                SearchResult(
                    item_id=uuid.uuid4(),
                    title="Shared launch brief",
                    summary="Cross-host workspace context",
                    source_type="note",
                    source_url=None,
                    tags=["launch"],
                    created_at=datetime.now(timezone.utc),
                    chunk_text="Agents should reuse the same launch brief when they migrate hosts.",
                    chunk_index=0,
                    score=0.91,
                )
            ],
            total=1,
        )

    monkeypatch.setattr("app.api.palace.retrieve_palace", fake_retrieve_palace)

    response = client.post(
        "/api/v1/palace/retrieve",
        json={
            "query": "launch brief",
            "scope_type": "workspace",
            "scope_key": "launch-pad",
        },
    )

    assert response.status_code == 200
    assert response.json()["trace"]["requested_scope_type"] == "workspace"
    assert response.json()["total"] == 1


def test_patch_palace_room_renames_room(monkeypatch) -> None:
    client = _build_app(FakeSession())
    room_id = uuid.uuid4()
    wing_id = uuid.uuid4()

    async def fake_update_room(db, *, tenant_id: str, room_id: uuid.UUID, body):
        assert tenant_id == "tenant-a"
        assert body.name == "Investor Diligence"
        return PalaceRoomDetail(
            room=PalaceRoomSummary(
                id=room_id,
                wing_id=wing_id,
                name=body.name,
                stable_key="product-growth:pricing-narrative",
                state="active",
                item_count=4,
                summary="Updated room label for diligence notes.",
                membership_status=_freshness(),
                snapshot_status=_freshness(),
                tunnel_status=_freshness(),
                redirect_room_id=None,
            ),
            wing_name="Product / Growth",
        )

    monkeypatch.setattr("app.api.palace.update_room", fake_update_room)

    response = client.patch(
        f"/api/v1/palace/rooms/{room_id}",
        json={"name": "  Investor Diligence  "},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["room"]["id"] == str(room_id)
    assert payload["room"]["name"] == "Investor Diligence"
    assert payload["room"]["stable_key"] == "product-growth:pricing-narrative"


def test_update_room_preserves_stable_key_and_records_event(monkeypatch) -> None:
    room_id = uuid.uuid4()
    wing_id = uuid.uuid4()
    session = FakeSession(
        objects={
            (Room, room_id): Room(
                id=room_id,
                tenant_id="tenant-a",
                wing_id=wing_id,
                slug="pricing-narrative",
                stable_key="product-growth:pricing-narrative",
                name="Pricing Narrative",
                state="active",
            )
        }
    )

    async def fake_get_room_detail(db, tenant_id: str, room_id: uuid.UUID):
        room = await db.get(Room, room_id)
        return PalaceRoomDetail(
            room=PalaceRoomSummary(
                id=room.id,
                wing_id=room.wing_id,
                name=room.name,
                stable_key=room.stable_key,
                state=room.state,
                item_count=0,
                summary=None,
                membership_status=_freshness(),
                snapshot_status=_freshness(),
                tunnel_status=_freshness(),
                redirect_room_id=None,
            ),
            wing_name="Product / Growth",
        )

    monkeypatch.setattr("app.services.palace.get_room_detail", fake_get_room_detail)

    from app.services.palace import update_room

    detail = asyncio.run(
        update_room(
            session,
            tenant_id="tenant-a",
            room_id=room_id,
            body=PalaceRoomUpdate(name="Investor Diligence"),
        )
    )

    room = session.objects[(Room, room_id)]
    event = next(obj for obj in session.added if isinstance(obj, PalaceRoomEvent))
    assert detail.room.name == "Investor Diligence"
    assert detail.room.stable_key == "product-growth:pricing-narrative"
    assert room.slug == "investor-diligence"
    assert event.event_type == "rename"
    assert event.payload == {
        "old_name": "Pricing Narrative",
        "new_name": "Investor Diligence",
        "stable_key": "product-growth:pricing-narrative",
    }
