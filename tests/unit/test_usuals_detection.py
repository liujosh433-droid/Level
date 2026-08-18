"""Deterministic usual detection + missing-usual gap logic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from level_core.calendar.usuals import (
    compute_usuals_from_events,
    missing_usuals_today,
    rollup_for_role_agent,
)
from level_core.schemas import (
    ActivityType,
    CachedEvent,
    CarePerson,
    CareRelation,
    CareRoleId,
    EventTime,
    HourBand,
    Usual,
    UsualStatus,
    Weekday,
)


def _event(person_id: str, when: datetime, activity: ActivityType, summary: str) -> CachedEvent:
    return CachedEvent(
        event_id=f"e_{summary}_{when.isoformat()}",
        calendar_id="primary",
        summary=summary,
        time=EventTime(start=when, end=when + timedelta(hours=1), tz="UTC"),
        activity_type=activity,
        matched_person_ids=[person_id],
    )


def _person(pid: str, name: str) -> CarePerson:
    return CarePerson(
        person_id=pid,
        display_name=name,
        relation=CareRelation.CHILD,
        care_role_id=CareRoleId.KIDS,
    )


def test_two_weeks_of_pickups_becomes_a_candidate() -> None:
    person = _person("p1", "Alpha")
    events = [
        _event(person.person_id, datetime(2026, 8, 3, 15, 0, tzinfo=UTC), ActivityType.SCHOOL_PICKUP, "Alpha pickup"),
        _event(person.person_id, datetime(2026, 8, 10, 15, 0, tzinfo=UTC), ActivityType.SCHOOL_PICKUP, "Alpha pickup"),
        _event(person.person_id, datetime(2026, 8, 17, 15, 0, tzinfo=UTC), ActivityType.SCHOOL_PICKUP, "Alpha pickup"),
    ]
    candidates = compute_usuals_from_events(events, [person])
    assert len(candidates) == 1
    c = candidates[0]
    assert c.person_id == "p1"
    assert c.activity_type == ActivityType.SCHOOL_PICKUP
    assert c.confidence > 0


def test_isolated_event_is_not_a_candidate() -> None:
    person = _person("p1", "Alpha")
    events = [
        _event(person.person_id, datetime(2026, 8, 3, 15, 0, tzinfo=UTC), ActivityType.SCHOOL_PICKUP, "Alpha pickup"),
    ]
    assert compute_usuals_from_events(events, [person]) == []


def test_missing_usual_when_no_matching_event_today() -> None:
    _ = _person("p1", "Alpha")
    today_wd = Weekday(datetime.now(UTC).weekday())
    usual = Usual(
        usual_id=Usual.compose_id("p1", today_wd, HourBand.AFTERNOON),
        person_id="p1",
        weekday=today_wd,
        hour_band=HourBand.AFTERNOON,
        activity_type=ActivityType.SCHOOL_PICKUP,
        display_summary="Alpha pickup",
        status=UsualStatus.KEPT,
    )
    missing = missing_usuals_today(usuals=[usual], todays_events=[])
    assert len(missing) == 1
    assert missing[0].usual.usual_id == usual.usual_id


def test_rollup_compresses_events() -> None:
    person = _person("p1", "Alpha")
    events = [
        _event(person.person_id, datetime(2026, 8, i, 15, 0, tzinfo=UTC), ActivityType.SCHOOL_PICKUP, "Alpha school pickup notes")
        for i in (3, 10, 17)
    ]
    rollup = rollup_for_role_agent(events)
    assert len(rollup) == 1
    row = rollup[0]
    assert row["count"] == 3
    assert row["summary_first_5_words"] == "Alpha school pickup notes"
    assert row["hour_band"] == HourBand.AFTERNOON.value
