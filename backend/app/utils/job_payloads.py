from __future__ import annotations

import uuid
from typing import Any


def build_retry_payload(*, task_name: str, task_kwargs: dict[str, Any]) -> dict[str, Any]:
    """Persist the durable worker inputs needed to replay a generic ingest job."""
    return {
        "retry_task": {
            "name": task_name,
            "kwargs": task_kwargs,
        }
    }


def load_retry_task_from_payload(
    *,
    job_type: str,
    job_id: uuid.UUID | str,
    tenant_id: str,
    payload: dict[str, Any] | None,
    expected_task_name: str | None,
) -> tuple[str, dict[str, Any]] | None:
    """Restore a replayable worker task from persisted job payload.

    Older jobs may not have a retry payload, so callers should fall back to
    reconstructing the task from item state when this returns ``None``.
    """
    if not payload:
        return None

    retry_task = payload.get("retry_task")
    if not isinstance(retry_task, dict):
        return None

    task_name = retry_task.get("name")
    raw_kwargs = retry_task.get("kwargs")
    if not isinstance(task_name, str) or not isinstance(raw_kwargs, dict):
        return None
    if expected_task_name and task_name != expected_task_name:
        return None

    task_kwargs = dict(raw_kwargs)
    task_kwargs["job_id"] = str(job_id)
    task_kwargs["tenant_id"] = tenant_id
    return task_name, task_kwargs
