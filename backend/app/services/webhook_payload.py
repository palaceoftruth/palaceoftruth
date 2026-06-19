"""Shared webhook payload serialization."""
from __future__ import annotations

from app.models.job import Job


def build_webhook_payload(job: Job) -> dict:
    """Return the external webhook body for a job."""
    if job.job_type == "memory_artifact":
        from app.services.memory import serialize_memory_job

        return serialize_memory_job(job).model_dump(mode="json")

    return {
        "id": str(job.id),
        "item_id": str(job.item_id) if job.item_id else None,
        "job_type": job.job_type,
        "status": job.status,
        "progress": job.progress,
        "error_message": job.error_message,
        "duplicate_of": str(job.duplicate_of) if job.duplicate_of else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }
