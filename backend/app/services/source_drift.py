"""Create deterministic review proposals from watched HTTP source changes."""

from __future__ import annotations

import difflib
import logging
import re
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.item import Item
from app.models.palace import (
    CandidateCurationArtifact,
    Claim,
    ClaimSource,
    SourceChunk,
    SourceRecord,
)
from app.models.source_resource import SourceResource
from app.services.curation_artifacts import _record_artifact_event
from app.services.source_resources import normalize_http_url


logger = logging.getLogger(__name__)

_MAX_DIFF_CHARS = 24_000
_SENSITIVE_FIELD_LINE = re.compile(
    r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie|x-api-key|"
    r"api[_-]?key|client[_-]?secret|password|secret|token|credential|signature|session|"
    r"access[_-]?token|refresh[_-]?token|private[_-]?key|signing[_-]?key|"
    r"webhook[_-]?secret|database[_-]?url|redis[_-]?url|dsn|connection[_-]?string)"
    r"\b[\"']?\s*[:=]"
)
_CREDENTIAL_TOKEN = re.compile(
    r"(?i)\b(?:gh[pousr]_[a-z0-9]{20,}|sk-[a-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|"
    r"eyJ[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\.[a-z0-9_-]{8,})\b"
)
_HIGH_ENTROPY_BLOB = re.compile(r"\b(?:[a-f0-9]{48,}|[a-z0-9+/=_-]{64,})\b", re.IGNORECASE)
_LABELED_VALUE = re.compile(r"^\s*[\"']?([^:=]{1,100})[\"']?\s*[:=]")
_SENSITIVE_EXACT_LABELS = frozenset(
    {
        "authorization",
        "proxy authorization",
        "cookie",
        "set cookie",
        "credentials",
        "dsn",
        "password",
        "secret",
        "session",
        "signature",
        "token",
    }
)
_SENSITIVE_LABEL_SUFFIXES = (
    " api key",
    " secret key",
    " access key",
    " access token",
    " refresh token",
    " auth token",
    " session token",
    " private key",
    " signing key",
    " webhook secret",
    " client secret",
    " connection string",
    " database url",
    " redis url",
)
_PEM_BEGIN = re.compile(r"(?i)-----BEGIN [^-]*PRIVATE KEY-----")
_PEM_END = re.compile(r"(?i)-----END [^-]*PRIVATE KEY-----")
_SENSITIVE_LINE_MARKERS = ("private transcript", "raw transcript")


class SourceDriftError(RuntimeError):
    """Raised when source provenance cannot safely support a review proposal."""


@dataclass(frozen=True)
class SourceDriftProposalResult:
    artifact: CandidateCurationArtifact | None
    outcome: str


def source_drift_dedupe_key(
    resource_id: uuid.UUID,
    previous_source_record_id: uuid.UUID,
    current_source_record_id: uuid.UUID,
) -> str:
    return f"source-drift:{resource_id}:{previous_source_record_id}:{current_source_record_id}"


def _redact_lines(chunks: list[SourceChunk]) -> list[str]:
    redacted: list[str] = []
    in_private_key = False
    for chunk in sorted(chunks, key=lambda row: row.chunk_index):
        for line in chunk.chunk_text.splitlines():
            if _PEM_BEGIN.search(line):
                in_private_key = True
                redacted.append("[sensitive private-key block redacted]")
                continue
            if in_private_key:
                if _PEM_END.search(line):
                    in_private_key = False
                continue
            lowered = line.lower()
            label_match = _LABELED_VALUE.match(line)
            normalized_label = (
                re.sub(r"[^a-z0-9]+", " ", label_match.group(1).lower()).strip()
                if label_match
                else ""
            )
            if (
                any(marker in lowered for marker in _SENSITIVE_LINE_MARKERS)
                or _SENSITIVE_FIELD_LINE.search(line)
                or (
                    normalized_label in _SENSITIVE_EXACT_LABELS
                    or normalized_label.endswith(_SENSITIVE_LABEL_SUFFIXES)
                )
                or _CREDENTIAL_TOKEN.search(line)
                or _HIGH_ENTROPY_BLOB.search(line)
            ):
                redacted.append("[sensitive source line redacted]")
                continue
            redacted.append(line)
    return redacted


def _review_target_url(raw_url: str) -> str:
    parsed = urlsplit(raw_url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def build_readable_source_diff(
    previous_chunks: list[SourceChunk],
    current_chunks: list[SourceChunk],
) -> tuple[str, bool]:
    """Return a bounded, deterministic unified diff without logging source text."""

    previous_lines = _redact_lines(previous_chunks)
    current_lines = _redact_lines(current_chunks)
    rendered = "\n".join(
        difflib.unified_diff(
            previous_lines,
            current_lines,
            fromfile="previous source version",
            tofile="current source version",
            lineterm="",
        )
    )
    if not rendered:
        rendered = "Source content changed only in redacted or non-text evidence."
    truncated = len(rendered) > _MAX_DIFF_CHARS
    if truncated:
        rendered = rendered[:_MAX_DIFF_CHARS].rstrip() + "\n... diff truncated for review safety"
    return rendered, truncated


def _record_is_tenant_scoped(record: SourceRecord, tenant_id: str) -> bool:
    return bool(record.tenant_id) and record.tenant_id == tenant_id


async def create_source_drift_proposal(
    db: AsyncSession,
    *,
    tenant_id: str,
    resource_id: uuid.UUID,
    previous_source_record_id: uuid.UUID,
    current_source_record_id: uuid.UUID,
) -> SourceDriftProposalResult:
    """Create exactly one reviewable proposal for one successful version pair.

    The caller owns the transaction. A unique tenant/dedupe constraint is the
    final concurrency guard; this read-first path makes ordinary worker retries
    a no-op without raising an integrity error.
    """

    if not tenant_id:
        logger.warning("source_drift_denied reason=missing_tenant")
        raise SourceDriftError("source drift proposal requires tenant scope")
    if previous_source_record_id == current_source_record_id:
        return SourceDriftProposalResult(artifact=None, outcome="unchanged")

    dedupe_key = source_drift_dedupe_key(
        resource_id, previous_source_record_id, current_source_record_id
    )
    existing = await db.scalar(
        select(CandidateCurationArtifact)
        .where(CandidateCurationArtifact.tenant_id == tenant_id)
        .where(CandidateCurationArtifact.dedupe_key == dedupe_key)
    )
    if existing is not None:
        logger.info(
            "source_drift_retry_deduplicated tenant_id=%s resource_id=%s artifact_id=%s",
            tenant_id,
            resource_id,
            existing.id,
        )
        return SourceDriftProposalResult(artifact=existing, outcome="deduplicated")

    resource = await db.scalar(
        select(SourceResource)
        .where(SourceResource.id == resource_id)
        .where(SourceResource.tenant_id == tenant_id)
        .where(SourceResource.kind == "http")
    )
    if resource is None or not resource.tenant_id:
        logger.warning(
            "source_drift_denied tenant_id=%s resource_id=%s reason=resource_scope",
            tenant_id,
            resource_id,
        )
        raise SourceDriftError("watched HTTP source is missing or outside tenant scope")
    if resource.last_successful_source_record_id != previous_source_record_id:
        raise SourceDriftError("source drift previous version is not the resource last-successful record")

    previous_record = await db.scalar(
        select(SourceRecord)
        .where(SourceRecord.id == previous_source_record_id)
        .where(SourceRecord.tenant_id == tenant_id)
    )
    current_record = await db.scalar(
        select(SourceRecord)
        .where(SourceRecord.id == current_source_record_id)
        .where(SourceRecord.tenant_id == tenant_id)
    )
    if previous_record is None or current_record is None:
        raise SourceDriftError("source drift records are missing or outside tenant scope")
    if not _record_is_tenant_scoped(previous_record, tenant_id) or not _record_is_tenant_scoped(
        current_record, tenant_id
    ):
        raise SourceDriftError("source drift record tenant provenance is incomplete")
    if previous_record.status not in {"active", "stale"} or current_record.status != "active":
        raise SourceDriftError("source drift requires the last two successful source records")
    if previous_record.item_id != current_record.item_id:
        raise SourceDriftError("source drift versions must belong to the same source item")
    if not previous_record.source_uri or not current_record.source_uri:
        raise SourceDriftError("source drift record URI provenance is incomplete")
    try:
        record_uris = {
            normalize_http_url(previous_record.source_uri),
            normalize_http_url(current_record.source_uri),
        }
        canonical_uri = normalize_http_url(resource.canonical_url)
    except ValueError as exc:
        raise SourceDriftError("source drift record URI provenance is invalid") from exc
    if record_uris != {canonical_uri}:
        raise SourceDriftError("source drift record URI does not match the watched source")
    if previous_record.content_hash == current_record.content_hash:
        logger.info(
            "source_drift_no_change tenant_id=%s resource_id=%s previous_source_record_id=%s current_source_record_id=%s",
            tenant_id,
            resource_id,
            previous_source_record_id,
            current_source_record_id,
        )
        return SourceDriftProposalResult(artifact=None, outcome="unchanged")

    affected_item_ids = list(dict.fromkeys((previous_record.item_id, current_record.item_id)))
    items = list(
        (
            await db.scalars(
                select(Item)
                .where(Item.tenant_id == tenant_id)
                .where(Item.id.in_(affected_item_ids))
            )
        ).all()
    )
    if len(items) != len(affected_item_ids) or any(not item.tenant_id for item in items):
        logger.warning(
            "source_drift_denied tenant_id=%s resource_id=%s reason=item_scope",
            tenant_id,
            resource_id,
        )
        raise SourceDriftError("affected source items are missing tenant ACL provenance")

    previous_chunks = list(
        (
            await db.scalars(
                select(SourceChunk)
                .where(SourceChunk.tenant_id == tenant_id)
                .where(SourceChunk.source_record_id == previous_source_record_id)
                .order_by(SourceChunk.chunk_index.asc())
            )
        ).all()
    )
    current_chunks = list(
        (
            await db.scalars(
                select(SourceChunk)
                .where(SourceChunk.tenant_id == tenant_id)
                .where(SourceChunk.source_record_id == current_source_record_id)
                .order_by(SourceChunk.chunk_index.asc())
            )
        ).all()
    )
    if not previous_chunks or not current_chunks:
        raise SourceDriftError("source drift text provenance is incomplete")

    affected_claim_ids = list(
        dict.fromkeys(
            (
                await db.scalars(
                    select(Claim.id)
                    .join(ClaimSource, ClaimSource.claim_id == Claim.id)
                    .where(Claim.tenant_id == tenant_id)
                    .where(ClaimSource.tenant_id == tenant_id)
                    .where(
                        ClaimSource.source_record_id.in_(
                            (previous_source_record_id, current_source_record_id)
                        )
                    )
                    .order_by(Claim.id.asc())
                )
            ).all()
        )
    )
    rendered_diff, truncated = build_readable_source_diff(previous_chunks, current_chunks)
    item_by_id = {item.id: item for item in items}
    owner_routes = sorted(
        {
            route
            for item in items
            for route in (item.governance_reviewer_subject, item.governance_owner_subject)
            if route
        }
    )
    item_id_strings = [str(item_id) for item_id in affected_item_ids]
    artifact = CandidateCurationArtifact(
        tenant_id=tenant_id,
        artifact_kind="candidate_source_drift",
        target_runtime="palace",
        target_surface=_review_target_url(resource.canonical_url),
        status="reviewable",
        source_item_ids=item_id_strings,
        source_digests={str(current_record.item_id): current_record.content_hash},
        source_resource_id=resource.id,
        previous_source_record_id=previous_record.id,
        current_source_record_id=current_record.id,
        affected_item_ids=item_id_strings,
        affected_claim_ids=[str(claim_id) for claim_id in affected_claim_ids],
        evidence_diff={
            "format": "unified_diff",
            "diff": rendered_diff,
            "truncated": truncated,
            "previous": {
                "source_record_id": str(previous_record.id),
                "source_version": previous_record.source_version,
                "content_hash": previous_record.content_hash,
            },
            "current": {
                "source_record_id": str(current_record.id),
                "source_version": current_record.source_version,
                "content_hash": current_record.content_hash,
            },
        },
        dedupe_key=dedupe_key,
        candidate_body=(
            f"Watched source changed. Review {len(affected_item_ids)} affected item(s) "
            f"and {len(affected_claim_ids)} affected claim(s).\n\n{rendered_diff}"
        ),
        privacy_review={
            "safe_for_review": True,
            "raw_sensitive_content_excluded": True,
            "contains_sensitive_content": False,
            "method": "deterministic_source_diff_redaction_v1",
        },
        eval_summary={
            "detector": "deterministic_source_version_diff_v1",
            "llm_used": False,
            "old_and_new_evidence_present": True,
        },
        approval={},
        created_by_principal="system:source-drift",
        metadata_={
            "proposal_type": "source_drift",
            "owner_routes": owner_routes or ["tenant-operator"],
            "review_inbox": {"resolved": False},
            "source_acl": {"tenant_scoped": True},
        },
    )
    # Retain this lookup so a future multi-item source projection cannot route
    # an item from a different result set into the evidence map by accident.
    if set(item_by_id) != set(affected_item_ids):
        raise SourceDriftError("affected source item ACL verification failed")
    try:
        async with db.begin_nested():
            db.add(artifact)
            await db.flush()
            _record_artifact_event(
                db,
                artifact=artifact,
                event_type="source_drift_created",
                previous_snapshot=None,
            )
            await db.flush()
    except IntegrityError as exc:
        existing = await db.scalar(
            select(CandidateCurationArtifact)
            .where(CandidateCurationArtifact.tenant_id == tenant_id)
            .where(CandidateCurationArtifact.dedupe_key == dedupe_key)
        )
        if existing is None:
            raise SourceDriftError("source drift dedupe conflict has no visible winner") from exc
        logger.info(
            "source_drift_retry_deduplicated tenant_id=%s resource_id=%s artifact_id=%s",
            tenant_id,
            resource_id,
            existing.id,
        )
        return SourceDriftProposalResult(artifact=existing, outcome="deduplicated")
    logger.info(
        "source_drift_created tenant_id=%s resource_id=%s artifact_id=%s affected_items=%d affected_claims=%d diff_truncated=%s",
        tenant_id,
        resource_id,
        artifact.id,
        len(affected_item_ids),
        len(affected_claim_ids),
        truncated,
    )
    return SourceDriftProposalResult(artifact=artifact, outcome="created")
