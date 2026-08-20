"""Deterministic usual detection + missing-usual gap logic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from level_core.calendar.usuals import (
    compute_usuals_from_events,
    missing_usuals_this_week,
    missing_usuals_today,
    rollup_for_role_agent,
)
from level_core.schemas import (
    ActivityType,
    CachedEvent,
    CarePerson,
    CareRelation,
    CareRoleId,
    Category,
    EventTime,
    HourBand,
    Usual,
    UsualStatus,
    Weekday,
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


def test_work_blocks_need_a_self_person() -> None:
    """Titles like 'Work' have no care-person name; without self they are dropped."""
    work = [
        CachedEvent(
            event_id=f"w{i}",
            calendar_id="primary",
            summary="Work",
            activity_type=ActivityType.WORK,
            time=EventTime(
                start=datetime(2026, 8, 3 + (i * 7), 16, 0, tzinfo=UTC),
                end=datetime(2026, 8, 3 + (i * 7), 21, 0, tzinfo=UTC),
                tz="UTC",
            ),
        )
        for i in range(3)
    ]
    kid = _person("p1", "Nova")
    assert compute_usuals_from_events(work, [kid]) == []

    self_p = CarePerson(
        person_id="p_self",
        display_name="You",
        relation=CareRelation.SELF,
        care_role_id=CareRoleId.SELF,
        is_self=True,
    )
    candidates = compute_usuals_from_events(work, [kid, self_p])
    assert len(candidates) == 1
    assert candidates[0].person_id == "p_self"
    assert candidates[0].activity_type == ActivityType.WORK
    assert candidates[0].display_summary == "Work"


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


def test_missing_this_week_warns_before_the_day() -> None:
    """A deleted Friday dropoff should warn on Thursday, not after Friday."""
    thursday = datetime(2026, 8, 20).date()
    usual = Usual(
        usual_id=Usual.compose_id("p_nova", Weekday.FRI, HourBand.EARLY_MORNING),
        person_id="p_nova",
        weekday=Weekday.FRI,
        hour_band=HourBand.EARLY_MORNING,
        activity_type=ActivityType.SCHOOL_DROPOFF,
        display_summary="Nova + Theo dropoff",
        status=UsualStatus.KEPT,
    )
    missing = missing_usuals_this_week(usuals=[usual], week_events=[], as_of_date=thursday)
    assert len(missing) == 1
    assert missing[0].weekday == Weekday.FRI
    assert missing[0].category == Category.DROPOFF

    friday_drop = _event(
        "p_nova",
        datetime(2026, 8, 21, 14, 45, tzinfo=UTC),
        ActivityType.SCHOOL_DROPOFF,
        "Nova + Theo dropoff",
    )
    covered = missing_usuals_this_week(
        usuals=[usual], week_events=[friday_drop], as_of_date=thursday
    )
    assert covered == []


def test_missing_this_week_skips_days_that_already_passed() -> None:
    """Monday's missing dropoff is already past on Thursday — don't warn."""
    thursday = datetime(2026, 8, 20).date()
    monday = Usual(
        usual_id=Usual.compose_id("p_nova", Weekday.MON, HourBand.EARLY_MORNING),
        person_id="p_nova",
        weekday=Weekday.MON,
        hour_band=HourBand.EARLY_MORNING,
        activity_type=ActivityType.SCHOOL_DROPOFF,
        display_summary="Nova + Theo dropoff",
        status=UsualStatus.KEPT,
    )
    friday = Usual(
        usual_id=Usual.compose_id("p_nova", Weekday.FRI, HourBand.EARLY_MORNING),
        person_id="p_nova",
        weekday=Weekday.FRI,
        hour_band=HourBand.EARLY_MORNING,
        activity_type=ActivityType.SCHOOL_DROPOFF,
        display_summary="Nova + Theo dropoff",
        status=UsualStatus.KEPT,
    )
    missing = missing_usuals_this_week(
        usuals=[monday, friday], week_events=[], as_of_date=thursday
    )
    assert [m.weekday for m in missing] == [Weekday.FRI]


def test_missing_this_week_ignores_events_outside_this_week() -> None:
    """Next week's matching dropoff must not cover (or warn about) this week."""
    thursday = datetime(2026, 8, 20).date()
    usual = Usual(
        usual_id=Usual.compose_id("p_nova", Weekday.FRI, HourBand.EARLY_MORNING),
        person_id="p_nova",
        weekday=Weekday.FRI,
        hour_band=HourBand.EARLY_MORNING,
        activity_type=ActivityType.SCHOOL_DROPOFF,
        display_summary="Nova + Theo dropoff",
        status=UsualStatus.KEPT,
    )
    next_friday = _event(
        "p_nova",
        datetime(2026, 8, 28, 14, 45, tzinfo=UTC),
        ActivityType.SCHOOL_DROPOFF,
        "Nova + Theo dropoff",
    )
    last_friday = _event(
        "p_nova",
        datetime(2026, 8, 14, 14, 45, tzinfo=UTC),
        ActivityType.SCHOOL_DROPOFF,
        "Nova + Theo dropoff",
    )
    missing = missing_usuals_this_week(
        usuals=[usual],
        week_events=[last_friday, next_friday],
        as_of_date=thursday,
    )
    assert len(missing) == 1
    assert missing[0].weekday == Weekday.FRI


def test_shared_dropoff_usual_fans_out_to_both_kids() -> None:
    nova = _person("p_nova", "Nova")
    theo = _person("p_theo", "Theo")
    events = [
        CachedEvent(
            event_id=f"drop_{day}",
            calendar_id="primary",
            summary="Nova + Theo dropoff",
            time=EventTime(
                start=datetime(2026, 8, day, 14, 45, tzinfo=UTC),
                end=datetime(2026, 8, day, 15, 15, tzinfo=UTC),
                tz="UTC",
            ),
            activity_type=ActivityType.SCHOOL_DROPOFF,
            matched_person_ids=["p_theo", "p_nova"],
        )
        for day in (7, 14)
    ]
    candidates = compute_usuals_from_events(events, [nova, theo])
    assert {c.person_id for c in candidates} == {"p_nova", "p_theo"}
    assert all(c.activity_type == ActivityType.SCHOOL_DROPOFF for c in candidates)


def test_missing_shared_dropoff_lists_both_kids() -> None:
    """A usual stored on Theo still warns for Nova when source events name both."""
    thursday = datetime(2026, 8, 20).date()
    source = _event(
        "p_theo",
        datetime(2026, 8, 14, 14, 45, tzinfo=UTC),
        ActivityType.SCHOOL_DROPOFF,
        "Nova + Theo dropoff",
    )
    source = source.model_copy(update={"matched_person_ids": ["p_theo", "p_nova"]})
    usual = Usual(
        usual_id=Usual.compose_id("p_theo", Weekday.FRI, HourBand.EARLY_MORNING),
        person_id="p_theo",
        weekday=Weekday.FRI,
        hour_band=HourBand.EARLY_MORNING,
        activity_type=ActivityType.SCHOOL_DROPOFF,
        display_summary="Nova + Theo dropoff",
        source_event_uids=[source.event_id],
        status=UsualStatus.KEPT,
    )
    missing = missing_usuals_this_week(
        usuals=[usual],
        week_events=[],
        as_of_date=thursday,
        events_by_id={source.event_id: source},
    )
    assert len(missing) == 1
    assert set(missing[0].person_ids) == {"p_theo", "p_nova"}

    nova_still_going = _event(
        "p_nova",
        datetime(2026, 8, 21, 14, 45, tzinfo=UTC),
        ActivityType.SCHOOL_DROPOFF,
        "Nova dropoff",
    )
    only_theo = missing_usuals_this_week(
        usuals=[usual],
        week_events=[nova_still_going],
        as_of_date=thursday,
        events_by_id={source.event_id: source},
    )
    assert [m.person_ids for m in only_theo] == [("p_theo",)]


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
