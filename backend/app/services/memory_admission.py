from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Literal

from app.schemas.memory import MemoryEntryRequest, MemoryScope
from app.services.codex_memory_privacy import CodexMemoryPrivacyScan, scan_codex_memory_privacy

logger = logging.getLogger(__name__)

MemoryWriteAdmissionStatus = Literal["accepted", "rejected", "quarantined"]

_SCOPED_WRITE_GRANTS = {
    "agent": "write:agent",
    "workspace": "write:workspace",
    "session": "write:session",
}


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
) -> MemoryWriteAdmissionDecision:
    """Gate durable memory writes before item/job storage."""
    audit = _base_audit(body, auth_mode=auth_mode, allowed_scopes=allowed_scopes, mcp_client_key=mcp_client_key)

    scope_decision = _scope_write_decision(
        scope=body.scope,
        auth_mode=auth_mode,
        allowed_scopes=allowed_scopes,
        mcp_client_key=mcp_client_key,
        mcp_agent_scope_key=mcp_agent_scope_key,
    )
    if scope_decision is not None:
        decision = MemoryWriteAdmissionDecision(
            status="rejected",
            reason_code=scope_decision,
            message="Authenticated writer is not granted to write the requested memory scope",
            retryable=False,
            http_status_code=403,
            audit={**audit, "scope_grant": _scope_grant_summary(body.scope, allowed_scopes)},
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
        audit={**audit, "scope_grant": _scope_grant_summary(body.scope, allowed_scopes), "privacy_scan": {"finding_count": 0}},
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
) -> str | None:
    if auth_mode not in {"mcp_oauth", "api_key"}:
        return None
    is_hermes_oauth_client = bool(mcp_client_key and mcp_client_key.startswith("hermes-"))
    if is_hermes_oauth_client and scope.type == "tenant_shared":
        return "hermes_agent_write_requires_agent_scope"
    # The broad admin capability is never a bypass for a Hermes OAuth client:
    # its server-owned canonical agent binding remains the write authority.
    if "admin" in allowed_scopes and not is_hermes_oauth_client:
        return None
    if scope.type == "tenant_shared":
        return None
    required_grant = _SCOPED_WRITE_GRANTS[scope.type]
    if required_grant not in allowed_scopes and (is_hermes_oauth_client or "admin" not in allowed_scopes):
        return f"missing_{required_grant.replace(':', '_')}"
    if is_hermes_oauth_client:
        # OAuth client names are not authority. The server-owned binding must
        # match the requested agent scope before a Hermes client can write.
        if not mcp_agent_scope_key:
            return "unbound_hermes_agent_client"
        if scope.type != "agent":
            return "hermes_agent_write_requires_agent_scope"
        if scope.key != mcp_agent_scope_key:
            return "hermes_agent_write_requires_canonical_scope"
    return None


def _scope_grant_summary(scope: MemoryScope, allowed_scopes: list[str]) -> dict[str, Any]:
    required = None if scope.type == "tenant_shared" else _SCOPED_WRITE_GRANTS[scope.type]
    return {
        "scope_type": scope.type,
        "scope_key_hash": _hash_text(scope.key) if scope.key else None,
        "required_scope": required,
        "grant_present": required is None or required in allowed_scopes or "admin" in allowed_scopes,
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
