import uuid
from datetime import datetime, timezone

import pytest

from app.models.item import Item
from app.schemas.memory import MemoryEntryRequest, MemoryScope, MemoryScopeProfile
from app.services.retention import (
    RetentionExtractedEntry,
    RetentionExtractionOutput,
    RetentionService,
)


class FakeSession:
    def __init__(self) -> None:
        self.added = []
        self.objects = {}
        self.commits = 0

    async def scalar(self, *_args, **_kwargs):
        return None

    async def get(self, model, key):
        return self.objects.get((model, key))

    async def execute(self, *_args, **_kwargs):
        raise AssertionError("unexpected execute call")

    def add(self, value) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        for value in self.added:
            if getattr(value, "id", None) is None:
                value.id = uuid.uuid4()
            self.objects[(type(value), value.id)] = value

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, value) -> None:
        if getattr(value, "id", None) is None:
            value.id = uuid.uuid4()
        if getattr(value, "created_at", None) is None:
            value.created_at = datetime.now(timezone.utc)

    async def rollback(self) -> None:
        raise AssertionError("unexpected rollback")


class FakeProfileService:
    def __init__(self, retain_mission: str) -> None:
        self.retain_mission = retain_mission
        self.scopes = []

    async def get_profile(self, scope: MemoryScope) -> MemoryScopeProfile:
        self.scopes.append(scope)
        return MemoryScopeProfile(scope=scope, retain_mission=self.retain_mission)


class FakeLLM:
    def __init__(self, output: RetentionExtractionOutput | Exception) -> None:
        self.output = output
        self.messages = None

    async def complete_structured(self, messages, _schema, *, schema_name: str):
        self.messages = messages
        assert schema_name == "retention_extraction"
        if isinstance(self.output, Exception):
            raise self.output
        return self.output


def _entry(**overrides) -> MemoryEntryRequest:
    payload = {
        "tenant_id": "tenant-a",
        "title": "Iris run note",
        "body": "Hello there.",
        "summary": None,
        "source": "hermes-agent",
        "created_at": datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc),
        "tags": ["agent:iris"],
        "scope": MemoryScope(type="agent", key="iris"),
        "idempotency_key": "source-note-1",
        "relationship_policy": "deferred",
    }
    payload.update(overrides)
    return MemoryEntryRequest(**payload)


@pytest.mark.asyncio
async def test_retention_service_empty_extraction_creates_zero_entries() -> None:
    db = FakeSession()
    llm = FakeLLM(RetentionExtractionOutput(entries=[]))
    profile = FakeProfileService("Retain SAR tickets. Do not retain greetings.")
    service = RetentionService(db, tenant_id="tenant-a", llm=llm, profile_service=profile)

    result = await service.retain(_entry(), mode="extracted_write")

    assert result.created_count == 0
    assert result.rejected_count == 0
    assert result.acceptance_results == []
    assert db.added == []
    assert profile.scopes == [MemoryScope(type="agent", key="iris")]
    assert "Retain SAR tickets" in llm.messages[1]["content"]


@pytest.mark.asyncio
async def test_retention_service_retains_sar_transition_with_metadata_and_policy() -> None:
    db = FakeSession()
    llm = FakeLLM(
        RetentionExtractionOutput(
            entries=[
                RetentionExtractedEntry(
                    title="SAR-1015 auto-advanced",
                    body="Iris auto-advanced SAR-1015 from Backlog to In Progress at 13:18 UTC.",
                    summary="Iris moved SAR-1015 into active work.",
                    confidence=0.92,
                    fact_kind="experience",
                    tags=["sar:SAR-1015", "agent:iris"],
                )
            ]
        )
    )
    service = RetentionService(
        db,
        tenant_id="tenant-a",
        llm=llm,
        profile_service=FakeProfileService("Retain SAR ticket state transitions."),
    )

    result = await service.retain(_entry(body="Iris auto-advanced SAR-1015."), mode="extracted_write")

    assert result.created_count == 1
    assert result.extraction_confidences == [0.92]
    item = next(value for value in db.added if isinstance(value, Item))
    assert item.title == "SAR-1015 auto-advanced"
    assert item.summary == "Iris moved SAR-1015 into active work."
    assert item.raw_content == "Iris auto-advanced SAR-1015 from Backlog to In Progress at 13:18 UTC."
    assert "fact-kind:experience" in item.tags
    assert "retention:extracted" in item.tags
    memory_entry = item.metadata_["memory_entry"]
    assert memory_entry["fact_kind"] == "experience"
    assert memory_entry["metadata"]["retention_extraction"]["confidence"] == 0.92
    assert result.acceptance_results[0].job.payload["relationship_policy"] == "deferred"


@pytest.mark.asyncio
async def test_retention_service_redacts_secret_values_before_prompt_and_write() -> None:
    db = FakeSession()
    llm = FakeLLM(
        RetentionExtractionOutput(
            entries=[
                RetentionExtractedEntry(
                    title="Token exposure was noted",
                    body="The incident included api_key=sk-proj-abcdefghijklmnopqrstuvwxyz123456",
                    summary="Bearer abcdefghijklmnopqrstuvwxyz1234567890 was present.",
                    confidence=0.8,
                    fact_kind="observation",
                    tags=["incident", "api_key=sk-proj-extractedabcdefghijklmnopqrstuvwxyz"],
                )
            ]
        )
    )
    service = RetentionService(
        db,
        tenant_id="tenant-a",
        llm=llm,
        profile_service=FakeProfileService("Retain incidents without raw secrets."),
    )

    result = await service.retain(
        _entry(
            body="Operator pasted api_key=sk-proj-abcdefghijklmnopqrstuvwxyz123456.",
            tags=["agent:iris", "api_key=sk-proj-sourceabcdefghijklmnopqrstuvwxyz"],
        ),
        mode="extracted_write",
    )

    assert "[redacted]" in llm.messages[1]["content"]
    assert "sk-proj-abcdefghijklmnopqrstuvwxyz123456" not in llm.messages[1]["content"]
    assert "sk-proj-sourceabcdefghijklmnopqrstuvwxyz" not in llm.messages[1]["content"]
    assert result.created_count == 1
    item = next(value for value in db.added if isinstance(value, Item))
    assert "[redacted]" in item.raw_content
    assert "sk-proj-abcdefghijklmnopqrstuvwxyz123456" not in item.raw_content
    assert "[redacted]" in item.summary
    assert "abcdefghijklmnopqrstuvwxyz1234567890" not in item.summary
    assert "redacted" in item.tags
    assert "api_key=sk-proj-sourceabcdefghijklmnopqrstuvwxyz" not in item.tags
    assert "api_key=sk-proj-extractedabcdefghijklmnopqrstuvwxyz" not in item.tags


@pytest.mark.asyncio
async def test_retention_service_raw_write_preserves_existing_memory_write_compatibility() -> None:
    db = FakeSession()
    service = RetentionService(
        db,
        tenant_id="tenant-a",
        llm=FakeLLM(RetentionExtractionOutput(entries=[])),
        profile_service=FakeProfileService("unused"),
    )

    result = await service.retain(_entry(title="Raw entry", body="Store this as provided."), mode="raw_write")

    assert result.created_count == 1
    item = next(value for value in db.added if isinstance(value, Item))
    assert item.title == "Raw entry"
    assert item.raw_content == "Store this as provided."
    assert "retention:extracted" not in item.tags


@pytest.mark.asyncio
async def test_retention_service_extraction_failure_writes_nothing() -> None:
    db = FakeSession()
    service = RetentionService(
        db,
        tenant_id="tenant-a",
        llm=FakeLLM(RuntimeError("model unavailable")),
        profile_service=FakeProfileService("Retain SAR transitions."),
    )

    with pytest.raises(RuntimeError, match="Retention extraction failed"):
        await service.retain(_entry(body="Iris auto-advanced SAR-1015."), mode="extracted_write")

    assert db.added == []
