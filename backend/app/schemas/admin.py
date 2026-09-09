"""Schemas for operator-only administrative mutations."""

from pydantic import BaseModel, ConfigDict


class McpOAuthClientSharedMemoryPromotionRequest(BaseModel):
    """Explicit opt-in switch for the shared-memory promotion capability."""

    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool
