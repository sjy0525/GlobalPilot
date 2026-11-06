from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class MemoryItemOutput(BaseModel):
    memory_id: UUID
    content: str
    regions: str
    platforms: list[str]
    version: int
    update_at: datetime


class MemoryJobOutput(BaseModel):
    id: UUID
    status: Literal["pending", "running", "completed", "failed", "needs_review"]
    summary: str | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)


class BriefMemoriesOutput(BaseModel):
    items: list[MemoryItemOutput]
    jobs: list[MemoryJobOutput]
