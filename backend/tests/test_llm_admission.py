import asyncio
import uuid

import pytest
from contextlib import asynccontextmanager

from app.schemas.chat import ChatRequest
from app.services.llm_admission import (
    _distributed_tenant_llm_slot,
    consume_tenant_token_budget,
    tenant_llm_slot,
)


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

    @asynccontextmanager
    async def distributed_slot(_tenant_id: str):
        yield

    monkeypatch.setattr(
        "app.services.llm_admission._distributed_tenant_llm_slot",
        distributed_slot,
    )

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


def test_first_daily_budget_write_cannot_exceed_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[str] = []

    class Result:
        @staticmethod
        def scalar_one_or_none():
            return None

    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        @staticmethod
        def begin():
            return Transaction()

        async def execute(self, statement, _params):
            statements.append(str(statement))
            return Result()

    monkeypatch.setattr("app.services.llm_admission.settings.tenant_llm_daily_token_limit", 100)
    monkeypatch.setattr("app.database.async_session", lambda **_kwargs: Session())

    with pytest.raises(Exception, match="daily LLM token limit"):
        asyncio.run(consume_tenant_token_budget("tenant-a", 101))

    normalized = " ".join(statements[0].split())
    assert (
        "SELECT :tenant_id, CURRENT_DATE, CAST(:tokens AS BIGINT) "
        "WHERE CAST(:tokens AS BIGINT) <= CAST(:limit AS BIGINT)"
    ) in normalized
    assert "EXCLUDED.used_tokens <= CAST(:limit AS BIGINT)" in normalized


def test_distributed_gate_holds_and_releases_postgres_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execution_options(self, **options):
            assert options == {"isolation_level": "AUTOCOMMIT"}
            calls.append("AUTOCOMMIT")
            return self

        async def scalar(self, statement, _params):
            sql = str(statement)
            calls.append(sql)
            return True

    class Engine:
        @staticmethod
        def connect():
            return Connection()

    monkeypatch.setattr("app.database.engine", Engine())
    monkeypatch.setattr("app.services.llm_admission.settings.tenant_llm_max_concurrent_requests", 2)

    async def scenario() -> None:
        async with _distributed_tenant_llm_slot("tenant-a"):
            assert any("pg_try_advisory_lock" in call for call in calls)

    asyncio.run(scenario())
    assert "AUTOCOMMIT" in calls
    assert any("pg_advisory_unlock" in call for call in calls)
