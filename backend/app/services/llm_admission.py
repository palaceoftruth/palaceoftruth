"""Admission policy for client-selected LLM work."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from app.config import settings


@dataclass
class _TenantGate:
    semaphore: asyncio.Semaphore
    users: int = 0


_GATES: OrderedDict[str, _TenantGate] = OrderedDict()
_GATES_LOCK = asyncio.Lock()
_MAX_TRACKED_TENANTS = 1024
logger = logging.getLogger(__name__)


class TenantLlmBudgetExceeded(RuntimeError):
    """Raised before provider work when a tenant spent its daily allowance."""


def validate_client_llm_model(model: str | None) -> str | None:
    """Allow only operator-configured client model overrides."""

    if model is None:
        return None
    cleaned = model.strip()
    if not cleaned:
        raise ValueError("model must not be blank")
    allowed = {settings.openrouter_default_model}
    allowed.update(
        value.strip()
        for value in settings.client_llm_allowed_models.split(",")
        if value.strip()
    )
    if cleaned not in allowed:
        raise ValueError("model is not allowed for client-selected LLM work")
    return cleaned


@asynccontextmanager
async def tenant_llm_slot(tenant_id: str) -> AsyncIterator[None]:
    """Bound concurrent LLM admission per tenant with bounded gate state."""

    async with _GATES_LOCK:
        gate = _GATES.get(tenant_id)
        if gate is None:
            gate = _TenantGate(
                asyncio.Semaphore(max(1, settings.tenant_llm_max_concurrent_requests))
            )
            _GATES[tenant_id] = gate
        gate.users += 1
        _GATES.move_to_end(tenant_id)
        for key, candidate in list(_GATES.items()):
            if len(_GATES) <= _MAX_TRACKED_TENANTS:
                break
            if candidate.users == 0:
                del _GATES[key]
    try:
        async with gate.semaphore:
            yield
    finally:
        async with _GATES_LOCK:
            gate.users -= 1
            _GATES.move_to_end(tenant_id)


async def consume_tenant_token_budget(tenant_id: str, estimated_tokens: int) -> None:
    """Atomically consume a conservative daily token estimate."""

    limit = settings.tenant_llm_daily_token_limit
    if limit <= 0:
        return
    from sqlalchemy import text

    from app.database import async_session

    try:
        session_context = async_session(info={"tenant_id": tenant_id, "system_access": False})
    except TypeError:
        session_context = async_session()
    async with session_context as session:
        async with session.begin():
            result = await session.execute(
                text(
                    "INSERT INTO tenant_llm_daily_usage (tenant_id, usage_day, used_tokens) "
                    "VALUES (:tenant_id, CURRENT_DATE, :tokens) "
                    "ON CONFLICT (tenant_id, usage_day) DO UPDATE "
                    "SET used_tokens = tenant_llm_daily_usage.used_tokens + EXCLUDED.used_tokens, "
                    "updated_at = now() "
                    "WHERE tenant_llm_daily_usage.used_tokens + EXCLUDED.used_tokens <= :limit "
                    "RETURNING used_tokens"
                ),
                {
                    "tenant_id": tenant_id,
                    "tokens": max(1, estimated_tokens),
                    "limit": limit,
                },
            )
            used = result.scalar_one_or_none()
    if used is None:
        logger.warning("tenant LLM daily token limit reached tenant_id=%s limit=%s", tenant_id, limit)
        raise TenantLlmBudgetExceeded("tenant daily LLM token limit reached")
    if used >= int(limit * 0.8):
        logger.warning(
            "tenant LLM daily token use above alert threshold tenant_id=%s used=%s limit=%s",
            tenant_id,
            used,
            limit,
        )
