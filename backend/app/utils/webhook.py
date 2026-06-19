"""Webhook URL validation and shared dispatch helper."""
import ipaddress
import logging
import uuid
from urllib.parse import urlparse

from fastapi import HTTPException

from app.services.webhook_payload import build_webhook_payload

logger = logging.getLogger(__name__)

_PRIVATE_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local
]

_BLOCKED_SUFFIXES = (".svc.cluster.local", ".local", "localhost")


def validate_webhook_url(url: str) -> str:
    """Validate that url is a safe, external http/https endpoint.

    Raises HTTPException(422) for:
    - Non-http/https schemes
    - Cluster-internal or loopback hostnames
    - Private / link-local IP address literals

    Returns the original url string on success.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=422, detail="webhook_url must use http or https scheme")

    host = parsed.hostname or ""
    if not host:
        raise HTTPException(status_code=422, detail="webhook_url must include a valid host")

    # Block known internal hostname patterns
    if host in ("localhost",) or any(host.endswith(s) for s in _BLOCKED_SUFFIXES):
        raise HTTPException(status_code=422, detail="webhook_url must not target internal hosts")

    # Block private IP address literals (not DNS names — those are checked at delivery time)
    try:
        addr = ipaddress.ip_address(host)
        if any(addr in net for net in _PRIVATE_NETS):
            raise HTTPException(status_code=422, detail="webhook_url must not target private IP ranges")
    except ValueError:
        pass  # hostname, not an IP literal — accepted

    return url


async def maybe_dispatch_webhook(arq_pool, job_id: str) -> None:
    """Read webhook config from the job record and enqueue deliver_webhook if set.

    Swallows all errors so a webhook dispatch failure never affects job status.
    """
    from app.database import async_session
    from app.models.job import Job

    try:
        async with async_session() as db:
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
