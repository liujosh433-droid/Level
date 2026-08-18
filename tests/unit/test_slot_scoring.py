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


def test_find_time_window_from_when_not_what() -> None:
    from level_core.schedule.slots import infer_event_kind, plan_label_from_message

    pottery = infer_event_kind("find a time for pottery class this week")
    assert pottery.label == "pottery class"
    assert pottery.windows == ((8, 20),)
    assert pottery.weekdays_only is False

    smog = infer_event_kind("find a time to book a smog check")
    assert smog.label == "smog check"
    assert smog.windows == ((8, 20),)

    evening = infer_event_kind("find a time in the evening for whatever")
    assert evening.windows == ((17, 21),)

    weekday = infer_event_kind("find a time on a weekday")
    assert weekday.weekdays_only is True
    assert weekday.windows == ((9, 17),)

    assert plan_label_from_message("find a time this week") == ""
    assert plan_label_from_message("find lunch this week") == "lunch"
    assert plan_label_from_message("when can I grab coffee this week") == "coffee"
    from level_core.schedule.slots import calendar_title_from_label

    assert calendar_title_from_label("lunch with a friend") == "Lunch"
    assert calendar_title_from_label("pottery class") == "Pottery class"
    assert calendar_title_from_label("doctor's appointment") == "Doctor's appointment"


def test_recommendations_skip_overnight_and_busy() -> None:
    from zoneinfo import ZoneInfo

    from level_core.config import get_settings
    from level_core.schedule.slots import infer_event_kind, recommend_slots

    tz = ZoneInfo(get_settings().calendar_tz)
    now = datetime(2026, 8, 18, 14, 0, tzinfo=tz)  # Tuesday afternoon
    kind = infer_event_kind("find a time in the evening")
    busy_start = datetime(2026, 8, 18, 18, 0, tzinfo=tz)
    events = [
        CachedEvent(
            event_id="e-busy",
            calendar_id="primary",
            summary="already booked",
            time=EventTime(
                start=busy_start,
                end=busy_start + timedelta(minutes=90),
                tz=get_settings().calendar_tz,
            ),
        )
    ]
    picks = recommend_slots(
        events=events,
        kind=kind,
        starts_at=now,
        window_days=6,
        priorities=[],
        usuals=[],
        limit=4,
    )
    assert picks
    for slot in picks:
        local = slot.start.astimezone(tz)
        assert 17 <= local.hour < 21
        assert local.hour != 3
        slot_end = slot.end.astimezone(tz)
        if local.date() == busy_start.date():
            assert not (local < busy_start + timedelta(minutes=90) and slot_end > busy_start)
    days = {s.start.astimezone(tz).date() for s in picks}
    assert len(days) == len(picks)


def test_empty_calendar_still_avoids_3am() -> None:
    from zoneinfo import ZoneInfo

    from level_core.config import get_settings
    from level_core.schedule.slots import infer_event_kind, recommend_slots

    tz = ZoneInfo(get_settings().calendar_tz)
    now = datetime(2026, 8, 18, 8, 0, tzinfo=tz)
    picks = recommend_slots(
        events=[],
        kind=infer_event_kind("when's the best time to meet"),
        starts_at=now,
        window_days=2,
        priorities=[],
        usuals=[],
        limit=4,
    )
    assert picks
    for slot in picks:
        hour = slot.start.astimezone(tz).hour
        assert 8 <= hour < 20


def test_unknown_event_uses_default_waking_hours() -> None:
    from zoneinfo import ZoneInfo

    from level_core.config import get_settings
    from level_core.schedule.slots import infer_event_kind, recommend_slots

    tz = ZoneInfo(get_settings().calendar_tz)
    now = datetime(2026, 8, 18, 8, 0, tzinfo=tz)
    picks = recommend_slots(
        events=[],
        kind=infer_event_kind("find a time for a smog check this week"),
        starts_at=now,
        window_days=3,
        priorities=[],
        usuals=[],
        limit=4,
    )
    assert picks
    for slot in picks:
        hour = slot.start.astimezone(tz).hour
        assert 8 <= hour < 20
