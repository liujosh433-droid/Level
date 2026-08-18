"""User-declared priorities (used at booking time)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from level_core.schemas.activity import ActivityType


class PriorityStatus(StrEnum):
    KEPT = "kept"
    NOT_ME = "not_me"


class Priority(BaseModel):
    priority_id: str
    text: str
    weight: int = Field(default=3, ge=1, le=5)
    activity_types: list[ActivityType] = Field(default_factory=list)
    source: str = "chat"  # chat | profile
    status: PriorityStatus = PriorityStatus.KEPT
    source_span: str | None = None
    version: int = 1
    updated_at: datetime = Field(default_factory=datetime.utcnow)
