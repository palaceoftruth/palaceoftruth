import asyncio
import uuid

import pytest

from app.schemas.chat import ChatRequest
from app.services.llm_admission import tenant_llm_slot


def test_client_model_override_requires_operator_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.llm_admission.settings.openrouter_default_model", "default/model")
    monkeypatch.setattr("app.services.llm_admission.settings.client_llm_allowed_models", "plan/model")

    accepted = ChatRequest.model_validate(
        {"messages": [{"role": "user", "content": "hello"}], "model": "plan/model"}
    )
    with pytest.raises(ValueError, match="not allowed"):
        ChatRequest.model_validate(
            {"messages": [{"role": "user", "content": "hello"}], "model": "expensive/model"}
        )

    assert accepted.model == "plan/model"


def test_tenant_llm_gate_prevents_same_tenant_starvation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.llm_admission.settings.tenant_llm_max_concurrent_requests", 1)
    tenant_id = f"test-{uuid.uuid4()}"
    active = 0
    maximum = 0

    async def worker() -> None:
        nonlocal active, maximum
        async with tenant_llm_slot(tenant_id):
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.02)
            active -= 1

    async def scenario() -> None:
        await asyncio.gather(worker(), worker(), worker())

    asyncio.run(scenario())
    assert maximum == 1
