"""Identity, freshness, and state transitions for HTTP source resources.

Schema/data flow contract: URL observations become explicit alias decisions;
accepted identity is tenant/kind scoped; refresh observations update the resource
while preserving the last successful ``SourceRecord``; each caller persists the
returned append-only audit snapshot in the same transaction.  This module never
performs HTTP requests or schedules refresh work.
"""

from __future__ import annotations

import hashlib
import ipaddress
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Literal
from urllib.parse import SplitResult, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.source_resource import SourceResource, SourceResourceAlias, SourceResourceAuditSnapshot


AliasSignal = Literal["submitted", "final", "canonical"]
Freshness = Literal["current", "due", "stale", "unreachable", "gone", "unknown"]
RefreshOutcome = Literal["success", "not_modified", "failure", "gone"]


@dataclass(frozen=True)
class RefreshLease:
    """A durable claim passed from the dispatcher to one future refresh job."""

    resource_id: uuid.UUID
    tenant_id: str
    token: uuid.UUID
    expires_at: datetime


@dataclass(frozen=True)
class AliasDecision:
    normalized_url: str
    decision: Literal["accepted", "rejected", "conflict"]
    reason: str


@dataclass(frozen=True)
class RefreshObservation:
    outcome: RefreshOutcome
    http_status: int | None = None
    source_record_id: uuid.UUID | None = None
    content_digest: str | None = None
    validator_etag: str | None = None
    validator_last_modified: str | None = None
    failure_reason: str | None = None
    robots_allowed: bool | None = None
    robots_decision: str | None = None
    robots_cached_at: datetime | None = None
    published_at: datetime | None = None
    captured_at: datetime | None = None


def normalize_http_url(raw_url: str) -> str:
    """Normalize only equivalences that are safe for HTTP resource identity.

    Query order, path case, percent-encoding, and trailing slashes are intentionally
    preserved because changing any of them can identify a different resource.
    """

    if not isinstance(raw_url, str) or not raw_url.strip():
        raise ValueError("HTTP source URL must be a non-empty string")
    if raw_url != raw_url.strip():
        raise ValueError("HTTP source URL must not contain surrounding whitespace")

    try:
        parsed = urlsplit(raw_url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("HTTP source URL has an invalid authority") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError("HTTP source URL scheme must be http or https")
    if not parsed.hostname:
        raise ValueError("HTTP source URL must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("HTTP source URL must not embed credentials")

    host = parsed.hostname.lower()
    try:
        ip = ipaddress.ip_address(host)
        host = f"[{ip.compressed}]" if ip.version == 6 else ip.compressed
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError("HTTP source URL host is invalid") from exc

    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    netloc = host if port is None or default_port else f"{host}:{port}"
    path = parsed.path or "/"
    # Fragments are browser-local navigation and never participate in HTTP retrieval.
    return urlunsplit(SplitResult(scheme, netloc, path, parsed.query, ""))


def canonical_http_identity(raw_url: str) -> str:
    normalized = normalize_http_url(raw_url)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def http_origin(raw_url: str) -> tuple[str, str, int]:
    normalized = urlsplit(normalize_http_url(raw_url))
    default_port = 443 if normalized.scheme == "https" else 80
    return normalized.scheme, normalized.hostname or "", normalized.port or default_port


def decide_alias(
    *,
    canonical_url: str,
    observed_url: str,
    signal: AliasSignal,
    accepted_canonical_urls: Iterable[str] = (),
) -> AliasDecision:
    """Classify an observation without silently broadening source identity."""

    if signal not in {"submitted", "final", "canonical"}:
        raise ValueError(f"unsupported alias signal: {signal}")
    normalized = normalize_http_url(observed_url)
    canonical = normalize_http_url(canonical_url)
    if http_origin(normalized) != http_origin(canonical):
        return AliasDecision(normalized, "conflict", "cross_origin_signal")

    accepted = {normalize_http_url(url) for url in accepted_canonical_urls}
    if signal == "canonical" and accepted and normalized not in accepted:
        return AliasDecision(normalized, "conflict", "conflicting_canonical_signal")
    return AliasDecision(normalized, "accepted", "same_origin_signal")


def build_alias(
    *,
    resource: SourceResource,
    tenant_id: str,
    observed_url: str,
    signal: AliasSignal,
    accepted_canonical_urls: Iterable[str] = (),
    final_url: str | None = None,
    canonical_signal_url: str | None = None,
    provenance: dict | None = None,
) -> SourceResourceAlias:
    if resource.tenant_id != tenant_id:
        raise ValueError("source resource does not belong to tenant")
    decision = decide_alias(
        canonical_url=resource.canonical_url,
        observed_url=observed_url,
        signal=signal,
        accepted_canonical_urls=accepted_canonical_urls,
    )
    return SourceResourceAlias(
        tenant_id=tenant_id,
        resource=resource,
        submitted_url=observed_url,
        final_url=final_url,
        canonical_signal_url=canonical_signal_url,
        normalized_url=decision.normalized_url,
        signal=signal,
        decision=decision.decision,
        decision_reason=decision.reason,
        provenance=provenance or {},
    )


def compute_freshness(resource: SourceResource, *, now: datetime | None = None) -> Freshness:
    now = now or datetime.now(timezone.utc)
    if resource.status == "gone":
        return "gone"
    if resource.status == "unreachable":
        return "unreachable"
    if resource.last_success_at is None:
        return "unknown"

    due_at = resource.next_due_at
    if due_at is None and resource.refresh_policy != "manual":
        due_at = resource.last_success_at + timedelta(seconds=resource.refresh_slo_seconds)
    if due_at is None or now < due_at:
        return "current"
    # The SLO is also the grace window between actionable due work and stale data.
    if now < due_at + timedelta(seconds=resource.refresh_slo_seconds):
        return "due"
    return "stale"


def is_due_for_refresh(resource: SourceResource, *, now: datetime) -> bool:
    """Return whether a resource can be claimed without bypassing its policy.

    The dispatcher retries ``unreachable`` resources after their bounded backoff,
    but never schedules manual, paused, or tombstoned resources.
    """

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if resource.kind != "http" or resource.refresh_policy == "manual":
        return False
    if resource.status not in {"active", "unreachable"}:
        return False
    if resource.next_due_at is None or resource.next_due_at > now:
        return False
    if resource.backoff_until is not None and resource.backoff_until > now:
        return False
    if resource.refresh_lease_expires_at is not None and resource.refresh_lease_expires_at > now:
        return False
    return True


def claim_refresh_lease(
    resource: SourceResource,
    *,
    now: datetime,
    lease_seconds: int,
    token: uuid.UUID | None = None,
) -> RefreshLease | None:
    """Claim one due resource, returning ``None`` when another worker owns it."""

    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    if not is_due_for_refresh(resource, now=now):
        return None
    lease_token = token or uuid.uuid4()
    expires_at = now + timedelta(seconds=lease_seconds)
    resource.refresh_lease_token = lease_token
    resource.refresh_lease_expires_at = expires_at
    return RefreshLease(
        resource_id=resource.id,
        tenant_id=resource.tenant_id,
        token=lease_token,
        expires_at=expires_at,
    )


def refresh_lease_job_id(lease: RefreshLease) -> str:
    """Create an auditable ARQ id that is stable for one durable lease."""

    return f"refresh-source-resource:{lease.resource_id}:{lease.token}"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def resource_snapshot(resource: SourceResource) -> dict:
    return {
        "canonical_identity": resource.canonical_identity,
        "canonical_url": resource.canonical_url,
        "status": resource.status,
        "refresh_policy": resource.refresh_policy,
        "refresh_slo_seconds": resource.refresh_slo_seconds,
        "last_http_status": resource.last_http_status,
        "last_failure_reason": resource.last_failure_reason,
        "consecutive_failures": resource.consecutive_failures,
        "robots_allowed": resource.robots_allowed,
        "robots_decision": resource.robots_decision,
        "robots_cached_at": _iso(resource.robots_cached_at),
        "content_digest": resource.content_digest,
        "validator_etag": resource.validator_etag,
        "validator_last_modified": resource.validator_last_modified,
        "published_at": _iso(resource.published_at),
        "captured_at": _iso(resource.captured_at),
        "last_verified_at": _iso(resource.last_verified_at),
        "content_changed_at": _iso(resource.content_changed_at),
        "last_checked_at": _iso(resource.last_checked_at),
        "last_success_at": _iso(resource.last_success_at),
        "next_due_at": _iso(resource.next_due_at),
        "backoff_until": _iso(resource.backoff_until),
        "current_source_record_id": str(resource.current_source_record_id) if resource.current_source_record_id else None,
        "last_successful_source_record_id": (
            str(resource.last_successful_source_record_id) if resource.last_successful_source_record_id else None
        ),
    }


def apply_refresh_observation(
    resource: SourceResource,
    observation: RefreshObservation,
    *,
    checked_at: datetime | None = None,
) -> SourceResourceAuditSnapshot:
    """Apply one already-observed result and produce its immutable audit row."""

    checked_at = checked_at or datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        raise ValueError("checked_at must be timezone-aware")
    if observation.outcome in {"failure", "gone"} and not observation.failure_reason:
        raise ValueError(f"{observation.outcome} observation requires failure_reason")
    if observation.outcome == "not_modified" and observation.source_record_id is not None:
        raise ValueError("not_modified observation cannot create a source record version")

    previous_snapshot = resource_snapshot(resource)
    resource.last_checked_at = checked_at
    resource.last_http_status = observation.http_status
    resource.robots_allowed = observation.robots_allowed
    if observation.robots_decision is not None:
        resource.robots_decision = observation.robots_decision
    if observation.robots_cached_at is not None:
        resource.robots_cached_at = observation.robots_cached_at

    if observation.outcome in {"success", "not_modified"}:
        previous_digest = resource.content_digest
        digest_changed = observation.content_digest is not None and observation.content_digest != previous_digest
        record_changed = (
            observation.source_record_id is not None
            and observation.source_record_id != resource.current_source_record_id
        )
        if observation.outcome == "success" and digest_changed and observation.source_record_id is None:
            raise ValueError("changed successful content requires a source record version")
        if observation.outcome == "success" and record_changed and observation.content_digest is None:
            raise ValueError("new source record version requires a content digest")
        changed = observation.outcome == "success" and (digest_changed or record_changed)
        resource.status = "active"
        resource.last_success_at = checked_at
        resource.last_verified_at = checked_at
        resource.last_failure_reason = None
        resource.consecutive_failures = 0
        resource.backoff_until = None
        resource.validator_etag = observation.validator_etag
        resource.validator_last_modified = observation.validator_last_modified
        if observation.content_digest is not None:
            resource.content_digest = observation.content_digest
        if observation.published_at is not None:
            resource.published_at = observation.published_at
        if changed:
            resource.content_changed_at = checked_at
            resource.captured_at = observation.captured_at or checked_at
        if observation.source_record_id is not None:
            resource.current_source_record_id = observation.source_record_id
            resource.last_successful_source_record_id = observation.source_record_id
        resource.next_due_at = (
            None
            if resource.refresh_policy == "manual"
            else checked_at + timedelta(seconds=resource.refresh_slo_seconds)
        )
    else:
        resource.status = "gone" if observation.outcome == "gone" else "unreachable"
        resource.last_failure_reason = observation.failure_reason
        resource.consecutive_failures = (resource.consecutive_failures or 0) + 1
        # Bounded exponential backoff avoids overflow and never exceeds the freshness SLO.
        delay_seconds = min(resource.refresh_slo_seconds, 60 * (2 ** min(resource.consecutive_failures - 1, 16)))
        resource.backoff_until = checked_at + timedelta(seconds=delay_seconds)
        resource.next_due_at = resource.backoff_until

    return SourceResourceAuditSnapshot(
        tenant_id=resource.tenant_id,
        event_kind=f"refresh_{observation.outcome}",
        previous_snapshot=previous_snapshot,
        next_snapshot=resource_snapshot(resource),
        recorded_at=checked_at,
    )


async def persist_refresh_observation(
    db: AsyncSession,
    *,
    resource: SourceResource,
    tenant_id: str,
    observation: RefreshObservation,
    checked_at: datetime | None = None,
) -> SourceResourceAuditSnapshot:
    if resource.tenant_id != tenant_id:
        raise ValueError("source resource does not belong to tenant")
    audit = apply_refresh_observation(resource, observation, checked_at=checked_at)
    # Assign the many-to-one side so an async session never lazy-loads the
    # collection merely to append this audit event.
    audit.resource = resource
    db.add(audit)
    db.add(resource)
    try:
        await db.flush()
    except Exception:
        # Preserve SQLAlchemy's original exception while ensuring callers cannot
        # accidentally continue using a failed transaction.
        await db.rollback()
        raise
    return audit
