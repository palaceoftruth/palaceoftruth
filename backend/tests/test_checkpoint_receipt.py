import hashlib
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.models.item import Item
from app.models.palace import MemoryEntry
from app.services.checkpoint_receipt import read_checkpoint_receipt


@pytest.fixture
def records():
    item_id, entry_id, job_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    item = SimpleNamespace(id=item_id, tenant_id="t", raw_content="exact archive", deleted_at=None, status="processing")
    entry = SimpleNamespace(id=entry_id, item_id=item_id, tenant_id="t", scope_type="agent", scope_key="a", idempotency_key="key")
    job = SimpleNamespace(id=job_id, item_id=item_id, tenant_id="t", status="queued", payload={"memory_entry_id": str(entry_id), "idempotency_key": "key", "scope_type": "agent", "scope_key": "a"})
    async def get(model, key):
        if model is Item:
            return item if key == item_id else None
        if model is MemoryEntry:
            return entry if key == entry_id else None
        return None
    return SimpleNamespace(get=AsyncMock(side_effect=get)), job, item, entry


@pytest.mark.asyncio
async def test_readback_proves_exact_committed_content_not_queue_completion(records):
    db, job, item, entry = records
    receipt = await read_checkpoint_receipt(db, job=job, tenant_id="t")
    assert receipt["durable"] is True
    assert receipt["body_sha256"] == hashlib.sha256(item.raw_content.encode()).hexdigest()
    assert job.status == "queued"
    assert db.get.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["wrong_tenant", "deleted", "missing_body", "wrong_scope", "wrong_key", "missing_pointer", "missing_item", "wrong_entry_tenant"])
async def test_readback_fails_closed(records, failure):
    db, job, item, entry = records
    if failure == "wrong_tenant":
        job.tenant_id = "other"
    elif failure == "deleted":
        item.status = "deleted"
    elif failure == "missing_body":
        item.raw_content = ""
    elif failure == "wrong_scope":
        entry.scope_key = "other"
    elif failure == "wrong_key":
        entry.idempotency_key = "other"
    elif failure == "missing_pointer":
        job.payload.pop("memory_entry_id")
    elif failure == "missing_item":
        job.item_id = uuid.uuid4()
    elif failure == "wrong_entry_tenant":
        entry.tenant_id = "other"
    with pytest.raises(HTTPException):
        await read_checkpoint_receipt(db, job=job, tenant_id="t")
