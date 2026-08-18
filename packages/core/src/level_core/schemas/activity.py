"""Shared activity_type enum.

One source of truth used by usuals, reminders, priority tags, and
agenda cache classification. Any agent that emits an activity_type MUST
draw from this enum; call_agent() validates via Pydantic.
"""

from __future__ import annotations

from enum import StrEnum


class ActivityType(StrEnum):
    SPORTS_SOCCER = "sports.soccer"
    SPORTS_BASKETBALL = "sports.basketball"
    SPORTS_SWIM = "sports.swim"
    SPORTS_OTHER = "sports.other"
    SCHOOL_PICKUP = "school.pickup"
    SCHOOL_DROPOFF = "school.dropoff"
    SCHOOL_EVENT = "school.event"
    MEDICAL_APPT = "medical.appointment"
    MEDICAL_THERAPY = "medical.therapy"
    WORK = "work"
    FAMILY = "family"
    COMMUTE = "commute"
    PERSONAL = "personal"
    OTHER = "other"


ALL_ACTIVITY_TYPES: tuple[ActivityType, ...] = tuple(ActivityType)
