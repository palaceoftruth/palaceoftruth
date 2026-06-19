from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.memory import (
    AgentMemoryRetrieveRequest,
    MemoryTrajectoryEntry,
    MemoryTrajectoryRequest,
    MemoryTrajectoryResponse,
)
from app.schemas.search import SearchResult
from app.services.memory import DelegatedAgentMemoryReadPolicy, retrieve_agent_memory


CONVERSATION_FACT_TAG = "conversation-fact"
_SUBJECT_RE = re.compile(r"^Subject:\s*(?P<value>.+)$", re.MULTILINE)
_PREDICATE_RE = re.compile(r"^Predicate:\s*(?P<value>.+)$", re.MULTILINE)
_OBJECT_RE = re.compile(r"^Object:\s*(?P<value>.+?)(?:\n\n|$)", re.MULTILINE | re.DOTALL)


def _extract_field(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    if not match:
        return None
    value = " ".join(match.group("value").strip().split())
    return value or None


def _event_time(result: SearchResult) -> datetime:
    timestamp = result.source_span.get("timestamp") if isinstance(result.source_span, dict) else None
    if isinstance(timestamp, str):
        try:
            return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            pass
    return result.created_at


def _trajectory_key(
    *,
    requested_subject: str | None,
    subject: str | None,
    predicate: str | None,
    title: str,
) -> str:
    if requested_subject:
        return requested_subject.strip().casefold()
    if subject and predicate:
        return f"{subject.strip().casefold()}:{predicate.strip().casefold()}"
    if subject:
        return subject.strip().casefold()
    return title.strip().casefold()


def _entry_from_result(result: SearchResult, *, requested_subject: str | None) -> MemoryTrajectoryEntry:
    subject = _extract_field(_SUBJECT_RE, result.chunk_text)
    predicate = _extract_field(_PREDICATE_RE, result.chunk_text)
    object_text = _extract_field(_OBJECT_RE, result.chunk_text) or result.summary or result.chunk_text
    return MemoryTrajectoryEntry(
        item_id=result.item_id,
        title=result.title,
        subject=subject,
        predicate=predicate,
        object_text=object_text,
        trajectory_key=_trajectory_key(
            requested_subject=requested_subject,
            subject=subject,
            predicate=predicate,
            title=result.title,
        ),
        status="current",
        event_time=_event_time(result),
        source_item_id=result.source_item_id,
        source_span=result.source_span,
        retrieved_scope_label=result.retrieved_scope_label,
        score=result.score,
    )


def _conversation_fact_tags(tags: list[str] | None) -> list[str]:
    cleaned = []
    for tag in tags or []:
        if tag not in cleaned:
            cleaned.append(tag)
    if CONVERSATION_FACT_TAG not in cleaned:
        cleaned.append(CONVERSATION_FACT_TAG)
    return cleaned


def _conversation_fact_tags_mode(tags: list[str] | None) -> Literal["any", "all"]:
    # Extra caller tags narrow trajectory facts; they should not turn the query
    # into "conversation-fact OR project-tag" and leak unrelated derived rows.
    return "all" if tags else "any"


def _mark_current(entries: list[MemoryTrajectoryEntry]) -> list[MemoryTrajectoryEntry]:
    latest_by_key: dict[str, datetime] = {}
    for entry in entries:
        latest_by_key[entry.trajectory_key] = max(
            latest_by_key.get(entry.trajectory_key, entry.event_time),
            entry.event_time,
        )
    return [
        entry.model_copy(
            update={
                "status": "current"
                if entry.event_time == latest_by_key.get(entry.trajectory_key)
                else "stale"
            }
        )
        for entry in entries
    ]


async def retrieve_memory_trajectory(
    db: AsyncSession,
    *,
    embedder,
    tenant_id: str,
    body: MemoryTrajectoryRequest,
    delegated_policy: DelegatedAgentMemoryReadPolicy | None = None,
) -> MemoryTrajectoryResponse:
    response = await retrieve_agent_memory(
        db,
        embedder=embedder,
        tenant_id=tenant_id,
        delegated_policy=delegated_policy,
        body=AgentMemoryRetrieveRequest(
            query=body.query,
            agent_scope_key=body.agent_scope_key,
            include_agent_scope_keys=body.include_agent_scope_keys,
            include_all_permitted_agent_scopes=body.include_all_permitted_agent_scopes,
            access_reason=body.access_reason,
            workspace_scope_keys=body.workspace_scope_keys,
            session_scope_key=body.session_scope_key,
            include_tenant_shared=body.include_tenant_shared,
            tenant_shared_policy=body.tenant_shared_policy,
            include_broad_corpus=body.include_broad_corpus,
            broad_corpus_policy=body.broad_corpus_policy,
            workspace_strict=body.workspace_strict,
            limit=body.limit,
            candidate_limit=body.candidate_limit,
            display_limit=body.display_limit,
            context_budget_chars=body.context_budget_chars,
            include_derived_artifacts=True,
            tags=_conversation_fact_tags(body.tags),
            tags_mode=_conversation_fact_tags_mode(body.tags),
            min_score=body.min_score,
            date_from=body.date_from,
            date_to=body.date_to,
        ),
    )
    entries = sorted(
        (
            _entry_from_result(result, requested_subject=body.trajectory_subject)
            for result in response.results
        ),
        key=lambda entry: (entry.event_time, str(entry.item_id)),
    )
    entries = _mark_current(entries)
    return MemoryTrajectoryResponse(
        query=body.query,
        trajectory_subject=body.trajectory_subject,
        scopes=response.scopes,
        trace=response.trace,
        entries=entries,
        current_entries=[entry for entry in entries if entry.status == "current"],
        total=len(entries),
    )
