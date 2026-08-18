"""Cached (denormalized) Google Calendar events.

We deliberately DO NOT store raw description or attendee email addresses.
Only stable first-name tokens make it into `attendee_tokens` for role match.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from level_core.schemas.activity import ActivityType


class EventTime(BaseModel):
    start: datetime
    end: datetime
    tz: str
    all_day: bool = False


class CachedEvent(BaseModel):
    event_id: str
    calendar_id: str
    summary: str
    time: EventTime
    location: str | None = None

    attendee_tokens: list[str] = Field(default_factory=list)

    activity_type: ActivityType | None = None
    classified_at: datetime | None = None

    matched_person_ids: list[str] = Field(default_factory=list)
    matched_reminder_ids: list[str] = Field(default_factory=list)

    origin: Literal["google", "level"] = "google"
    level_reason: str | None = None

    etag: str | None = None
    version: int = 1
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class DailyAgenda(BaseModel):
    """Denormalized index for O(1) /today reads."""

    date: str  # yyyy-mm-dd in user's TZ
    event_ids: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=datetime.utcnow)
