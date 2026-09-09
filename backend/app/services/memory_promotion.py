"""Promotion of one canonical agent memory into tenant-shared memory."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.item import Item
from app.models.palace import MemoryEntry
from app.schemas.memory import MemoryEntryRequest, MemoryScope
from app.schemas.search import split_system_provenance_tags
from app.services.memory import MemoryArtifactAcceptanceResult, accept_canonical_memory_entry
from app.services.memory_admission import evaluate_memory_write_admission
from app.services.mcp_containment import CONTAINMENT_HERMES_AGENT


def _deny(message: str) -> HTTPException:
    return HTTPException(status_code=403, detail=message)


def _require_promotion_authority(
    *,
    auth_mode: str | None,
    delegated_grant_id: Any | None,
    agent_scope_key: str | None,
    containment_mode: str | None,
    allowed_scopes: list[str],
) -> str:
    """Check promotion authority without the admin implicit-scope shortcut."""
    if auth_mode != "mcp_oauth":
        raise _deny("Memory promotion requires MCP OAuth")
    if delegated_grant_id is not None:
        raise _deny("Delegated OAuth grants cannot promote memory")
    if containment_mode != CONTAINMENT_HERMES_AGENT or not agent_scope_key:
        raise _deny("Memory promotion requires a canonical bound agent")
    required = {"memory:promote_shared", "write", "write:agent"}
    if not required.issubset(set(allowed_scopes)):
        raise _deny("MCP OAuth token is missing an explicit memory promotion scope")
    return agent_scope_key


async def promote_memory_entry_to_shared(
    db: AsyncSession,
    *,
    entry_id: uuid.UUID,
    tenant_id: str,
    auth_mode: str | None,
    delegated_grant_id: Any | None,
    agent_scope_key: str | None,
    containment_mode: str | None,
    allowed_scopes: list[str],
    signing_key: str | None,
    client_key: str | None = None,
) -> MemoryArtifactAcceptanceResult:
    """Validate and accept one own-agent memory in ``tenant_shared`` scope."""
    bound_agent = _require_promotion_authority(
        auth_mode=auth_mode,
        delegated_grant_id=delegated_grant_id,
        agent_scope_key=agent_scope_key,
        containment_mode=containment_mode,
        allowed_scopes=allowed_scopes,
    )

    # Lock the source entry for the complete validation/build interval. This
    # prevents two concurrent promotions from observing different source state.
    source, item = (
        await db.execute(
            select(MemoryEntry, Item)
            .join(Item, Item.id == MemoryEntry.item_id)
            .where(MemoryEntry.id == entry_id)
            .where(MemoryEntry.tenant_id == tenant_id)
            .where(MemoryEntry.scope_type == "agent")
            .where(MemoryEntry.scope_key == bound_agent)
            .where(Item.tenant_id == tenant_id)
            .with_for_update(of=(MemoryEntry, Item))
        )
    ).one_or_none() or (None, None)
    if source is None or item is None:
        # Deliberately do not distinguish a sibling or another tenant.
        raise HTTPException(status_code=404, detail="Canonical agent memory entry was not found")

    now = datetime.now(timezone.utc)
    if item.deleted_at is not None or item.status in {"deleted", "rejected", "superseded", "failed"}:
        raise HTTPException(status_code=409, detail="Source memory entry is not promotable")
    if item.status != "ready":
        raise HTTPException(status_code=409, detail="Source memory entry is not ready")
    if item.governance_verification_state in {"rejected", "stale"} or item.governance_superseded_by_item_id is not None:
        raise HTTPException(status_code=409, detail="Source memory governance state does not permit promotion")
    if source.superseded_by_entry_id is not None:
        raise HTTPException(status_code=409, detail="Source memory entry is superseded")
    if source.valid_until is not None and source.valid_until <= now:
        raise HTTPException(status_code=409, detail="Source memory entry is expired")
    if not item.raw_content or not item.raw_content.strip():
        raise HTTPException(status_code=422, detail="Source memory entry has no content")

    idempotency_key = hashlib.sha256(
        f"{tenant_id}:{source.id}:tenant_shared".encode("utf-8")
    ).hexdigest()
    # Keep every replay byte-for-byte stable. A wall-clock audit timestamp here
    # would change the normalized request fingerprint and turn a replay into a
    # duplicate conflict.
    source_timestamp = source.updated_at or source.created_at
    _, semantic_tags = split_system_provenance_tags(list(item.tags or []))
    # Client metadata is intentionally omitted. The promotion record below is
    # server-owned provenance and cannot claim that the copied content is trusted.
    body = MemoryEntryRequest(
        tenant_id=tenant_id,
        title=item.title,
        body=item.raw_content,
        summary=item.summary,
        source=source.source or "memory_promotion",
        source_url=source.source_url or item.source_url,
        created_at=source.created_at,
        tags=[tag for tag in semantic_tags if tag != f"agent-{bound_agent}"],
        scope=MemoryScope(type="tenant_shared"),
        created_by_role="agent",
        metadata={
            "promotion": {
                "kind": "agent_memory_to_tenant_shared",
                "source_entry_id": str(source.id),
                "source_item_id": str(item.id),
                "source_agent_scope_key": bound_agent,
                "target_scope": "tenant_shared",
                "source_updated_at": source_timestamp.astimezone(timezone.utc).isoformat(),
            }
        },
        idempotency_key=idempotency_key,
        valid_from=source.valid_from,
        valid_until=source.valid_until,
        fact_kind=source.fact_kind,
        relationship_policy="skip",
    )
    admission = evaluate_memory_write_admission(
        body=body,
        auth_mode=auth_mode,
        allowed_scopes=allowed_scopes,
        mcp_client_key=client_key,
        mcp_agent_scope_key=bound_agent,
        # The strong promotion checks above authorize the explicit destination;
        # admission still runs its ordinary privacy checks unchanged.
        containment_mode="standard",
    )
    if not admission.accepted:
        raise HTTPException(status_code=admission.http_status_code, detail=admission.response_detail())
    return await accept_canonical_memory_entry(
        db,
        body=body,
        signing_key=signing_key,
        admission_audit={
            **admission.audit, "promotion": body.metadata["promotion"],
            "promotion_actor_client_key": client_key,
        },
    )
