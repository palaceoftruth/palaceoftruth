"""Search-side tests for the Recommendation 1 governance surface.

Reuses the ``_FakeEmbedder`` / ``_FakeDB`` / ``_governance_candidate``
helpers from :mod:`tests.test_search` so each test drives a real
``SearchService.vector_search`` invocation without standing up the schema.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services.search import SearchService

from tests.test_search import _FakeDB, _FakeEmbedder


def _row(**overrides) -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    base = dict(
        item_id=uuid.uuid4(),
        title="governance probe",
        summary=None,
        source_type="note",
        source_url=None,
        tags=[],
        created_at=now,
        effective_date=None,
        effective_date_source=None,
        effective_date_quality=None,
        chunk_text="governance probe",
        chunk_index=0,
        score=0.8,
        item_metadata={},
        canonical_valid_until=None,
        canonical_superseded_by_entry_id=None,
        governance_owner_subject=None,
        governance_reviewer_subject=None,
        governance_verification_state=None,
        governance_verified_at=None,
        governance_verified_by_subject=None,
        governance_verification_deadline=None,
        governance_risk_class=None,
        governance_superseded_by_item_id=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_search_result_includes_governance_surface_when_item_has_owner_and_deadline() -> None:
    now = datetime.now(timezone.utc)
    db = _FakeDB(rows=[_row(
        governance_owner_subject="alice",
        governance_reviewer_subject="agent:reviewer",
        governance_verification_state="verified",
        governance_verified_at=now,
        governance_verified_by_subject="agent:reviewer",
        governance_verification_deadline=now + timedelta(days=30),
        governance_risk_class="high",
    )])
    service = SearchService(db, _FakeEmbedder(), tenant_id="tenant-a")

    results = asyncio.run(service.vector_search(query="accountable knowledge", limit=1))

    result = results[0]
    assert result.governance is not None
    assert result.governance.owner_subject == "alice"
    assert result.governance.verification_state == "verified"
    assert result.governance.risk_class == "high"
    assert result.governance_currentness_state == "current"


def test_search_result_governance_state_expired_for_past_deadline_high_risk() -> None:
    now = datetime.now(timezone.utc)
    db = _FakeDB(rows=[_row(
        governance_owner_subject="alice",
        governance_verification_state="verified",
        governance_verification_deadline=now - timedelta(days=1),
        governance_risk_class="critical",
    )])
    service = SearchService(db, _FakeEmbedder(), tenant_id="tenant-a")

    results = asyncio.run(service.vector_search(query="expired claim", limit=1))

    result = results[0]
    assert result.governance_currentness_state == "expired"
    trace_row = next(
        row for row in service.last_ranking_trace["results"]
        if row["item_id"] == str(result.item_id)
    )
    assert trace_row["governance_expired_high_risk"] is True
    assert "governance_expired_high_risk" in trace_row["adjustments"]
    assert trace_row["adjustments"]["governance_expired_high_risk"] < 0


def test_search_result_governance_state_unassigned_when_no_governance_set() -> None:
    db = _FakeDB(rows=[_row()])
    service = SearchService(db, _FakeEmbedder(), tenant_id="tenant-a")

    results = asyncio.run(service.vector_search(query="untriaged note", limit=1))

    result = results[0]
    assert result.governance is None
    assert result.governance_currentness_state == "unassigned"
    # ``exclude_if`` strips absent flat fields so legacy callers stay
    # bit-compatible. The wire must never emit ``null`` for a column the
    # row has not triaged.
    serialized = result.model_dump(exclude_none=True)
    assert "governance_owner_subject" not in serialized
    assert "governance_verification_state" not in serialized
    assert "governance_risk_class" not in serialized


def test_search_filter_excludes_governance_superseded_items_in_current_mode() -> None:
    successor_id = uuid.uuid4()
    superseded_id = uuid.uuid4()
    db = _FakeDB(rows=[
        _row(
            item_id=superseded_id,
            title="Superseded claim",
            governance_owner_subject="alice",
            governance_verification_state="verified",
            governance_verification_deadline=datetime.now(timezone.utc) + timedelta(days=30),
            governance_risk_class="high",
            governance_superseded_by_item_id=successor_id,
            score=0.95,
        ),
        _row(
            item_id=successor_id,
            title="Successor claim",
            governance_owner_subject="alice",
            governance_verification_state="verified",
            governance_verification_deadline=datetime.now(timezone.utc) + timedelta(days=30),
            governance_risk_class="moderate",
            score=0.50,
        ),
    ])
    service = SearchService(db, _FakeEmbedder(), tenant_id="tenant-a")

    results = asyncio.run(service.vector_search(query="current claim", limit=5))

    assert successor_id in {result.item_id for result in results}
    assert superseded_id not in {result.item_id for result in results}
    assert service.last_ranking_trace["excluded_governance_counts"].get("superseded", 0) >= 1


def test_search_keeps_expired_high_risk_items_but_penalizes_them() -> None:
    now = datetime.now(timezone.utc)
    safe_id = uuid.uuid4()
    expired_id = uuid.uuid4()
    db = _FakeDB(rows=[
        _row(
            item_id=expired_id,
            title="Expired high risk doc",
            governance_owner_subject="alice",
            governance_verification_state="verified",
            governance_verified_at=now - timedelta(days=60),
            governance_verification_deadline=now - timedelta(days=1),
            governance_risk_class="critical",
            score=0.80,
        ),
        _row(
            item_id=safe_id,
            title="Safe current doc",
            governance_owner_subject="alice",
            governance_verification_state="verified",
            governance_verified_at=now,
            governance_verification_deadline=now + timedelta(days=30),
            governance_risk_class="low",
            score=0.79,
        ),
    ])
    service = SearchService(db, _FakeEmbedder(), tenant_id="tenant-a")

    results = asyncio.run(service.vector_search(query="latest", limit=2))

    # Expired high-risk items stay visible in current mode (so the wire still
    # carries the explicit warning) but they are ranked below safe items.
    assert {result.item_id for result in results} == {safe_id, expired_id}
    assert [result.item_id for result in results] == [safe_id, expired_id]
    expired_result = next(result for result in results if result.item_id == expired_id)
    assert expired_result.governance_currentness_state == "expired"
    # The penalty was applied via adjustments without resorting to exclusion.
    trace_row = next(
        row for row in service.last_ranking_trace["results"]
        if row["item_id"] == str(expired_id)
    )
    assert trace_row["governance_expired_high_risk"] is True
    assert "governance_expired_high_risk" in trace_row["adjustments"]
    # Excluded counts must NOT record expired items as dropped.
    assert "expired" not in service.last_ranking_trace["excluded_governance_counts"]


def test_as_of_query_returns_former_state_with_supersession_citation() -> None:
    """Historical-mode queries must surface superseded items with the wire
    citation pointing at the successor.

    The ``classify_query_intent`` helper keys off of phrases like "as of" to
    flip into historical mode. Driving a real query through ``vector_search``
    is the cheapest way to assert the integration; helpers like a forced
    ``currentness_mode="historical"`` argument would require lifting internal
    state out of the service, which is not warranted for a single test.
    """
    now = datetime.now(timezone.utc)
    superseded_id = uuid.uuid4()
    successor_id = uuid.uuid4()
    db = _FakeDB(rows=[
        _row(
            item_id=superseded_id,
            title="Previous playbook as of last quarter",
            score=0.85,
            chunk_text="Previous playbook as of last quarter",
            governance_owner_subject="alice",
            governance_verification_state="verified",
            governance_verification_deadline=now + timedelta(days=30),
            governance_risk_class="moderate",
            governance_superseded_by_item_id=successor_id,
        ),
        _row(
            item_id=successor_id,
            title="Newer playbook entry",
            score=0.55,
            chunk_text="Newer playbook entry from this quarter",
            governance_owner_subject="alice",
            governance_verification_state="verified",
            governance_verification_deadline=now + timedelta(days=30),
            governance_risk_class="moderate",
        ),
    ])
    service = SearchService(db, _FakeEmbedder(), tenant_id="tenant-a")

    results = asyncio.run(
        service.vector_search(query="playbook as of last quarter", limit=5)
    )

    assert service.last_ranking_trace["currentness_mode"] == "historical"
    assert superseded_id in {result.item_id for result in results}

    superseded_result = next(
        result for result in results if result.item_id == superseded_id
    )
    assert superseded_result.governance_superseded_by_item_id == str(successor_id)
    assert superseded_result.governance_currentness_state == "superseded"
