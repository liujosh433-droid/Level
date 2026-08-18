"""Reminder matching is structured equality (person_id, activity_type)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from level_core.calendar.enrich import _reminder_matches
from level_core.schemas import (
    ActivityType,
    CachedEvent,
    EventTime,
    Reminder,
    ReminderMatch,
)


def _event(activity: ActivityType, people: list[str]) -> CachedEvent:
    now = datetime.now(UTC)
    return CachedEvent(
        event_id="e1",
        calendar_id="primary",
        summary="event",
        time=EventTime(start=now, end=now + timedelta(hours=1), tz="UTC"),
        activity_type=activity,
        matched_person_ids=people,
    )


def test_match_when_person_and_activity_align() -> None:
    r = Reminder(
        reminder_id="r1",
        text="Bring soccer shoes",
        match=ReminderMatch(person_id="p1", activity_type=ActivityType.SPORTS_SOCCER),
    )
    e = _event(ActivityType.SPORTS_SOCCER, ["p1"])
    assert _reminder_matches(r, e)


def test_no_match_when_activity_differs() -> None:
    r = Reminder(
        reminder_id="r1",
        text="Bring soccer shoes",
        match=ReminderMatch(person_id="p1", activity_type=ActivityType.SPORTS_SOCCER),
    )
    e = _event(ActivityType.SPORTS_BASKETBALL, ["p1"])
    assert not _reminder_matches(r, e)


def test_no_match_when_person_missing() -> None:
    r = Reminder(
        reminder_id="r1",
        text="Bring soccer shoes",
        match=ReminderMatch(person_id="p1", activity_type=ActivityType.SPORTS_SOCCER),
    )
    e = _event(ActivityType.SPORTS_SOCCER, ["p2"])
    assert not _reminder_matches(r, e)


def test_matches_any_person_when_reminder_is_generic() -> None:
    r = Reminder(
        reminder_id="r1",
        text="Bring waterbottle",
        match=ReminderMatch(person_id=None, activity_type=ActivityType.SPORTS_SOCCER),
    )
    e = _event(ActivityType.SPORTS_SOCCER, ["p2"])
    assert _reminder_matches(r, e)
