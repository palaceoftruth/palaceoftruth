import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi import Response
from sqlalchemy.dialects import postgresql

from app.auth import AuthContext
from app.services import memory_promotion
from app.services.mcp_containment import CONTAINMENT_HERMES_AGENT


_GRANTS = ["memory:promote_shared", "write", "write:agent"]


def _authority(**overrides):
    values = {
        "auth_mode": "mcp_oauth",
        "delegated_grant_id": None,
        "agent_scope_key": "agent/codex",
        "containment_mode": CONTAINMENT_HERMES_AGENT,
        "allowed_scopes": _GRANTS,
    }
    values.update(overrides)
    return values


def test_promotion_requires_explicit_bound_agent_grants() -> None:
    assert memory_promotion._require_promotion_authority(**_authority()) == "agent/codex"


@pytest.mark.parametrize(
    "overrides",
    [
        {"auth_mode": "api_key"},
        {"delegated_grant_id": "delegated-1"},
        {"agent_scope_key": None},
        {"containment_mode": "standard"},
        {"allowed_scopes": ["memory:promote_shared", "write"]},
        {"allowed_scopes": ["admin"]},
    ],
)
def test_promotion_denies_weak_or_delegated_authority(overrides) -> None:
    with pytest.raises(HTTPException) as exc_info:
        memory_promotion._require_promotion_authority(**_authority(**overrides))
    assert exc_info.value.status_code == 403


class _Result:
    def __init__(self, value):
        self.value = value

    def one_or_none(self):
        return self.value


class _PromotionDB:
    def __init__(self, result):
        self.result = result
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement.compile(dialect=postgresql.dialect()).string)
        return _Result(self.result)


def _source_item(**overrides):
    source_id = uuid.uuid4()
    item_id = uuid.uuid4()
    created = datetime(2026, 1, 2, tzinfo=timezone.utc)
    source = SimpleNamespace(
        id=source_id,
        item_id=item_id,
        tenant_id="tenant-a",
        scope_type="agent",
        scope_key="agent/codex",
        source="codex",
        source_url="https://example.test/source",
        created_at=created,
        updated_at=created,
        valid_from=None,
        valid_until=None,
        superseded_by_entry_id=None,
        fact_kind="experience",
    )
    item = SimpleNamespace(
        id=item_id,
        tenant_id="tenant-a",
        title="Stable title",
        summary="Stable summary",
        raw_content="Stable source content",
        source_url="https://example.test/item",
        tags=["one", "two"],
        status="ready",
        deleted_at=None,
        governance_verification_state=None,
        governance_verification_deadline=None,
        governance_superseded_by_item_id=None,
    )
    for key, value in overrides.items():
        if hasattr(source, key):
            setattr(source, key, value)
        else:
            setattr(item, key, value)
    return source, item


def _accepted_admission():
    return SimpleNamespace(accepted=True, audit={})


def test_promotion_copies_safe_fields_and_is_stable_on_replay(monkeypatch) -> None:
    source, item = _source_item()
    item.tags = ["one", "scope-agent", "agent-agent/codex", "hermes-memory-source"]
    db = _PromotionDB((source, item))
    bodies = []

    async def accept(_db, *, body, signing_key, admission_audit):
        bodies.append((body, admission_audit))
        return "accepted"

    monkeypatch.setattr(memory_promotion, "evaluate_memory_write_admission", lambda **_: _accepted_admission())
    monkeypatch.setattr(memory_promotion, "accept_canonical_memory_entry", accept)

    kwargs = {
        "entry_id": source.id,
        "tenant_id": "tenant-a",
        **_authority(),
        "signing_key": "test-key",
    }
    assert asyncio.run(memory_promotion.promote_memory_entry_to_shared(db, **kwargs)) == "accepted"
    assert asyncio.run(memory_promotion.promote_memory_entry_to_shared(db, **kwargs)) == "accepted"

    first, second = bodies
    first_body, first_audit = first
    second_body, second_audit = second
    assert first_body.model_dump(mode="json") == second_body.model_dump(mode="json")
    assert first_body.scope.type == "tenant_shared"
    assert first_body.body == item.raw_content
    assert first_body.source_url == source.source_url
    assert first_body.tags == ["one"]
    assert first_body.supersedes_entry_id is None
    assert first_body.metadata == {"promotion": first_body.metadata["promotion"]}
    assert "source_agent_scope_key" in first_audit["promotion"]
    assert first_audit == second_audit
    assert len(first_body.idempotency_key) == 64


@pytest.mark.parametrize(
    "overrides, status, message",
    [
        ({"deleted_at": datetime(2026, 1, 3, tzinfo=timezone.utc)}, 409, "not promotable"),
        ({"status": "rejected"}, 409, "not promotable"),
        ({"status": "processing"}, 409, "not ready"),
        ({"governance_verification_state": "rejected"}, 409, "governance"),
        ({"governance_verification_state": "stale"}, 409, "governance"),
        ({"governance_verification_deadline": datetime(2026, 1, 1, tzinfo=timezone.utc)}, 409, "expired"),
        ({"governance_superseded_by_item_id": uuid.uuid4()}, 409, "governance"),
        ({"raw_content": "   "}, 422, "no content"),
        ({"superseded_by_entry_id": uuid.uuid4()}, 409, "superseded"),
        ({"valid_until": datetime(2026, 1, 1, tzinfo=timezone.utc)}, 409, "expired"),
    ],
)
def test_promotion_rejects_invalid_source_state(overrides, status, message, monkeypatch) -> None:
    source, item = _source_item(**overrides)
    db = _PromotionDB((source, item))
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            memory_promotion.promote_memory_entry_to_shared(
                db,
                entry_id=source.id,
                tenant_id="tenant-a",
                **_authority(),
                signing_key=None,
            )
        )
    assert exc_info.value.status_code == status
    assert message in str(exc_info.value.detail)


@pytest.mark.parametrize("tenant_id, agent_scope", [("other-tenant", "agent/codex"), ("tenant-a", "agent/sibling")])
def test_promotion_filters_cross_tenant_and_sibling_scope(tenant_id, agent_scope) -> None:
    # A real query returns no row because tenant and exact bound scope are part
    # of the predicate; the service then uses one indistinguishable 404.
    db = _PromotionDB(None)
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            memory_promotion.promote_memory_entry_to_shared(
                db,
                entry_id=uuid.uuid4(),
                tenant_id=tenant_id,
                **_authority(agent_scope_key=agent_scope),
                signing_key=None,
            )
        )
    assert exc_info.value.status_code == 404
    sql = db.statements[0]
    assert "memory_entries.tenant_id" in sql
    assert "memory_entries.scope_key" in sql
    assert "items.tenant_id" in sql


def test_promotion_privacy_admission_rejection_is_preserved(monkeypatch) -> None:
    source, item = _source_item()
    db = _PromotionDB((source, item))
    decision = SimpleNamespace(
        accepted=False,
        http_status_code=422,
        response_detail=lambda: {"reason_code": "potential_secret"},
    )
    monkeypatch.setattr(memory_promotion, "evaluate_memory_write_admission", lambda **_: decision)
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            memory_promotion.promote_memory_entry_to_shared(
                db,
                entry_id=source.id,
                tenant_id="tenant-a",
                **_authority(),
                signing_key=None,
            )
        )
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["reason_code"] == "potential_secret"


@pytest.mark.parametrize("missing", ["memory:promote_shared", "write", "write:agent"])
def test_each_promotion_grant_is_required_independently(missing) -> None:
    grants = [grant for grant in _GRANTS if grant != missing]
    with pytest.raises(HTTPException) as exc_info:
        memory_promotion._require_promotion_authority(**_authority(allowed_scopes=grants))
    assert exc_info.value.status_code == 403


def test_route_forwards_auth_and_queue_contract(monkeypatch) -> None:
    from app.api import memory as api_memory

    entry_id = uuid.uuid4()
    job = SimpleNamespace(id=uuid.uuid4())
    result = SimpleNamespace(job=job, enqueue_requested=True)
    context = AuthContext(
        tenant_id="tenant-a",
        auth_mode="mcp_oauth",
        agent_scope_key="agent/codex",
        containment_mode=CONTAINMENT_HERMES_AGENT,
        client_key="hermes-codex",
        scopes=tuple(_GRANTS),
        delegated_grant_id=None,
    )
    request = SimpleNamespace(
        state=SimpleNamespace(tenant_id="tenant-a", auth_context=context),
        app=SimpleNamespace(state=SimpleNamespace()),
    )
    forwarded = {}
    queued = []

    async def promote(_db, **kwargs):
        forwarded.update(kwargs)
        return result

    async def enqueue(_request, _db, *, job):
        queued.append(job)

    accepted = SimpleNamespace(
        contract_status="accepted",
        poll_after_seconds=5,
        retry_after_seconds=None,
    )
    monkeypatch.setattr(api_memory, "promote_memory_entry_to_shared", promote)
    monkeypatch.setattr(api_memory, "_memory_queue_contract_hint", lambda _request: asyncio.sleep(0, result=None))
    monkeypatch.setattr(api_memory, "_enqueue_memory_job_or_raise", enqueue)
    monkeypatch.setattr(api_memory, "build_memory_acceptance_response", lambda *args, **kwargs: accepted)
    monkeypatch.setattr(api_memory, "_memory_job_poll_url", lambda *_args: "/poll")
    monkeypatch.setattr(api_memory, "_set_memory_contract_headers", lambda *_args, **_kwargs: None)

    response = Response()
    returned = asyncio.run(api_memory.promote_memory_entry(entry_id, request, response, object()))
    assert returned is accepted
    assert queued == [job]
    assert forwarded["entry_id"] == entry_id
    assert forwarded["tenant_id"] == "tenant-a"
    assert forwarded["auth_mode"] == "mcp_oauth"
    assert forwarded["agent_scope_key"] == "agent/codex"
    assert forwarded["client_key"] == "hermes-codex"
    assert forwarded["allowed_scopes"] == _GRANTS


@pytest.mark.parametrize("source_days, governance_days, expected_days", [(None, 2, 2), (4, 2, 2), (1, 2, 1)])
def test_promotion_cannot_outlive_source_or_governance_deadline(monkeypatch, source_days, governance_days, expected_days):
    now = datetime.now(timezone.utc)
    source, item = _source_item()
    source.valid_until = now + timedelta(days=source_days) if source_days else None
    item.governance_verification_deadline = now + timedelta(days=governance_days)
    captured = []
    async def accept(_db, *, body, **kwargs):
        captured.append(body)
    monkeypatch.setattr(memory_promotion, "accept_canonical_memory_entry", accept)
    asyncio.run(memory_promotion.promote_memory_entry_to_shared(
        _PromotionDB((source, item)), entry_id=source.id, tenant_id="tenant-a", **_authority(), signing_key=None))
    assert captured[0].valid_until == now + timedelta(days=expected_days)
