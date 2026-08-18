"""Deterministic free/busy + priority-weighted slot ranking.

Gemini is only used to synthesize the reasoning line shown to the user.
The slot generator and scoring are pure Python so the recommendation is
reproducible in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from level_core.config import get_settings
from level_core.schemas import (
    ActivityType,
    CachedEvent,
    Priority,
    Usual,
    UsualStatus,
)
from level_core.schemas.usual import hour_to_band


@dataclass
class CandidateSlot:
    start: datetime
    end: datetime
    score: float
    conflicts: list[str] = field(default_factory=list)
    aligned_priorities: list[str] = field(default_factory=list)
    aligned_usuals: list[str] = field(default_factory=list)

    @property
    def local_label(self) -> str:
        settings = get_settings()
        tz = ZoneInfo(settings.calendar_tz)
        return self.start.astimezone(tz).strftime("%a %b %d, %-I:%M %p")


def find_candidate_slots(
    *,
    events: list[CachedEvent],
    window_days: int,
    duration_minutes: int,
    starts_at: datetime | None = None,
    step_minutes: int = 30,
    day_start_hour: int = 8,
    day_end_hour: int = 21,
) -> list[CandidateSlot]:
    starts_at = starts_at or datetime.now(UTC)
    settings = get_settings()
    tz = ZoneInfo(settings.calendar_tz)

    busy = sorted(
        [
            (e.time.start.astimezone(UTC), e.time.end.astimezone(UTC))
            for e in events
        ]
    )

    slots: list[CandidateSlot] = []
    duration = timedelta(minutes=duration_minutes)
    step = timedelta(minutes=step_minutes)

    cursor_day = starts_at.astimezone(tz).replace(minute=0, second=0, microsecond=0)
    for d in range(window_days):
        day = cursor_day + timedelta(days=d)
        day_start = day.replace(hour=day_start_hour, minute=0)
        day_end = day.replace(hour=day_end_hour, minute=0)
        slot_start = max(day_start, starts_at.astimezone(tz))
        while slot_start + duration <= day_end:
            slot_end = slot_start + duration
            utc_start = slot_start.astimezone(UTC)
            utc_end = slot_end.astimezone(UTC)
            conflicts = _overlap_ids(busy, utc_start, utc_end, events)
            slots.append(
                CandidateSlot(
                    start=utc_start,
                    end=utc_end,
                    score=0.0,
                    conflicts=conflicts,
                )
            )
            slot_start += step
    return slots


def _overlap_ids(
    busy: list[tuple[datetime, datetime]],
    slot_start: datetime,
    slot_end: datetime,
    events: list[CachedEvent],
) -> list[str]:
    ids: list[str] = []
    for e in events:
        s = e.time.start.astimezone(UTC)
        t = e.time.end.astimezone(UTC)
        if s < slot_end and t > slot_start:
            ids.append(e.event_id)
    return ids


def score_slots(
    slots: list[CandidateSlot],
    *,
    activity_type: ActivityType,
    priorities: list[Priority],
    usuals: list[Usual],
    events_by_id: dict[str, CachedEvent],
) -> list[CandidateSlot]:
    settings = get_settings()
    tz = ZoneInfo(settings.calendar_tz)
    kept_usuals = [u for u in usuals if u.status == UsualStatus.KEPT]

    ranked: list[CandidateSlot] = []
    for slot in slots:
        base = 1.0
        if slot.conflicts:
            base -= 0.5 + 0.05 * len(slot.conflicts)
            for cid in slot.conflicts:
                ev = events_by_id.get(cid)
                if ev and ev.activity_type in {u.activity_type for u in kept_usuals}:
                    base -= 0.25

        local = slot.start.astimezone(tz)
        band = hour_to_band(local.hour)

        aligned_p: list[str] = []
        for p in priorities:
            if p.status != "kept":
                continue
            if activity_type in p.activity_types:
                base += 0.1 * (p.weight / 5.0)
                aligned_p.append(p.text)

        aligned_u: list[str] = []
        for u in kept_usuals:
            if u.activity_type == activity_type and u.weekday == local.weekday() and u.hour_band == band:
                base += 0.15
                aligned_u.append(u.display_summary)

        slot.score = round(max(-1.0, min(1.5, base)), 4)
        slot.aligned_priorities = aligned_p
        slot.aligned_usuals = aligned_u
        ranked.append(slot)
    return sorted(ranked, key=lambda s: s.score, reverse=True)
