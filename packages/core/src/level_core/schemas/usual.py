"""Usual (weekly repeating) events."""

from __future__ import annotations

from datetime import datetime
from enum import IntEnum, StrEnum

from pydantic import BaseModel, Field

from level_core.schemas.activity import ActivityType


class Weekday(IntEnum):
    MON = 0
    TUE = 1
    WED = 2
    THU = 3
    FRI = 4
    SAT = 5
    SUN = 6


class HourBand(StrEnum):
    """Coarse time-of-day bucket used to detect repeating patterns."""

    EARLY_MORNING = "early_morning"   # 05:00 - 08:59
    MORNING = "morning"               # 09:00 - 11:59
    MIDDAY = "midday"                 # 12:00 - 13:59
    AFTERNOON = "afternoon"           # 14:00 - 16:59
    EVENING = "evening"               # 17:00 - 19:59
    NIGHT = "night"                   # 20:00 - 23:59
    OVERNIGHT = "overnight"           # 00:00 - 04:59


def hour_to_band(hour: int) -> HourBand:
    if 5 <= hour <= 8:
        return HourBand.EARLY_MORNING
    if 9 <= hour <= 11:
        return HourBand.MORNING
    if 12 <= hour <= 13:
        return HourBand.MIDDAY
    if 14 <= hour <= 16:
        return HourBand.AFTERNOON
    if 17 <= hour <= 19:
        return HourBand.EVENING
    if 20 <= hour <= 23:
        return HourBand.NIGHT
    return HourBand.OVERNIGHT


class UsualStatus(StrEnum):
    PROPOSED = "proposed"
    KEPT = "kept"
    NOT_ME = "not_me"


class Usual(BaseModel):
    """Per gameplan: 'You usually have kid A pickup Wednesdays 3-4pm'."""

    usual_id: str
    person_id: str
    weekday: Weekday
    hour_band: HourBand
    activity_type: ActivityType
    display_summary: str
    source_event_uids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    status: UsualStatus = UsualStatus.PROPOSED
    version: int = 1
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    @staticmethod
    def compose_id(person_id: str, weekday: Weekday, hour_band: HourBand) -> str:
        return f"u:{person_id}:{int(weekday)}:{hour_band.value}"
