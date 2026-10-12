
import uuid
import pytest
from fastapi import HTTPException
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.api.memory import get_checkpoint_receipt
from app.services.memory import MEMORY_JOB_TYPE


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["cross_tenant", "cross_agent", "delegated_scope", "missing_scope"])
async def test_route_rejects_unauthorized_receipt_before_readback(case):
    job = SimpleNamespace(tenant_id="t", job_type=MEMORY_JOB_TYPE, payload={"scope_type": "agent", "scope_key": "a"})
    state = SimpleNamespace(tenant_id="t", auth_mode="mcp_oauth", mcp_agent_scope_key="a", mcp_containment_mode="hermes_agent", auth_context=None)
    if case == "cross_tenant":
        job.tenant_id = "other"
    elif case == "cross_agent":
        job.payload["scope_key"] = "other"
    elif case == "delegated_scope":
        state.auth_context = SimpleNamespace(delegated_grant_id="grant", delegated_agent_scope_keys=(), delegated_workspace_scope_keys=())
    else:
        job.payload.pop("scope_type")
    db = SimpleNamespace(get=AsyncMock(return_value=job))
    with pytest.raises(HTTPException) as error:
        await get_checkpoint_receipt(uuid.uuid4(), SimpleNamespace(state=state), db)
    assert error.value.status_code in {403, 404, 409}
    assert db.get.await_count == 1
