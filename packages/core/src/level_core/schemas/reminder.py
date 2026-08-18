"""User reminders attached to inferred events (e.g. soccer shoes)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from level_core.schemas.activity import ActivityType


class ReminderStatus(StrEnum):
    ACTIVE = "active"
    DISMISSED = "dismissed"


class ReminderMatch(BaseModel):
    """Structured match spec. F8 uses (person_id, activity_type) equality."""

    person_id: str | None = None
    activity_type: ActivityType


class Reminder(BaseModel):
    reminder_id: str
    text: str
    match: ReminderMatch
    lead_minutes: int = 60
    status: ReminderStatus = ReminderStatus.ACTIVE
    source_span: str | None = None
    version: int = 1
    updated_at: datetime = Field(default_factory=datetime.utcnow)
