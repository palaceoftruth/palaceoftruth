from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Literal

from app.schemas.memory import MemoryEntryRequest, MemoryScope
from app.services.codex_memory_privacy import CodexMemoryPrivacyScan, scan_codex_memory_privacy
from app.services.mcp_containment import CONTAINMENT_STANDARD, is_contained_agent_client

logger = logging.getLogger(__name__)

MemoryWriteAdmissionStatus = Literal["accepted", "rejected", "quarantined"]

_SCOPED_WRITE_GRANTS = {
    "agent": "write:agent",
    "workspace": "write:workspace",
    "session": "write:session",
    # Tenant-shared writes are the widest blast radius in the product, so they
    # need a grant too. "write" is the catalog scope already documented as
    # "Create tenant-shared memory entries".
    "tenant_shared": "write",
}

# Auth modes whose callers hold an explicit, server-issued grant list. Anything
# outside this set is an in-process trusted caller with no scope list to check.
_GRANTED_AUTH_MODES = frozenset({"mcp_oauth", "api_key", "browser_extension"})


@dataclass(frozen=True)
class MemoryWriteAdmissionDecision:
    status: MemoryWriteAdmissionStatus
    reason_code: str
    message: str
    retryable: bool
    http_status_code: int
    audit: dict[str, Any]

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"

    def response_detail(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason_code": self.reason_code,
            "message": self.message,
            "retryable": self.retryable,
            "audit": self.audit,
        }


def evaluate_memory_write_admission(
    *,
    body: MemoryEntryRequest,
    auth_mode: str | None,
    allowed_scopes: list[str],
    mcp_client_key: str | None,
    mcp_agent_scope_key: str | None = None,
    containment_mode: str | None = CONTAINMENT_STANDARD,
) -> MemoryWriteAdmissionDecision:
    """Gate durable memory writes before item/job storage."""
    audit = _base_audit(body, auth_mode=auth_mode, allowed_scopes=allowed_scopes, mcp_client_key=mcp_client_key)

    scope_decision = _scope_write_decision(
        scope=body.scope,
        auth_mode=auth_mode,
        allowed_scopes=allowed_scopes,
        mcp_client_key=mcp_client_key,
        mcp_agent_scope_key=mcp_agent_scope_key,
        containment_mode=containment_mode,
    )
    if scope_decision is not None:
        decision = MemoryWriteAdmissionDecision(
            status="rejected",
            reason_code=scope_decision,
            message="Authenticated writer is not granted to write the requested memory scope",
            retryable=False,
            http_status_code=403,
            audit={**audit, "scope_grant": _scope_grant_summary(body.scope, allowed_scopes, contained=is_contained_agent_client(containment_mode))},
        )
        log_memory_write_admission(decision)
        return decision

    privacy_scan = _scan_request(body)
    if privacy_scan.has_findings:
        decision = MemoryWriteAdmissionDecision(
            status="quarantined",
            reason_code="potential_secret",
            message="Memory write was quarantined before storage because it appears to contain secret material",
            retryable=False,
            http_status_code=422,
            audit={**audit, "privacy_scan": _scan_audit(privacy_scan)},
        )
        log_memory_write_admission(decision)
        return decision

    if _looks_like_sensitive_transcript(body):
        decision = MemoryWriteAdmissionDecision(
            status="quarantined",
            reason_code="raw_transcript_body",
            message="Memory write was quarantined before storage because it appears to contain a raw transcript body",
            retryable=False,
            http_status_code=422,
            audit={**audit, "privacy_scan": {"finding_count": 0}, "transcript_body": True},
        )
        log_memory_write_admission(decision)
        return decision

    decision = MemoryWriteAdmissionDecision(
        status="accepted",
        reason_code="accepted",
        message="Memory write admission accepted",
        retryable=False,
        http_status_code=202,
        audit={**audit, "scope_grant": _scope_grant_summary(body.scope, allowed_scopes, contained=is_contained_agent_client(containment_mode)), "privacy_scan": {"finding_count": 0}},
    )
    log_memory_write_admission(decision)
    return decision


def log_memory_write_admission(decision: MemoryWriteAdmissionDecision) -> None:
    logger.info(
        "memory write admission %s",
        json.dumps(
            {
                "status": decision.status,
                "reason_code": decision.reason_code,
                "retryable": decision.retryable,
                "audit": decision.audit,
            },
            sort_keys=True,
            default=str,
        ),
    )


def _scope_write_decision(
    *,
    scope: MemoryScope,
    auth_mode: str | None,
    allowed_scopes: list[str],
    mcp_client_key: str | None,
    mcp_agent_scope_key: str | None,
    containment_mode: str | None,
) -> str | None:
    if auth_mode not in _GRANTED_AUTH_MODES:
        return None
    # Containment is read from the server-owned mcp_clients column, never from
    # the caller-chosen client_key. mcp_client_key stays audit-only here.
    is_contained_agent = is_contained_agent_client(containment_mode)
    if is_contained_agent and scope.type == "tenant_shared":
        return "hermes_agent_write_requires_agent_scope"

    required_grant = _SCOPED_WRITE_GRANTS[scope.type]
    if not _grant_present(required_grant, allowed_scopes, contained=is_contained_agent):
        return f"missing_{required_grant.replace(':', '_')}"

    if is_contained_agent:
        # OAuth client names are not authority. The server-owned binding must
        # match the requested agent scope before a contained client can write.
        if not mcp_agent_scope_key:
            return "unbound_hermes_agent_client"
        if scope.type != "agent":
            return "hermes_agent_write_requires_agent_scope"
        if scope.key != mcp_agent_scope_key:
            return "hermes_agent_write_requires_canonical_scope"
    elif scope.type == "agent" and mcp_agent_scope_key and scope.key != mcp_agent_scope_key:
        # Any client bound to a canonical agent scope writes only into it, so a
        # write:agent grant cannot be replayed against another agent's scope.
        return "agent_write_outside_bound_scope"
    return None


def _grant_present(required_grant: str, allowed_scopes: list[str], *, contained: bool) -> bool:
    if required_grant in allowed_scopes:
        return True
    # "admin" is itself a stored, server-issued grant, so it still authorizes a
    # scoped write — but never for a contained agent client, whose canonical
    # binding is the only write authority it has.
    return not contained and "admin" in allowed_scopes


def _scope_grant_summary(scope: MemoryScope, allowed_scopes: list[str], *, contained: bool = False) -> dict[str, Any]:
    required = _SCOPED_WRITE_GRANTS[scope.type]
    return {
        "scope_type": scope.type,
        "scope_key_hash": _hash_text(scope.key) if scope.key else None,
        "required_scope": required,
        "grant_present": _grant_present(required, allowed_scopes, contained=contained),
    }


def _scan_request(body: MemoryEntryRequest) -> CodexMemoryPrivacyScan:
    text = "\n".join(
        part
        for part in (
            body.title,
            body.summary or "",
            body.body,
            body.source,
            body.source_url or "",
            json.dumps(body.metadata or {}, sort_keys=True, default=str),
        )
        if part
    )
    return scan_codex_memory_privacy(text)


def _scan_audit(scan: CodexMemoryPrivacyScan) -> dict[str, Any]:
    return {
        "severity": scan.severity,
        "finding_count": len(scan.findings),
        "findings": [
            {
                "kind": finding.kind,
                "severity": finding.severity,
                "line": finding.line,
                "column": finding.column,
                "pattern": finding.pattern,
            }
            for finding in scan.findings[:8]
        ],
    }


def _looks_like_sensitive_transcript(body: MemoryEntryRequest) -> bool:
    body_text = body.body
    lower_markers = " ".join(
        [
            body.source.lower(),
            " ".join(tag.lower() for tag in body.tags),
            json.dumps(body.metadata or {}, sort_keys=True, default=str).lower(),
        ]
    )
    if "transcript" not in lower_markers:
        return False
    speaker_lines = 0
    for line in body_text.splitlines():
        stripped = line.strip().lower()
        if stripped.startswith(("user:", "assistant:", "human:", "agent:", "speaker ", "speaker:")):
            speaker_lines += 1
    return len(body_text) > 800 or speaker_lines >= 6


def _base_audit(
    body: MemoryEntryRequest,
    *,
    auth_mode: str | None,
    allowed_scopes: list[str],
    mcp_client_key: str | None,
) -> dict[str, Any]:
    return {
        "tenant_id_hash": _hash_text(body.tenant_id),
        "title_hash": _hash_text(body.title),
        "body_sha256": _hash_text(body.body),
        "body_length": len(body.body),
        "source_hash": _hash_text(body.source),
        "scope_type": body.scope.type,
        "scope_key_hash": _hash_text(body.scope.key) if body.scope.key else None,
        "auth_mode": auth_mode,
        "mcp_client_key_hash": _hash_text(mcp_client_key) if mcp_client_key else None,
        "allowed_scope_count": len(allowed_scopes),
    }


def _hash_text(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode()).hexdigest()
