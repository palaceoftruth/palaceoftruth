#!/usr/bin/env python3
"""Bounded, tenant-safe backfill for captured browser image candidates.

The command deliberately keeps the remote download outside the claim
transaction.  A short row-lock claim makes concurrent operators cooperate,
while the durable claim lets a later run recover an interrupted download.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import or_, select

from app.database import tenant_async_session
from app.models.item import Item
from app.models.job import Job
from app.services.bundle import persist_upload_artifact_bytes
from app.services.image_analysis import build_image_analysis_metadata
from app.utils.job_payloads import build_retry_payload
from app.workers.queues import enqueue_worker_job

logger = logging.getLogger(__name__)

CLAIM_STALE_AFTER = timedelta(minutes=15)
ELIGIBLE_STATUS = "captured_not_processed"
ACTIVE_JOB_STATUSES = ("queued", "processing")


@dataclass(frozen=True)
class BackfillReport:
    eligible: int = 0
    queued: int = 0
    skipped: int = 0
    retryable_failures: int = 0
    permanent_failures: int = 0
    hash_mismatches: int = 0


@dataclass(frozen=True)
class CandidateSnapshot:
    item_id: uuid.UUID
    tenant_id: str
    metadata: dict[str, Any]
    browser_image: dict[str, Any]
    claim_token: str


class BackfillFailure(RuntimeError):
    """A sanitized, operator-safe failure classification."""

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill captured browser image summaries.")
    parser.add_argument("--tenant-id", required=True, help="Tenant to backfill.")
    parser.add_argument("--limit", type=int, default=100, help="Maximum candidates to inspect.")
    parser.add_argument("--dry-run", action="store_true", help="Count eligible candidates without writes or downloads.")
    return parser.parse_args()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _claim_is_stale(browser_image: dict[str, Any], *, now: datetime) -> bool:
    claim = browser_image.get("backfill_claim")
    if not isinstance(claim, dict):
        return False
    claimed_at = _as_utc(claim.get("claimed_at"))
    return claimed_at is None or now - claimed_at >= CLAIM_STALE_AFTER


def _eligible(item: Item, *, now: datetime | None = None) -> bool:
    """Apply the eligibility gate again in Python after the bounded SQL query."""
    if item.deleted_at is not None or item.status in {"queued", "processing", "ready", "deleted"}:
        return False
    metadata = item.metadata_ if isinstance(item.metadata_, dict) else {}
    browser_image = metadata.get("browser_capture_image")
    if not isinstance(browser_image, dict):
        return False
    status = browser_image.get("status")
    if status in {"completed", "queued", "processing", "deleted", "permanent_failed", "failed_permanently"}:
        return False
    claim = browser_image.get("backfill_claim")
    if isinstance(claim, dict) and not _claim_is_stale(browser_image, now=now or _utc_now()):
        return False
    error = browser_image.get("backfill_error")
    if isinstance(error, dict):
        return error.get("classification") == "retryable"
    return status == ELIGIBLE_STATUS or isinstance(claim, dict)


def _candidate_query(*, tenant_id: str, limit: int, now: datetime):
    """Select only eligible-shaped rows before applying the operator limit."""

    browser = Item.metadata_["browser_capture_image"]
    status = browser["status"].as_string()
    error_class = browser["backfill_error"]["classification"].as_string()
    claim_token = browser["backfill_claim"]["token"].as_string()
    claimed_at = browser["backfill_claim"]["claimed_at"].as_string()
    cutoff = now - CLAIM_STALE_AFTER
    return (
        select(Item)
        .where(Item.tenant_id == tenant_id)
        .where(Item.source_type == "image_candidate")
        .where(Item.deleted_at.is_(None))
        .where(Item.status.not_in(("queued", "processing", "ready", "deleted")))
        .where(status.not_in(("completed", "queued", "processing", "deleted", "permanent_failed", "failed_permanently")))
        .where(
            or_(
                claim_token.is_(None),
                claimed_at.is_(None),
                claimed_at <= cutoff.isoformat(),
            )
        )
        .where(or_(status == ELIGIBLE_STATUS, error_class == "retryable", claim_token.is_not(None)))
        .order_by(Item.created_at.asc(), Item.id.asc())
        .limit(limit)
    )


async def _has_active_job(db: Any, *, tenant_id: str, item_id: uuid.UUID) -> bool:
    result = await db.execute(
        select(Job.id)
        .where(Job.tenant_id == tenant_id)
        .where(Job.item_id == item_id)
        .where(Job.job_type == "image")
        .where(Job.status.in_(ACTIVE_JOB_STATUSES))
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def _claim_one(db: Any, *, tenant_id: str, item: Item, now: datetime) -> CandidateSnapshot | None:
    """Claim one row in a short transaction and return detached source data."""
    # The caller selected candidates in deterministic order. Re-read by ID
    # under a lock because the initial selection is intentionally not held.
    locked = await db.execute(
        select(Item)
        .where(Item.tenant_id == tenant_id)
        .where(Item.id == item.id)
        .where(Item.source_type == "image_candidate")
        .where(Item.deleted_at.is_(None))
        .with_for_update(skip_locked=True)
    )
    row = locked.scalar_one_or_none()
    if row is None or not _eligible(row, now=now) or await _has_active_job(db, tenant_id=tenant_id, item_id=row.id):
        return None

    metadata = dict(row.metadata_ or {})
    browser_image = dict(metadata["browser_capture_image"])
    token = uuid.uuid4().hex
    browser_image["backfill_claim"] = {
        "token": token,
        "claimed_at": now.isoformat(),
    }
    metadata["browser_capture_image"] = browser_image
    row.metadata_ = metadata
    await db.commit()
    return CandidateSnapshot(
        item_id=row.id,
        tenant_id=tenant_id,
        metadata=metadata,
        browser_image=browser_image,
        claim_token=token,
    )


def _required_source(snapshot: CandidateSnapshot) -> tuple[str, str, str, str, str]:
    browser = snapshot.browser_image
    source_url = browser.get("source_post_url")
    candidate_url = browser.get("candidate_url")
    final_url = browser.get("final_url")
    expected_hash = browser.get("byte_hash")
    media_type = browser.get("media_type")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (source_url, candidate_url, final_url, expected_hash, media_type)
    ):
        raise BackfillFailure("missing_metadata", "required image provenance is missing", retryable=False)
    if len(expected_hash) != 64 or any(char not in "0123456789abcdefABCDEF" for char in expected_hash):
        raise BackfillFailure("missing_metadata", "stored image hash is invalid", retryable=False)
    return source_url, candidate_url, final_url, expected_hash.lower(), media_type


def _status_code(exc: Exception) -> int | None:
    value = getattr(exc, "status_code", None)
    if isinstance(value, int):
        return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def _classify_download_error(exc: Exception) -> BackfillFailure:
    explicit_retryable = getattr(exc, "retryable", None)
    explicit_code = getattr(exc, "code", None)
    if isinstance(explicit_retryable, bool):
        code = explicit_code if isinstance(explicit_code, str) else "download_error"
        messages = {
            "rate_limited": "image host rate limited the request",
            "server_error": "image host returned a server error",
            "network_error": "image download failed due to a network error",
        }
        return BackfillFailure(
            code,
            messages.get(code, "image download failed" if explicit_retryable else "image candidate is invalid"),
            retryable=explicit_retryable,
        )
    status = _status_code(exc)
    name = exc.__class__.__name__.lower()
    cause = getattr(exc, "__cause__", None)
    if cause is not None and (
        "timeout" in cause.__class__.__name__.lower()
        or "requesterror" in cause.__class__.__name__.lower()
    ):
        return BackfillFailure("network_error", "image download failed due to a network error", retryable=True)
    if status == 429:
        return BackfillFailure("rate_limited", "image host rate limited the request", retryable=True)
    if status is not None and 500 <= status <= 599:
        return BackfillFailure("server_error", "image host returned a server error", retryable=True)
    if "timeout" in name or isinstance(exc, (TimeoutError, ConnectionError)):
        return BackfillFailure("network_timeout", "image download timed out", retryable=True)
    if "requesterror" in name or "network" in name:
        return BackfillFailure("network_error", "image download failed due to a network error", retryable=True)
    if "outboundurl" in name or status in {400, 401, 403, 404, 405, 409, 422}:
        return BackfillFailure("invalid_url", "image URL is not allowed", retryable=False)
    return BackfillFailure("download_error", "image download failed", retryable=True)


async def _download_candidate(source_url: str, candidate_url: str) -> Any:
    """Call the shared SSRF-safe candidate service (kept import-lazy for tests)."""
    import httpx

    from app.schemas.ingest import BrowserImageCandidate
    from app.services.image_candidates import (
        HTTP_TIMEOUT,
        download_image_candidate,
        validate_candidate_relationship,
    )

    candidate = BrowserImageCandidate(url=candidate_url, source_post_url=source_url)
    normalized_url = validate_candidate_relationship(
        candidate=candidate,
        source_url=source_url,
        resolved_kind="social_post",
    )
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, trust_env=False) as client:
        return await download_image_candidate(
            client=client,
            candidate=candidate,
            normalized_candidate_url=normalized_url,
            source_url=source_url,
        )


def _extension_for(media_type: str, downloaded: Any) -> str:
    extension = getattr(downloaded, "extension", None)
    if isinstance(extension, str) and extension.startswith("."):
        return extension
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }.get(media_type, "")


def _queued_metadata(
    snapshot: CandidateSnapshot,
    downloaded: Any,
    storage_path: str,
    extension: str,
    content: bytes,
    byte_hash: str,
) -> dict[str, Any]:
    browser = dict(snapshot.browser_image)
    browser.pop("backfill_claim", None)
    browser.pop("backfill_error", None)
    browser["status"] = "queued"
    media_type = str(getattr(downloaded, "media_type", browser.get("media_type")))
    # Keep the original citation URL. Hash verification protects the evidence
    # bytes; a later redirect must not rewrite captured provenance.
    browser["artifact"] = {
        "filename": f"{snapshot.item_id}{extension}",
        "media_type": media_type,
        "extension": extension,
        "storage_path": storage_path,
    }
    metadata = dict(snapshot.metadata)
    metadata["browser_capture_image"] = browser
    metadata["filename"] = f"{snapshot.item_id}{extension}"
    metadata["media_type"] = media_type
    metadata.update(
        build_image_analysis_metadata(
            filename=str(metadata["filename"]),
            media_type=media_type,
            extension=extension,
            image_bytes=content,
            byte_hash=byte_hash,
            artifact_storage_path=storage_path,
            status="queued",
        )
    )
    metadata["image_analysis"]["artifact"]["source"] = "browser_image_candidate"
    return metadata


async def _record_failure(snapshot: CandidateSnapshot, failure: BackfillFailure, *, session_factory: Callable[[str], Any]) -> bool:
    async with session_factory(snapshot.tenant_id) as db:
        result = await db.execute(
            select(Item)
            .where(Item.tenant_id == snapshot.tenant_id)
            .where(Item.id == snapshot.item_id)
            .where(Item.deleted_at.is_(None))
            .with_for_update(skip_locked=True)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return False
        metadata = dict(row.metadata_ or {})
        browser = dict(metadata.get("browser_capture_image") or {})
        claim = browser.get("backfill_claim")
        if not isinstance(claim, dict) or claim.get("token") != snapshot.claim_token:
            return False
        browser.pop("backfill_claim", None)
        browser["backfill_error"] = {
            "classification": "retryable" if failure.retryable else "permanent",
            "code": failure.code,
            "message": failure.message,
            "attempted_at": _utc_now().isoformat(),
            "source": "image_summary_backfill",
        }
        browser["status"] = ELIGIBLE_STATUS if failure.retryable else "permanent_failed"
        metadata["browser_capture_image"] = browser
        row.metadata_ = metadata
        await db.commit()
    return True


async def _materialize(
    snapshot: CandidateSnapshot,
    downloaded: Any,
    *,
    session_factory: Callable[[str], Any],
    queue_pool: Any,
    enqueue: Callable[..., Awaitable[Any]],
) -> str:
    _source_url, _candidate_url, _final_url, expected_hash, stored_media_type = _required_source(snapshot)
    content = getattr(downloaded, "content", None)
    if not isinstance(content, bytes) or not content:
        raise BackfillFailure("unsupported_media", "image response contained no supported bytes", retryable=False)
    actual_hash = hashlib.sha256(content).hexdigest()
    reported_hash = getattr(downloaded, "byte_hash", actual_hash)
    if actual_hash != expected_hash or reported_hash != expected_hash:
        raise BackfillFailure("hash_mismatch", "downloaded bytes do not match captured evidence", retryable=False)
    media_type = str(getattr(downloaded, "media_type", stored_media_type))
    if not media_type.startswith("image/"):
        raise BackfillFailure("unsupported_media", "downloaded media type is not an image", retryable=False)
    if media_type != stored_media_type:
        raise BackfillFailure("unsupported_media", "downloaded media type changed", retryable=False)
    extension = _extension_for(media_type, downloaded)
    if not extension:
        raise BackfillFailure("unsupported_media", "downloaded image type is not supported", retryable=False)
    async with session_factory(snapshot.tenant_id) as db:
        result = await db.execute(
            select(Item)
            .where(Item.tenant_id == snapshot.tenant_id)
            .where(Item.id == snapshot.item_id)
            .where(Item.deleted_at.is_(None))
            .with_for_update(skip_locked=True)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return "skipped"
        current = row.metadata_ if isinstance(row.metadata_, dict) else {}
        current_browser = current.get("browser_capture_image") if isinstance(current, dict) else None
        claim = current_browser.get("backfill_claim") if isinstance(current_browser, dict) else None
        if not isinstance(claim, dict) or claim.get("token") != snapshot.claim_token:
            return "skipped"
        if await _has_active_job(db, tenant_id=snapshot.tenant_id, item_id=snapshot.item_id):
            return "skipped"
        storage_path = persist_upload_artifact_bytes(
            content,
            tenant_id=snapshot.tenant_id,
            item_id=snapshot.item_id,
            extension=extension,
        )
        metadata = _queued_metadata(
            snapshot,
            downloaded,
            storage_path,
            extension,
            content,
            actual_hash,
        )
        job = Job(item_id=row.id, job_type="image", status="queued", progress=0, tenant_id=snapshot.tenant_id)
        db.add(job)
        await db.flush()
        job.payload = build_retry_payload(
            task_name="process_image",
            task_kwargs={"image_metadata": metadata},
        )
        row.metadata_ = metadata
        row.status = "processing"
        await db.commit()
        job_id = str(job.id)
    try:
        await enqueue(
            queue_pool,
            "process_image",
            job_id=job_id,
            tenant_id=snapshot.tenant_id,
            image_metadata=metadata,
        )
    except Exception as exc:
        logger.warning("image backfill enqueue failed for item %s: %s", snapshot.item_id, exc.__class__.__name__)
        await _record_enqueue_failure(snapshot, job_id, session_factory=session_factory)
        return "retryable"
    return "queued"


async def _record_enqueue_failure(snapshot: CandidateSnapshot, job_id: str, *, session_factory: Callable[[str], Any]) -> None:
    async with session_factory(snapshot.tenant_id) as db:
        job = await db.scalar(
            select(Job)
            .where(Job.tenant_id == snapshot.tenant_id)
            .where(Job.id == uuid.UUID(job_id))
        )
        if job is None:
            return
        job.status = "failed"
        job.progress = 0
        job.error_message = "Failed to enqueue image backfill task"
        job.completed_at = _utc_now()
        row = await db.scalar(
            select(Item)
            .where(Item.tenant_id == snapshot.tenant_id)
            .where(Item.id == snapshot.item_id)
        )
        if row is not None:
            metadata = dict(row.metadata_ or {})
            browser = dict(metadata.get("browser_capture_image") or {})
            browser.pop("backfill_claim", None)
            browser["status"] = ELIGIBLE_STATUS
            browser["backfill_error"] = {
                "classification": "retryable",
                "code": "enqueue_failed",
                "message": "worker queue unavailable",
                "attempted_at": _utc_now().isoformat(),
                "source": "image_summary_backfill",
            }
            metadata["browser_capture_image"] = browser
            analysis = dict(metadata.get("image_analysis") or {})
            analysis["status"] = "failed"
            vision = dict(analysis.get("vision") or {})
            vision["error"] = {
                "message": "worker queue unavailable",
                "retryable": True,
                "code": "enqueue_failed",
            }
            analysis["vision"] = vision
            metadata["image_analysis"] = analysis
            row.metadata_ = metadata
            row.status = "failed"
        await db.commit()


async def run_backfill(
    *,
    tenant_id: str,
    limit: int,
    dry_run: bool = False,
    session_factory: Callable[[str], Any] = tenant_async_session,
    downloader: Callable[[str, str], Awaitable[Any]] = _download_candidate,
    queue_pool: Any = None,
    enqueue: Callable[..., Awaitable[Any]] = enqueue_worker_job,
) -> BackfillReport:
    """Run one bounded tenant backfill; dependencies are injectable for tests."""
    if not tenant_id.strip():
        raise ValueError("tenant_id must not be blank")
    if limit < 1:
        raise ValueError("limit must be positive")

    # Keep the selection read-only, deterministic, and bounded. Eligibility is
    # checked again under the claim lock to handle concurrent operators.
    async with session_factory(tenant_id) as db:
        selection_now = _utc_now()
        result = await db.execute(_candidate_query(tenant_id=tenant_id, limit=limit, now=selection_now))
        candidates = list(result.scalars().all())
        if dry_run:
            eligible = sum(1 for item in candidates if _eligible(item))
            report = BackfillReport(eligible=eligible, skipped=len(candidates) - eligible)
            _print_report(report)
            return report

    report = BackfillReport()
    for item in candidates:
        now = _utc_now()
        # Count only rows that pass the pre-claim gate. A race becomes skipped.
        async with session_factory(tenant_id) as db:
            if not _eligible(item, now=now):
                report = BackfillReport(**{**report.__dict__, "skipped": report.skipped + 1})
                continue
            snapshot = await _claim_one(db, tenant_id=tenant_id, item=item, now=now)
        if snapshot is None:
            report = BackfillReport(**{**report.__dict__, "skipped": report.skipped + 1})
            continue
        report = BackfillReport(**{**report.__dict__, "eligible": report.eligible + 1})
        try:
            source_url, candidate_url, _final_url, _expected_hash, _media = _required_source(snapshot)
            downloaded = await downloader(source_url, candidate_url)
            outcome = await _materialize(
                snapshot,
                downloaded,
                session_factory=session_factory,
                queue_pool=queue_pool,
                enqueue=enqueue,
            )
        except BackfillFailure as failure:
            await _record_failure(snapshot, failure, session_factory=session_factory)
            key = "hash_mismatches" if failure.code == "hash_mismatch" else ("retryable_failures" if failure.retryable else "permanent_failures")
            report = BackfillReport(**{**report.__dict__, key: getattr(report, key) + 1})
        except Exception as exc:
            failure = _classify_download_error(exc)
            await _record_failure(snapshot, failure, session_factory=session_factory)
            key = "retryable_failures" if failure.retryable else "permanent_failures"
            report = BackfillReport(**{**report.__dict__, key: getattr(report, key) + 1})
        else:
            if outcome == "queued":
                report = BackfillReport(**{**report.__dict__, "queued": report.queued + 1})
            elif outcome == "retryable":
                report = BackfillReport(**{**report.__dict__, "retryable_failures": report.retryable_failures + 1})
            else:
                report = BackfillReport(**{**report.__dict__, "skipped": report.skipped + 1})
    _print_report(report)
    return report


def _print_report(report: BackfillReport) -> None:
    print(
        "eligible={eligible} queued={queued} skipped={skipped} "
        "retryable_failures={retryable_failures} permanent_failures={permanent_failures} "
        "hash_mismatches={hash_mismatches}".format(**report.__dict__)
    )


async def _main() -> None:
    args = _parse_args()
    if args.dry_run:
        await run_backfill(tenant_id=args.tenant_id, limit=args.limit, dry_run=True)
        return
    from arq import create_pool
    from app.config import make_redis_settings
    from app.workers.serialization import job_deserializer, job_serializer

    pool = await create_pool(
        make_redis_settings(),
        job_serializer=job_serializer,
        job_deserializer=job_deserializer,
    )
    try:
        await run_backfill(tenant_id=args.tenant_id, limit=args.limit, dry_run=args.dry_run, queue_pool=pool)
    finally:
        await pool.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
