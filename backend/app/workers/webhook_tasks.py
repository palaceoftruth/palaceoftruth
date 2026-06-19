"""ARQ task for delivering webhook callbacks on terminal job states."""
import hashlib
import hmac
import json
import logging
import uuid

import httpx

from app.database import async_session
from app.models.job import Job
from app.services.webhook_payload import build_webhook_payload

logger = logging.getLogger(__name__)

# Retry schedule: attempt 1 immediately, attempt 2 after 30 s, attempt 3 after 5 min
_RETRY_DELAYS = [0, 30, 300]
_MAX_ATTEMPTS = len(_RETRY_DELAYS)

# Per-attempt connect + read timeout (seconds)
_HTTP_TIMEOUT = httpx.Timeout(10.0)


def _sign(body: bytes, signing_key: str) -> str:
    """Return X-Hub-Signature-256 value: 'sha256=<hex>'."""
    sig = hmac.new(signing_key.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


async def deliver_webhook(
    ctx: dict,
    job_id: str,
    webhook_url: str,
    signing_key: str | None = None,
    attempt: int = 0,
    payload_snapshot: dict | None = None,
) -> None:
    """Deliver a webhook POST for the given job_id.

    Self-managed retry: on connection error, timeout, or HTTP 5xx, re-enqueues
    itself with attempt+1 up to _MAX_ATTEMPTS. After exhaustion logs and stops.
    """
    async with async_session() as db:
        job = await db.get(Job, uuid.UUID(job_id))

    if not job:
        logger.warning("deliver_webhook: job %s not found, skipping", job_id)
        return

    # Preserve the terminal state that originally triggered delivery so retries
    # cannot drift if the job gets requeued or otherwise updated later.
    payload = payload_snapshot or build_webhook_payload(job)
    body = json.dumps(payload, default=str).encode()

    headers = {"Content-Type": "application/json"}
    if signing_key:
        headers["X-Hub-Signature-256"] = _sign(body, signing_key)

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            response = await client.post(webhook_url, content=body, headers=headers)

        if response.status_code < 500:
            # 2xx, 3xx, 4xx — treat as delivered (caller's problem if 4xx)
            logger.info(
                "webhook delivered job_id=%s url=%s status=%d attempt=%d",
                job_id, webhook_url, response.status_code, attempt,
            )
            return

        # 5xx — retriable
        raise _RetriableError(f"HTTP {response.status_code}")

    except (httpx.ConnectError, httpx.TimeoutException, _RetriableError) as exc:
        next_attempt = attempt + 1
        if next_attempt >= _MAX_ATTEMPTS:
            logger.error(
                "webhook exhausted retries job_id=%s url=%s attempts=%d last_error=%s",
                job_id, webhook_url, _MAX_ATTEMPTS, exc,
            )
            return

        delay = _RETRY_DELAYS[next_attempt]
        logger.warning(
            "webhook retry scheduled job_id=%s url=%s attempt=%d delay=%ds error=%s",
            job_id, webhook_url, next_attempt, delay, exc,
        )
        await ctx["redis"].enqueue_job(
            "deliver_webhook",
            job_id=job_id,
            webhook_url=webhook_url,
            signing_key=signing_key,
            attempt=next_attempt,
            payload_snapshot=payload,
            _defer_by=delay,
        )


class _RetriableError(Exception):
    """Raised for HTTP 5xx responses to trigger the retry path."""
