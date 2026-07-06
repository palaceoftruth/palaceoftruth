import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.curation_artifacts import router
from app.auth import AuthContext, verify_memory_auth
from app.database import get_db
from app.models.palace import CandidateCurationArtifact, CandidateCurationArtifactEvent


class FakeScalarResult:
    def __init__(self, rows) -> None:
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


class FakeSession:
    def __init__(self, *, artifacts=None) -> None:
        self.artifacts = artifacts or {}
        self.events = []
        self.statements = []
        self.commits = 0
        self.rollbacks = 0

    def add(self, obj):
        if isinstance(obj, CandidateCurationArtifact):
            if obj.id is None:
                obj.id = uuid.uuid4()
            self.artifacts[obj.id] = obj
        if isinstance(obj, CandidateCurationArtifactEvent):
            if obj.id is None:
                obj.id = uuid.uuid4()
            self.events.append(obj)

    async def flush(self):
        now = datetime.now(timezone.utc)
        for artifact in self.artifacts.values():
            if artifact.id is None:
                artifact.id = uuid.uuid4()
            if artifact.created_at is None:
                artifact.created_at = now
            if artifact.updated_at is None:
                artifact.updated_at = now
        for event in self.events:
            if event.created_at is None:
                event.created_at = now

    async def get(self, model, key):
        if model is CandidateCurationArtifact:
            return self.artifacts.get(key)
        return None

    async def execute(self, statement):
        self.statements.append(str(statement))
        return FakeScalarResult(list(self.artifacts.values()))

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _artifact(*, tenant_id: str = "tenant-a", status: str = "draft") -> CandidateCurationArtifact:
    now = datetime.now(timezone.utc)
    return CandidateCurationArtifact(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        artifact_kind="candidate_skill",
        target_runtime="codex",
        target_surface="skills/codex-pm-tasks",
        status=status,
        source_item_ids=["SAR-512"],
        source_digests={"SAR-512": "sha256:test"},
        candidate_body="Use the task pool helper for durable PM task writes.",
        privacy_review={
            "safe_for_review": True,
            "raw_sensitive_content_excluded": True,
            "contains_sensitive_content": False,
        },
        eval_summary={
            "compatibility": {
                "passed": True,
                "transport_results": {
                    "codex": "pass",
                    "hermes": "pass",
                    "rest": "pass",
                    "mcp": "pass",
                },
            },
            "interference": {
                "overrides_newer_guidance": False,
                "overrides_more_specific_guidance": False,
            },
            "regression_cases": [{"case_id": "preserve-human-promotion-approval", "passed": True}],
        },
        approval={
            "approved_by": "codex-review",
            "approved_at": "2026-05-21T19:30:00Z",
            "decision": "approved",
            "promotion_target": "codex skill PR",
        }
        if status in {"approved", "promoted"}
        else {},
        metadata_={"created_from": "test"},
        created_at=now,
        updated_at=now,
    )


def _payload() -> dict:
    return {
        "artifact_kind": "candidate_skill",
        "target_runtime": "codex",
        "target_surface": "skills/codex-pm-tasks",
        "status": "draft",
        "source_item_ids": ["SAR-512", "PR-999"],
        "source_digests": {"candidate_body": "sha256:test", "evidence": "sha256:evidence"},
        "candidate_body": "Use the project-manager helper for task writes.",
        "privacy_review": {
            "safe_for_review": True,
            "raw_sensitive_content_excluded": True,
            "contains_sensitive_content": False,
        },
        "eval_summary": {"evidence_coverage": 1.0, "failure_case_ids": []},
        "approval": {},
        "metadata": {"source": "dotodo"},
    }


def _client(session: FakeSession, *, tenant_id: str = "tenant-a") -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    async def override_get_db():
        yield session

    async def override_verify(request: Request):
        request.state.auth_context = AuthContext(tenant_id=tenant_id, auth_mode="api_key", token_hash_reference="key-hash")
        request.state.tenant_id = tenant_id
        request.state.key_hash = "key-hash"
        request.state.auth_mode = "api_key"
        return "raw-key"

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[verify_memory_auth] = override_verify
    return TestClient(app)


def test_create_candidate_curation_artifact_stores_sanitized_tenant_scoped_payload() -> None:
    session = FakeSession()
    client = _client(session)

    response = client.post("/api/v1/curation-artifacts", json=_payload())

    assert response.status_code == 201
    body = response.json()
    assert body["tenant_id"] == "tenant-a"
    assert body["artifact_kind"] == "candidate_skill"
    assert body["status"] == "draft"
    assert body["source_item_ids"] == ["SAR-512", "PR-999"]
    assert body["metadata"] == {"source": "dotodo"}
    assert session.commits == 1
    assert len(session.events) == 1
    assert session.events[0].event_type == "created"
    assert session.events[0].previous_snapshot is None
    assert session.events[0].next_snapshot["status"] == "draft"


def test_create_no_source_generated_insight_remains_advisory_and_needs_source() -> None:
    session = FakeSession()
    client = _client(session)
    payload = _payload()
    payload["status"] = "needs_source"
    payload["source_item_ids"] = []
    payload["source_digests"] = {}

    response = client.post("/api/v1/curation-artifacts", json=payload)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "needs_source"
    assert body["promotion_state"] == "needs_source"
    assert body["source_support_level"] == "no_source"
    assert body["advisory_generated_context"] is True
    assert body["promoted_source_backed"] is False
    assert session.events[0].next_snapshot["status"] == "needs_source"


def test_create_reviewable_generated_insight_accepts_single_source_support() -> None:
    session = FakeSession()
    client = _client(session)
    payload = _payload()
    payload["status"] = "reviewable"
    payload["source_item_ids"] = ["item-1"]
    payload["source_digests"] = {"item-1": "sha256:item-1"}

    response = client.post("/api/v1/curation-artifacts", json=payload)

    assert response.status_code == 201
    body = response.json()
    assert body["promotion_state"] == "reviewable"
    assert body["source_support_level"] == "single_source"
    assert body["advisory_generated_context"] is True


def test_promoted_generated_insight_requires_source_support() -> None:
    artifact = _artifact(tenant_id="tenant-a", status="reviewable")
    artifact.source_item_ids = []
    artifact.source_digests = {}
    session = FakeSession(artifacts={artifact.id: artifact})
    client = _client(session)

    response = client.patch(
        f"/api/v1/curation-artifacts/{artifact.id}",
        json={
            "status": "promoted",
            "approval": {
                "approved_by": "codex-review",
                "approved_at": "2026-06-20T17:30:00Z",
                "decision": "approved",
                "promotion_target": "codex skill PR",
            },
        },
    )

    assert response.status_code == 422
    assert "source_item_ids and source_digests" in response.json()["detail"]
    assert session.commits == 0
    assert session.rollbacks == 1


def test_promoted_generated_insight_blocks_conflicting_sources() -> None:
    artifact = _artifact(tenant_id="tenant-a", status="reviewable")
    artifact.metadata_ = {"source_conflicts": True}
    session = FakeSession(artifacts={artifact.id: artifact})
    client = _client(session)

    response = client.patch(
        f"/api/v1/curation-artifacts/{artifact.id}",
        json={
            "status": "promoted",
            "approval": {
                "approved_by": "codex-review",
                "approved_at": "2026-06-20T17:30:00Z",
                "decision": "approved",
                "promotion_target": "codex skill PR",
            },
        },
    )

    assert response.status_code == 422
    assert "conflict-free source evidence" in response.json()["detail"]
    assert session.rollbacks == 1


def test_promoted_generated_insight_blocks_stale_sources() -> None:
    artifact = _artifact(tenant_id="tenant-a", status="reviewable")
    artifact.metadata_ = {"source_evidence_stale": True}
    session = FakeSession(artifacts={artifact.id: artifact})
    client = _client(session)

    response = client.patch(
        f"/api/v1/curation-artifacts/{artifact.id}",
        json={
            "status": "promoted",
            "approval": {
                "approved_by": "codex-review",
                "approved_at": "2026-06-20T17:30:00Z",
                "decision": "approved",
                "promotion_target": "codex skill PR",
            },
        },
    )

    assert response.status_code == 422
    assert "fresh source evidence" in response.json()["detail"]
    assert session.rollbacks == 1


def test_promoted_generated_insight_requires_digest_for_each_source() -> None:
    artifact = _artifact(tenant_id="tenant-a", status="reviewable")
    artifact.source_item_ids = ["item-a"]
    artifact.source_digests = {"unrelated": "sha256:unrelated"}
    session = FakeSession(artifacts={artifact.id: artifact})
    client = _client(session)

    response = client.patch(
        f"/api/v1/curation-artifacts/{artifact.id}",
        json={
            "status": "promoted",
            "approval": {
                "approved_by": "codex-review",
                "approved_at": "2026-06-20T17:30:00Z",
                "decision": "approved",
                "promotion_target": "codex skill PR",
            },
        },
    )

    assert response.status_code == 422
    assert "for each source_item_id" in response.json()["detail"]
    assert session.rollbacks == 1


def test_create_candidate_curation_artifact_rejects_private_or_secret_body() -> None:
    session = FakeSession()
    client = _client(session)
    payload = _payload()
    payload["candidate_body"] = "password=super-secret"

    response = client.post("/api/v1/curation-artifacts", json=payload)

    assert response.status_code == 422
    assert "sanitized" in response.json()["detail"]
    assert session.commits == 0
    assert session.rollbacks == 1


def test_create_candidate_curation_artifact_requires_explicit_privacy_gate() -> None:
    session = FakeSession()
    client = _client(session)
    payload = _payload()
    payload["privacy_review"] = {"reviewed_by": "codex"}

    response = client.post("/api/v1/curation-artifacts", json=payload)

    assert response.status_code == 422
    assert "safe_for_review" in response.json()["detail"]
    assert session.commits == 0
    assert session.rollbacks == 1


def test_create_candidate_curation_artifact_rejects_cross_tenant_supersedes_lineage() -> None:
    other = _artifact(tenant_id="tenant-b")
    session = FakeSession(artifacts={other.id: other})
    client = _client(session)
    payload = _payload()
    payload["supersedes_artifact_id"] = str(other.id)

    response = client.post("/api/v1/curation-artifacts", json=payload)

    assert response.status_code == 422
    assert response.json()["detail"] == "lineage target artifact not found"
    assert session.commits == 0
    assert session.rollbacks == 1


def test_list_candidate_curation_artifacts_scopes_query_by_tenant_and_status() -> None:
    artifact = _artifact(tenant_id="tenant-a", status="proposed")
    session = FakeSession(artifacts={artifact.id: artifact})
    client = _client(session)

    response = client.get("/api/v1/curation-artifacts?status=proposed&limit=10")

    assert response.status_code == 200
    assert response.json()["total"] == 1
    normalized_sql = " ".join(session.statements[0].lower().split())
    assert "candidate_curation_artifacts.tenant_id = :tenant_id_1" in normalized_sql
    assert "candidate_curation_artifacts.status = :status_1" in normalized_sql


def test_review_inbox_lists_existing_generated_artifacts_with_triage_context() -> None:
    source_ready = _artifact(tenant_id="tenant-a", status="reviewable")
    source_ready.eval_summary = {"confidence": 0.82}
    needs_source = _artifact(tenant_id="tenant-a", status="needs_source")
    needs_source.source_item_ids = []
    needs_source.source_digests = {}
    session = FakeSession(artifacts={source_ready.id: source_ready, needs_source.id: needs_source})
    client = _client(session)

    response = client.get("/api/v1/curation-artifacts/review-inbox")

    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["total"] == 2
    assert body["summary"]["needs_source"] == 1
    reviewable = next(item for item in body["items"] if item["artifact"]["id"] == str(source_ready.id))
    assert reviewable["suggested_action"] == "accept"
    assert reviewable["confidence"] == 0.82
    assert reviewable["source_count"] == 1
    assert reviewable["affected_scope"] == "codex:skills/codex-pm-tasks"
    assert reviewable["reversible_actions"] == ["pin", "defer"]


def test_review_inbox_filters_deferred_candidates_before_limit() -> None:
    artifact = _artifact(tenant_id="tenant-a", status="reviewable")
    session = FakeSession(artifacts={artifact.id: artifact})
    client = _client(session)

    response = client.get("/api/v1/curation-artifacts/review-inbox?limit=10")

    assert response.status_code == 200
    normalized_sql = " ".join(session.statements[0].lower().split())
    assert "candidate_curation_artifacts.metadata" in normalized_sql
    assert "is not true" in normalized_sql
    assert "limit" in normalized_sql


def test_review_inbox_safe_batch_pin_updates_metadata_without_status_change() -> None:
    first = _artifact(tenant_id="tenant-a", status="reviewable")
    second = _artifact(tenant_id="tenant-a", status="proposed")
    session = FakeSession(artifacts={first.id: first, second.id: second})
    client = _client(session)

    response = client.post(
        "/api/v1/curation-artifacts/review-inbox/actions",
        json={
            "action": "pin",
            "artifact_ids": [str(first.id), str(second.id)],
            "actor": "operator-a",
            "note": "Keep visible for weekly review",
        },
    )

    assert response.status_code == 200
    assert response.json()["updated"] == 2
    assert first.status == "reviewable"
    assert second.status == "proposed"
    assert first.metadata_["review_inbox"]["pinned"] is True
    assert first.metadata_["review_inbox"]["last_actor"] == "operator-a"
    assert session.commits == 1
    assert len(session.events) == 2


def test_review_inbox_rejects_batch_accept_as_unsafe() -> None:
    first = _artifact(tenant_id="tenant-a", status="reviewable")
    second = _artifact(tenant_id="tenant-a", status="reviewable")
    client = _client(FakeSession(artifacts={first.id: first, second.id: second}))

    response = client.post(
        "/api/v1/curation-artifacts/review-inbox/actions",
        json={
            "action": "accept",
            "artifact_ids": [str(first.id), str(second.id)],
            "actor": "operator-a",
        },
    )

    assert response.status_code == 422
    assert "batch review inbox actions are limited to pin and defer" in response.text


def test_review_inbox_accept_promotes_only_source_backed_artifacts() -> None:
    artifact = _artifact(tenant_id="tenant-a", status="reviewable")
    session = FakeSession(artifacts={artifact.id: artifact})
    client = _client(session)

    response = client.post(
        "/api/v1/curation-artifacts/review-inbox/actions",
        json={
            "action": "accept",
            "artifact_ids": [str(artifact.id)],
            "actor": "operator-a",
            "note": "Evidence is sufficient",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["artifacts"][0]["status"] == "promoted"
    assert body["artifacts"][0]["approval"]["approved_by"] == "operator-a"
    assert body["artifacts"][0]["metadata"]["review_inbox"]["resolved"] is True
    assert artifact.approved_at is not None
    assert session.commits == 1


def test_review_inbox_accept_rejects_source_backed_draft() -> None:
    artifact = _artifact(tenant_id="tenant-a", status="draft")
    session = FakeSession(artifacts={artifact.id: artifact})
    client = _client(session)

    response = client.post(
        "/api/v1/curation-artifacts/review-inbox/actions",
        json={
            "action": "accept",
            "artifact_ids": [str(artifact.id)],
            "actor": "operator-a",
        },
    )

    assert response.status_code == 422
    assert "only reviewable or proposed" in response.json()["detail"]
    assert artifact.status == "draft"
    assert session.commits == 0
    assert session.rollbacks == 1


def test_review_inbox_accept_blocks_unsourced_artifact() -> None:
    artifact = _artifact(tenant_id="tenant-a", status="needs_source")
    artifact.source_item_ids = []
    artifact.source_digests = {}
    session = FakeSession(artifacts={artifact.id: artifact})
    client = _client(session)

    response = client.post(
        "/api/v1/curation-artifacts/review-inbox/actions",
        json={
            "action": "accept",
            "artifact_ids": [str(artifact.id)],
            "actor": "operator-a",
        },
    )

    assert response.status_code == 422
    assert "only reviewable or proposed" in response.json()["detail"]
    assert session.commits == 0
    assert session.rollbacks == 1


def test_get_candidate_curation_artifact_hides_other_tenant_artifact() -> None:
    artifact = _artifact(tenant_id="tenant-b")
    client = _client(FakeSession(artifacts={artifact.id: artifact}))

    response = client.get(f"/api/v1/curation-artifacts/{artifact.id}")

    assert response.status_code == 404


def test_patch_candidate_curation_artifact_updates_metadata_status_and_approval() -> None:
    artifact = _artifact(tenant_id="tenant-a", status="proposed")
    session = FakeSession(artifacts={artifact.id: artifact})
    client = _client(session)

    response = client.patch(
        f"/api/v1/curation-artifacts/{artifact.id}",
        json={
            "status": "approved",
            "approval": {
                "approved_by": "codex-review",
                "approved_at": "2026-05-21T19:30:00Z",
                "decision": "approved",
                "promotion_target": "codex skill PR",
            },
            "metadata": {"review": "passed"},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "approved"
    assert body["approval"]["approved_by"] == "codex-review"
    assert body["metadata"] == {"review": "passed"}
    assert artifact.approved_at is not None
    assert session.commits == 1
    assert len(session.events) == 1
    assert session.events[0].event_type == "updated"
    assert session.events[0].previous_status == "proposed"
    assert session.events[0].next_status == "approved"
    assert session.events[0].previous_snapshot["metadata"] == {"created_from": "test"}
    assert session.events[0].next_snapshot["metadata"] == {"review": "passed"}


def test_patch_candidate_curation_artifact_promotes_source_backed_generated_insight() -> None:
    artifact = _artifact(tenant_id="tenant-a", status="reviewable")
    session = FakeSession(artifacts={artifact.id: artifact})
    client = _client(session)

    response = client.patch(
        f"/api/v1/curation-artifacts/{artifact.id}",
        json={
            "status": "promoted",
            "approval": {
                "approved_by": "codex-review",
                "approved_at": "2026-06-20T17:30:00Z",
                "decision": "approved",
                "promotion_target": "codex skill PR",
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "promoted"
    assert body["promotion_state"] == "promoted"
    assert body["source_support_level"] == "single_source"
    assert body["advisory_generated_context"] is False
    assert body["promoted_source_backed"] is True
    assert artifact.approved_at is not None
    assert session.events[0].previous_status == "reviewable"
    assert session.events[0].next_status == "promoted"


def test_patch_candidate_curation_artifact_rejects_destructive_status() -> None:
    artifact = _artifact(tenant_id="tenant-a", status="draft")
    session = FakeSession(artifacts={artifact.id: artifact})
    client = _client(session)

    response = client.patch(f"/api/v1/curation-artifacts/{artifact.id}", json={"status": "deleted"})

    assert response.status_code == 422
    assert "append-only" in response.json()["detail"]
    assert session.commits == 0
    assert session.rollbacks == 1


def test_delete_candidate_curation_artifact_is_not_supported() -> None:
    artifact = _artifact(tenant_id="tenant-a")
    client = _client(FakeSession(artifacts={artifact.id: artifact}))

    response = client.delete(f"/api/v1/curation-artifacts/{artifact.id}")

    assert response.status_code == 405


def test_superseded_candidate_requires_replacement_lineage() -> None:
    artifact = _artifact(tenant_id="tenant-a", status="proposed")
    session = FakeSession(artifacts={artifact.id: artifact})
    client = _client(session)

    response = client.patch(f"/api/v1/curation-artifacts/{artifact.id}", json={"status": "superseded"})

    assert response.status_code == 422
    assert "superseded_by_artifact_id" in response.json()["detail"]
    assert session.rollbacks == 1


def test_superseded_candidate_rejects_cross_tenant_replacement_lineage() -> None:
    artifact = _artifact(tenant_id="tenant-a", status="proposed")
    other = _artifact(tenant_id="tenant-b", status="proposed")
    session = FakeSession(artifacts={artifact.id: artifact, other.id: other})
    client = _client(session)

    response = client.patch(
        f"/api/v1/curation-artifacts/{artifact.id}",
        json={"status": "superseded", "superseded_by_artifact_id": str(other.id)},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "lineage target artifact not found"
    assert session.rollbacks == 1


def test_promotion_handoff_renders_approved_candidate_without_mutating_artifact() -> None:
    artifact = _artifact(tenant_id="tenant-a", status="approved")
    session = FakeSession(artifacts={artifact.id: artifact})
    client = _client(session)

    response = client.get(f"/api/v1/curation-artifacts/{artifact.id}/promotion-handoff")

    assert response.status_code == 200
    body = response.json()
    assert body["artifact_id"] == str(artifact.id)
    assert body["target_runtime"] == "codex"
    assert body["promotion_target"] == "codex skill PR"
    assert body["gate_evidence"]["promotion_ready"] is True
    assert body["gate_evidence"]["mutating"] is False
    assert "Use the task pool helper" in body["rendered_handoff"]
    assert "Approval decision: approved" in body["rendered_handoff"]
    assert "Approved by: codex-review" in body["rendered_handoff"]
    assert "Do not apply this candidate automatically from Palace." in body["rendered_handoff"]
    assert session.commits == 0
    assert session.rollbacks == 0
    assert len(session.events) == 0


def test_promotion_handoff_blocks_unapproved_rejected_and_deprecated_candidates() -> None:
    for status in ("draft", "needs_source", "reviewable", "proposed", "rejected", "deprecated", "stale"):
        artifact = _artifact(tenant_id="tenant-a", status=status)
        client = _client(FakeSession(artifacts={artifact.id: artifact}))

        response = client.get(f"/api/v1/curation-artifacts/{artifact.id}/promotion-handoff")

        assert response.status_code == 422
        assert "only promoted" in response.json()["detail"]


def test_promotion_handoff_blocks_privacy_failed_approved_candidate() -> None:
    artifact = _artifact(tenant_id="tenant-a", status="approved")
    artifact.privacy_review = {
        "safe_for_review": False,
        "raw_sensitive_content_excluded": False,
        "contains_sensitive_content": True,
    }
    client = _client(FakeSession(artifacts={artifact.id: artifact}))

    response = client.get(f"/api/v1/curation-artifacts/{artifact.id}/promotion-handoff")

    assert response.status_code == 422
    assert "safe_for_review" in response.json()["detail"]


def test_promotion_handoff_blocks_score_failed_approved_candidate() -> None:
    artifact = _artifact(tenant_id="tenant-a", status="approved")
    artifact.eval_summary = {
        "compatibility": {"passed": False, "failed_transports": ["mcp"]},
        "regression_cases": [{"case_id": "mcp-rendering-stays-compatible", "passed": False}],
    }
    client = _client(FakeSession(artifacts={artifact.id: artifact}))

    response = client.get(f"/api/v1/curation-artifacts/{artifact.id}/promotion-handoff")

    assert response.status_code == 422
    assert "mcp-rendering-stays-compatible" in response.json()["detail"]


def test_promotion_handoff_blocks_approved_candidate_with_unmatched_source_digest() -> None:
    artifact = _artifact(tenant_id="tenant-a", status="approved")
    artifact.source_item_ids = ["item-a"]
    artifact.source_digests = {"unrelated": "sha256:unrelated"}
    client = _client(FakeSession(artifacts={artifact.id: artifact}))

    response = client.get(f"/api/v1/curation-artifacts/{artifact.id}/promotion-handoff")

    assert response.status_code == 422
    assert "for each source_item_id" in response.json()["detail"]


def test_promotion_handoff_blocks_malformed_score_evidence() -> None:
    artifact = _artifact(tenant_id="tenant-a", status="approved")
    artifact.eval_summary = {
        "compatibility": {
            "passed": True,
            "transport_results": {
                "codex": "pass",
                "hermes": "pass",
                "rest": "pass",
                "mcp": "pass",
            },
        },
        "regression_cases": ["bad"],
    }
    client = _client(FakeSession(artifacts={artifact.id: artifact}))

    response = client.get(f"/api/v1/curation-artifacts/{artifact.id}/promotion-handoff")

    assert response.status_code == 422
    assert "score evidence is invalid" in response.json()["detail"]


def test_promotion_handoff_requires_explicit_approved_decision_and_target() -> None:
    artifact = _artifact(tenant_id="tenant-a", status="approved")
    artifact.approval = {
        "approved_by": "codex-review",
        "approved_at": "2026-05-21T19:30:00Z",
        "decision": "rejected",
    }
    client = _client(FakeSession(artifacts={artifact.id: artifact}))

    response = client.get(f"/api/v1/curation-artifacts/{artifact.id}/promotion-handoff")

    assert response.status_code == 422
    assert "approval.decision" in response.json()["detail"]
