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


def test_work_today_covers_the_self_work_usual() -> None:
    """Regression: 'Josh work 9am-4:45pm' on today should cover the Josh
    work usual, not flag it as missing.

    The bug: compute_usuals_from_events uses resolve_person_ids (which
    falls back to is_self when no named person matches), so a bare
    'Work' event clusters into a self-owned usual. But
    missing_usuals_today used to read e.matched_person_ids RAW - and
    the demo ICS loader / LLM enricher both deliberately skip self on
    the match pass (otherwise every event would tag as Me). Result:
    Josh's own work usual was flagged as missing every day even though
    Work was right there on today's calendar. Passing `people` uses
    the same resolver at check time, restoring symmetry.
    """
    self_p = CarePerson(
        person_id="p_self",
        display_name="Josh",
        relation=CareRelation.SELF,
        care_role_id=CareRoleId.SELF,
        is_self=True,
    )
    today = datetime.now(UTC)
    today_wd = Weekday(today.weekday())

    usual = Usual(
        usual_id=Usual.compose_id("p_self", today_wd, HourBand.MORNING),
        person_id="p_self",
        weekday=today_wd,
        hour_band=HourBand.MORNING,
        activity_type=ActivityType.WORK,
        display_summary="Work",
        # Weekly pattern — clears the _is_regular_weekly gate that
        # keeps sub-weekly (biweekly, monthly) usuals out of the
        # missing lists.
        confidence=1.0,
        status=UsualStatus.KEPT,
    )
    todays_work = CachedEvent(
        event_id="e_work_today",
        calendar_id="primary",
        summary="Work",
        activity_type=ActivityType.WORK,
        time=EventTime(
            start=today.replace(hour=9, minute=0, second=0, microsecond=0),
            end=today.replace(hour=16, minute=45, second=0, microsecond=0),
            tz="UTC",
        ),
        # Empty on purpose: the ICS loader / enricher never stamp self
        # on a title without a name.
        matched_person_ids=[],
    )

    # Without `people`: the old broken behaviour. Kept to lock in the
    # backward-compat fallback that older callers still exercise.
    stale = missing_usuals_today(usuals=[usual], todays_events=[todays_work])
    assert len(stale) == 1

    # With `people`: the fixed behaviour. Work today covers the usual.
    fixed = missing_usuals_today(
        usuals=[usual], todays_events=[todays_work], people=[self_p]
    )
    assert fixed == []


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
        confidence=1.0,
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
        confidence=1.0,
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


def test_biweekly_usual_does_not_flag_missing_on_off_week() -> None:
    """A Saturday-morning house-reset that only fires every other Sat
    should NOT show up as "missing" on its scheduled off-week.

    Regression: solo-house-reset in the demo ICS is interval=2. From
    2 past occurrences over a 4-week observation window it forms a
    (self, SAT, MORNING, PERSONAL) usual with confidence ~0.5, which
    is indistinguishable from a weekly usual with heavy skipping.
    On the biweekly's off-week the frontend was showing "Josh
    Saturday 9-10am missing" - noise, not signal. The
    _is_regular_weekly gate keeps sub-weekly patterns out of the
    missing lists while leaving the usual itself in storage (still
    visible on the profile page).
    """
    thursday = datetime(2026, 8, 20).date()
    biweekly = Usual(
        usual_id=Usual.compose_id("p_self", Weekday.SAT, HourBand.MORNING),
        person_id="p_self",
        weekday=Weekday.SAT,
        hour_band=HourBand.MORNING,
        activity_type=ActivityType.PERSONAL,
        display_summary="House reset / laundry",
        # 2 past occurrences over 4 observed weeks. UsualCandidate
        # would set 0.5 here; the persisted usual carries the same.
        confidence=0.5,
        status=UsualStatus.KEPT,
    )
    weekly = Usual(
        usual_id=Usual.compose_id("p_nova", Weekday.SAT, HourBand.MORNING),
        person_id="p_nova",
        weekday=Weekday.SAT,
        hour_band=HourBand.MORNING,
        activity_type=ActivityType.SPORTS_OTHER,
        display_summary="Nova ballet",
        confidence=0.75,  # 3/4 past occurrences - one skip
        status=UsualStatus.KEPT,
    )
    missing = missing_usuals_this_week(
        usuals=[biweekly, weekly], week_events=[], as_of_date=thursday
    )
    # Only the weekly usual survives. The biweekly is silenced on
    # its off-week regardless of coverage.
    assert [m.category for m in missing] == [Category.SPORTS]


def test_missing_this_week_carries_concrete_title_hint() -> None:
    """The nudge label ("Grocery run") should come from the source
    usuals, not the coarse category ("Personal").

    Rationale: Category.PERSONAL is too abstract on its own — "Josh's
    personal is missing this week" reads like a therapist joke. The
    title_hint is the majority-vote display_summary from the source
    usuals; the UI falls back to category_label when it's absent.
    """
    thursday = datetime(2026, 8, 20).date()
    grocery = Usual(
        usual_id=Usual.compose_id("p_self", Weekday.FRI, HourBand.AFTERNOON),
        person_id="p_self",
        weekday=Weekday.FRI,
        hour_band=HourBand.AFTERNOON,
        activity_type=ActivityType.PERSONAL,
        display_summary="Grocery run",
        confidence=1.0,
        status=UsualStatus.KEPT,
    )
    missing = missing_usuals_this_week(usuals=[grocery], week_events=[], as_of_date=thursday)
    assert len(missing) == 1
    assert missing[0].category == Category.PERSONAL
    assert missing[0].title_hint == "Grocery run"


def test_title_hint_is_dropped_when_it_equals_the_category() -> None:
    """"Work" == Category.WORK.label — the hint would be redundant."""
    thursday = datetime(2026, 8, 20).date()
    work = Usual(
        usual_id=Usual.compose_id("p_self", Weekday.FRI, HourBand.MORNING),
        person_id="p_self",
        weekday=Weekday.FRI,
        hour_band=HourBand.MORNING,
        activity_type=ActivityType.WORK,
        display_summary="Work",
        confidence=1.0,
        status=UsualStatus.KEPT,
    )
    missing = missing_usuals_this_week(usuals=[work], week_events=[], as_of_date=thursday)
    assert missing[0].title_hint is None


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
        confidence=1.0,
        status=UsualStatus.KEPT,
    )
    friday = Usual(
        usual_id=Usual.compose_id("p_nova", Weekday.FRI, HourBand.EARLY_MORNING),
        person_id="p_nova",
        weekday=Weekday.FRI,
        hour_band=HourBand.EARLY_MORNING,
        activity_type=ActivityType.SCHOOL_DROPOFF,
        display_summary="Nova + Theo dropoff",
        confidence=1.0,
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
        confidence=1.0,
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
        confidence=1.0,
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
