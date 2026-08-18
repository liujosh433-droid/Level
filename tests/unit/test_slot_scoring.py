"""Priority-weighted slot ranking is deterministic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from level_core.schedule.slots import find_candidate_slots, score_slots
from level_core.schemas import (
    ActivityType,
    CachedEvent,
    EventTime,
    HourBand,
    Priority,
    Usual,
    UsualStatus,
    Weekday,
)


def _event(start: datetime, activity: ActivityType | None = None) -> CachedEvent:
    return CachedEvent(
        event_id=f"e-{start.isoformat()}",
        calendar_id="primary",
        summary="busy",
        time=EventTime(start=start, end=start + timedelta(minutes=60), tz="UTC"),
        activity_type=activity,
    )


def test_slot_finder_respects_busy_and_priority_weight() -> None:
    now = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
    events = [_event(now)]
    slots = find_candidate_slots(
        events=events,
        window_days=1,
        duration_minutes=60,
        starts_at=now,
    )
    ranked = score_slots(
        slots,
        activity_type=ActivityType.PERSONAL,
        priorities=[
            Priority(
                priority_id="p1",
                text="mornings are for me",
                weight=5,
                activity_types=[ActivityType.PERSONAL],
                status="kept",
            )
        ],
        usuals=[],
        events_by_id={e.event_id: e for e in events},
    )
    assert ranked
    top = ranked[0]
    assert top.aligned_priorities == ["mornings are for me"]
    assert top.score > 0


def test_conflict_with_kept_usual_penalizes_slot() -> None:
    now = datetime(2026, 8, 20, 15, 0, tzinfo=UTC)
    pickup = _event(now, ActivityType.SCHOOL_PICKUP)
    slots = find_candidate_slots(
        events=[pickup],
        window_days=1,
        duration_minutes=60,
        starts_at=now,
    )
    kept_usual = Usual(
        usual_id="u1",
        person_id="p1",
        weekday=Weekday(now.weekday()),
        hour_band=HourBand.AFTERNOON,
        activity_type=ActivityType.SCHOOL_PICKUP,
        display_summary="Alpha pickup",
        status=UsualStatus.KEPT,
    )
    ranked = score_slots(
        slots,
        activity_type=ActivityType.PERSONAL,
        priorities=[],
        usuals=[kept_usual],
        events_by_id={pickup.event_id: pickup},
    )
    conflicting = next(s for s in ranked if pickup.event_id in s.conflicts)
    assert conflicting.score < 1.0
