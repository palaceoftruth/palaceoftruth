"""Webhook URL validation and shared dispatch helper."""
import logging
import uuid

from fastapi import HTTPException

from app.services.webhook_payload import build_webhook_payload
from app.utils.outbound_http import OutboundUrlError, validate_public_http_url

logger = logging.getLogger(__name__)

def validate_webhook_url(url: str) -> str:
    """Validate and resolve an external webhook endpoint."""
    try:
        return validate_public_http_url(url)
    except OutboundUrlError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid webhook_url: {exc}") from exc


async def maybe_dispatch_webhook(arq_pool, job_id: str) -> None:
    """Read webhook config from the job record and enqueue deliver_webhook if set.

    Swallows all errors so a webhook dispatch failure never affects job status.
    """
    from app.database import async_session
    from app.models.job import Job

    try:
        try:
            session = async_session(info={"tenant_id": "__unbound__", "system_access": True})
        except TypeError:
            session = async_session()
        async with session as db:
            job = await db.get(Job, uuid.UUID(job_id))
            if job and job.webhook_url:
                await arq_pool.enqueue_job(
                    "deliver_webhook",
                    job_id=job_id,
                    webhook_url=job.webhook_url,
                    signing_key=job.signing_key,
                    payload_snapshot=build_webhook_payload(job),
                )
    except Exception as exc:
        logger.error("webhook dispatch failed for job %s: %s", job_id, exc)
