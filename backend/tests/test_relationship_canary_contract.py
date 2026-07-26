import json
import uuid
from types import SimpleNamespace

import pytest

from app.services import relationship_canary
from app.services.relationship_canary import _memory_request
from app.services.relationship_canary_contract import (
    EXPECTED_ALIASES,
    FIXTURE_SHA256,
    RelationshipCanaryContractError,
    build_plan,
    empirical_p95,
    load_validated_fixture,
)


def test_compiled_canary_plan_is_zero_write_and_exactly_bounded() -> None:
    plan = build_plan()

    assert plan["mode"] == "dry_run"
    assert plan["fixture_sha256"] == FIXTURE_SHA256
    assert plan["target"] == {
        "cluster": "k3s-lab",
        "namespace": "palace-sarvent",
        "tenant_id": "sar-1083-canary",
        "scope": {"type": "workspace", "key": "sar-1083-canary"},
    }
    assert plan["aliases"] == list(EXPECTED_ALIASES)
    assert plan["record_count"] == 6
    assert plan["pair_count"] == 3
    assert plan["classification_sample_count"] == 20
    assert plan["provider_call_ceiling"] == 40
    assert plan["mutations_performed"] == 0
    assert plan["safety"] == {
        "synthetic_only": True,
        "private_data_read": False,
        "bulk_rewrite": False,
        "forced_malformed": False,
        "deletes": 0,
        "cleanup": "retain",
    }


def test_fixture_tampering_fails_before_execution(tmp_path) -> None:
    fixture = load_validated_fixture()
    fixture["cases"][0]["records"].append(
        {"alias": "seventh", "title": "Not approved", "summary": "Not approved"}
    )
    tampered = tmp_path / "fixture.json"
    tampered.write_text(json.dumps(fixture))

    with pytest.raises(RelationshipCanaryContractError, match="digest"):
        load_validated_fixture(tampered)


def test_memory_requests_are_deterministic_skip_relationship_discovery_and_retain() -> None:
    fixture = load_validated_fixture()
    requests = [
        _memory_request(case["id"], record)
        for case in fixture["cases"]
        for record in case["records"]
    ]

    assert len({request.idempotency_key for request in requests}) == 6
    assert all(len(request.idempotency_key or "") == 64 for request in requests)
    assert all(request.tenant_id == "sar-1083-canary" for request in requests)
    assert all(request.scope.model_dump() == {"type": "workspace", "key": "sar-1083-canary"} for request in requests)
    assert all(request.relationship_policy == "skip" for request in requests)
    assert all(request.enable_ai_enrichment is False for request in requests)
    assert all(request.metadata["relationship_canary"]["retain"] is True for request in requests)


def test_empirical_p95_uses_nearest_rank_and_rejects_invalid_samples() -> None:
    assert empirical_p95([float(value) for value in range(1, 21)]) == 19.0
    assert empirical_p95([0.1, 0.2, 0.3]) == 0.3
    with pytest.raises(RelationshipCanaryContractError):
        empirical_p95([])
    with pytest.raises(RelationshipCanaryContractError):
        empirical_p95([0.1, float("nan")])
    with pytest.raises(RelationshipCanaryContractError):
        empirical_p95([-0.1])


class _ScalarRows:
    def scalars(self):
        return []


class _RunnerDB:
    def __init__(self):
        self.control_item = SimpleNamespace(metadata_={})

    async def execute(self, _statement):
        return _ScalarRows()

    async def get(self, _model, _key):
        return self.control_item

    async def commit(self):
        return None

    async def rollback(self):
        return None


@pytest.mark.asyncio
async def test_live_runner_classifies_only_the_three_fixture_pairs(monkeypatch) -> None:
    fixture = load_validated_fixture()
    alias_ids = {
        record["alias"]: uuid.uuid4()
        for case in fixture["cases"]
        for record in case["records"]
    }
    materialized_aliases = []

    async def fake_materialize(_db, *, request, embedder, llm):
        del embedder, llm
        alias = request.metadata["relationship_canary"]["record_alias"]
        materialized_aliases.append(alias)
        return {
            "alias": alias,
            "item_id": str(alias_ids[alias]),
            "job_id": str(uuid.uuid4()),
            "created": True,
            "replayed": False,
            "status": "ready",
        }

    pair_calls = []
    outcomes = iter(
        [
            *[
                SimpleNamespace(
                    relationship="related_to",
                    confidence=0.9,
                    validation_outcome="valid",
                    provider="openrouter",
                    retry_provider="openrouter",
                    fallback_used=False,
                    retry_count=0,
                    duration_seconds=0.1,
                    edge_persisted=index == 0,
                )
                for index in range(7)
            ],
            *[
                SimpleNamespace(
                    relationship="none",
                    confidence=0.0,
                    validation_outcome="empty",
                    provider="openrouter",
                    retry_provider="openrouter",
                    fallback_used=False,
                    retry_count=0,
                    duration_seconds=0.2,
                    edge_persisted=False,
                )
                for _ in range(7)
            ],
            *[
                SimpleNamespace(
                    relationship="none",
                    confidence=0.0,
                    validation_outcome="provider_error",
                    provider="openai",
                    retry_provider="openrouter",
                    fallback_used=True,
                    retry_count=1,
                    duration_seconds=0.3,
                    edge_persisted=False,
                )
                for _ in range(6)
            ],
        ]
    )

    class FakeRelationshipService:
        def __init__(self, *_args, **_kwargs):
            pass

        async def classify_candidate(self, source_id, target_id, **kwargs):
            pair_calls.append((source_id, target_id, kwargs))
            return next(outcomes)

    edge_counts = iter([0, 1, *([1, 1] * 6), *([0, 0] * 13)])
    monkeypatch.setattr(relationship_canary, "_materialize_record", fake_materialize)
    monkeypatch.setattr(relationship_canary, "_indexed_item_count", lambda *_args: _async_value(6))
    monkeypatch.setattr(relationship_canary, "_pair_edge_count", lambda *_args: _async_value(next(edge_counts)))
    monkeypatch.setattr(relationship_canary, "RelationshipService", FakeRelationshipService)
    monkeypatch.setattr(relationship_canary.settings, "app_version", "deadbeef")
    monkeypatch.setattr(relationship_canary.settings, "deployment_cluster", "k3s-lab")
    monkeypatch.setattr(relationship_canary.settings, "deployment_namespace", "palace-sarvent")
    monkeypatch.setattr(relationship_canary.settings, "sar1083_relationship_canary_enabled", True)
    monkeypatch.setattr(
        relationship_canary.settings,
        "sar1083_relationship_canary_authorization_id",
        "linear-comment:approval",
    )

    runner_db = _RunnerDB()
    report = await relationship_canary.run_live_canary(
        runner_db,
        embedder=object(),
        llm=object(),
        authorization_id="linear-comment:approval",
        expected_app_version="deadbeef",
    )

    assert materialized_aliases == list(EXPECTED_ALIASES)
    assert len(pair_calls) == 20
    assert pair_calls[0][2]["persist"] is True
    assert all(call[2]["persist"] is False for call in pair_calls[1:])
    assert report["passed"] is True
    assert report["record_count"] == 6
    assert report["latency"]["sample_count"] == 20
    assert report["latency"]["samples"] == [*([0.1] * 7), *([0.2] * 7), *([0.3] * 6)]
    assert report["latency"]["p95"] == 0.3
    assert runner_db.control_item.metadata_["sar1083_canary_run"]["status"] == "complete"


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_live_runner_rejects_wrong_deployment_before_database_access(monkeypatch) -> None:
    class NoDatabaseAccess:
        async def execute(self, *_args, **_kwargs):
            raise AssertionError("runtime gate reached the database")

    monkeypatch.setattr(relationship_canary.settings, "app_version", "deadbeef")
    monkeypatch.setattr(relationship_canary.settings, "deployment_cluster", "rke2-abby")
    monkeypatch.setattr(relationship_canary.settings, "deployment_namespace", "palace-sarvent")
    monkeypatch.setattr(relationship_canary.settings, "sar1083_relationship_canary_enabled", True)
    monkeypatch.setattr(
        relationship_canary.settings,
        "sar1083_relationship_canary_authorization_id",
        "linear-comment:approval",
    )

    with pytest.raises(RelationshipCanaryContractError, match="deployment identity"):
        await relationship_canary.run_live_canary(
            NoDatabaseAccess(),
            embedder=object(),
            llm=object(),
            authorization_id="linear-comment:approval",
            expected_app_version="deadbeef",
        )


@pytest.mark.asyncio
async def test_completed_live_runner_replay_performs_no_classification(monkeypatch) -> None:
    fixture = load_validated_fixture()
    alias_ids = {
        record["alias"]: uuid.uuid4()
        for case in fixture["cases"]
        for record in case["records"]
    }
    stored_report = {"task_id": "SAR-1083", "passed": True, "operations": [{"sample_index": 1}]}

    async def fake_materialize(_db, *, request, embedder, llm):
        del embedder, llm
        alias = request.metadata["relationship_canary"]["record_alias"]
        return {
            "alias": alias,
            "item_id": str(alias_ids[alias]),
            "job_id": str(uuid.uuid4()),
            "created": False,
            "replayed": True,
            "status": "ready",
        }

    class ReplayDB(_RunnerDB):
        def __init__(self):
            super().__init__()
            self.control_item.metadata_ = {
                "sar1083_canary_run": {
                    "status": "complete",
                    "report": stored_report,
                }
            }

    class NoRelationshipService:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("completed replay attempted relationship classification")

    monkeypatch.setattr(relationship_canary, "_materialize_record", fake_materialize)
    monkeypatch.setattr(relationship_canary, "_indexed_item_count", lambda *_args: _async_value(6))
    monkeypatch.setattr(relationship_canary, "RelationshipService", NoRelationshipService)
    monkeypatch.setattr(relationship_canary.settings, "app_version", "deadbeef")
    monkeypatch.setattr(relationship_canary.settings, "deployment_cluster", "k3s-lab")
    monkeypatch.setattr(relationship_canary.settings, "deployment_namespace", "palace-sarvent")
    monkeypatch.setattr(relationship_canary.settings, "sar1083_relationship_canary_enabled", True)
    monkeypatch.setattr(
        relationship_canary.settings,
        "sar1083_relationship_canary_authorization_id",
        "linear-comment:approval",
    )

    report = await relationship_canary.run_live_canary(
        ReplayDB(),
        embedder=object(),
        llm=object(),
        authorization_id="linear-comment:approval",
        expected_app_version="deadbeef",
    )

    assert report["task_id"] == "SAR-1083"
    assert report["replayed_execution"] is True
    assert report["operations"] == stored_report["operations"]
