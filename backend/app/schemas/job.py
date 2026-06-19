import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class JobProgressEventResponse(BaseModel):
    phase: str
    status: str
    progress: int | None = None
    message: str | None = None
    metadata_: dict | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class JobResponse(BaseModel):
    id: uuid.UUID
    item_id: uuid.UUID | None
    job_type: str
    status: str
    progress: int
    error_message: str | None
    duplicate_of: uuid.UUID | None = None
    created_at: datetime
    completed_at: datetime | None
    recent_progress_events: list[JobProgressEventResponse] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class JobListResponse(BaseModel):
    jobs: list[JobResponse]
    total: int
