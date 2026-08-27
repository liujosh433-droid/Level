"""Priority-weighted slot ranking is deterministic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

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
    assert plan_label_from_message("lets book a lunch event next Wednesday 2-3pm") == "lunch event"
    from level_core.schedule.slots import calendar_title_from_label

    assert calendar_title_from_label("lunch with a friend") == "Lunch"
    assert calendar_title_from_label("pottery class") == "Pottery class"
    assert calendar_title_from_label("doctor's appointment") == "Doctor's appointment"


def test_meal_names_constrain_the_window() -> None:
    """Regression: "when's the best time to book lunch this week?" used
    to fall through to the default 8am-8pm window because the pattern
    table didn't know meal names, so the ranker returned dinner-time
    slots (6-7pm) alongside lunch times. Meal-specific windows
    guarantee the ranker only sees plausible hours per meal.
    """
    from level_core.schedule.slots import infer_event_kind

    lunch = infer_event_kind("when's the best time to book lunch this week?")
    assert lunch.windows == ((11, 14),), "lunch must be midday, not 8am-8pm"
    assert lunch.duration_minutes == 60
    assert 12.0 <= lunch.ideal_hour <= 13.0

    dinner = infer_event_kind("book team dinner this week")
    assert dinner.windows == ((17, 21),)
    assert dinner.duration_minutes == 90

    breakfast = infer_event_kind("find a time for breakfast tomorrow")
    assert breakfast.windows == ((7, 10),)

    coffee = infer_event_kind("when can I grab coffee this week")
    assert coffee.windows == ((8, 14),), "coffee spans morning-through-early-afternoon"
    assert coffee.duration_minutes == 30

    drinks = infer_event_kind("book drinks with the team this week")
    assert drinks.windows == ((16, 19),)

    happy_hour = infer_event_kind("find a happy hour slot this week")
    assert happy_hour.windows == ((16, 19),)


def test_meal_name_beats_time_of_day() -> None:
    """"lunch this afternoon" is still lunch (11-14), not the full
    12-17 afternoon window. Meal precedence over TOD keeps the
    "book X" flow narrow when the user is specific about the meal.
    """
    from level_core.schedule.slots import infer_event_kind

    kind = infer_event_kind("book lunch this afternoon")
    assert kind.windows == ((11, 14),), (
        f"meal must beat time-of-day; got windows={kind.windows}"
    )

    dinner = infer_event_kind("book dinner tonight")
    assert dinner.windows == ((17, 21),)


def test_lunch_window_respects_users_local_timezone() -> None:
    """The meal hours in _MEAL_PATTERNS are pure integers - the ranker
    must interpret them in the caller's tz, not UTC. This test
    exercises a non-default tz (New York) so a future refactor that
    accidentally hard-codes UTC or the app default breaks loudly.
    """
    from zoneinfo import ZoneInfo

    from level_core.schedule.slots import infer_event_kind, recommend_slots

    ny = ZoneInfo("America/New_York")
    tokyo = ZoneInfo("Asia/Tokyo")
    # Monday 8am NY -> 21:00 UTC -> 6am Tue Tokyo. Pick a time that
    # would map to a totally different hour in the other zones so a
    # tz bug can't accidentally still pass.
    now_ny = datetime(2026, 8, 24, 8, 0, tzinfo=ny)

    kind = infer_event_kind("when's the best time to book lunch this week?")

    for tz_name, tz in [("New York", ny), ("Tokyo", tokyo)]:
        picks = recommend_slots(
            events=[],
            kind=kind,
            starts_at=now_ny,
            window_days=5,
            priorities=[],
            usuals=[],
            tz=tz,
            limit=4,
        )
        assert picks, f"no slots in {tz_name}"
        for slot in picks:
            local = slot.start.astimezone(tz)
            assert 11 <= local.hour < 14, (
                f"in {tz_name}, lunch slot at local {local.time()} "
                f"is not in [11:00, 14:00) - tz bug or the meal window "
                f"leaked into UTC"
            )


def test_lunch_recommendations_never_include_dinner_hours() -> None:
    """End-to-end guard: recommend_slots on an empty calendar with a
    lunch kind must not return any slot after 2pm.
    """
    from zoneinfo import ZoneInfo

    from level_core.config import get_settings
    from level_core.schedule.slots import infer_event_kind, recommend_slots

    tz = ZoneInfo(get_settings().calendar_tz)
    now = datetime(2026, 8, 24, 8, 0, tzinfo=tz)  # Monday morning
    kind = infer_event_kind("when's the best time to book lunch this week?")

    picks = recommend_slots(
        events=[],
        kind=kind,
        starts_at=now,
        window_days=5,
        priorities=[],
        usuals=[],
        limit=4,
    )

    assert picks
    for slot in picks:
        local_start = slot.start.astimezone(tz)
        local_end = slot.end.astimezone(tz)
        assert 11 <= local_start.hour < 14, (
            f"lunch slot at {local_start.time()} is not in [11, 14)"
        )
        assert local_end.hour <= 14, (
            f"lunch slot ends at {local_end.time()}; must end by 14:00"
        )


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


# ---------------------------------------------------------------------------
# Async wrapper: regex fast-path vs LLM fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_wrapper_hits_regex_without_calling_llm(
    store, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    """Common meal / TOD words must never trigger the LLM path.
    Latency is the whole point of the fast path.
    """
    from level_core.agents import slot_window
    from level_core.schedule.slots import infer_event_kind_async

    called = {"count": 0}

    async def _boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        called["count"] += 1
        raise AssertionError("LLM was called for a regex-covered label")

    monkeypatch.setattr(slot_window, "run", _boom)

    kind = await infer_event_kind_async(store, "when's the best time to book lunch this week?")
    assert kind.windows == ((11, 14),)
    assert called["count"] == 0

    kind = await infer_event_kind_async(store, "find a time in the evening")
    assert kind.windows == ((17, 21),)
    assert called["count"] == 0


@pytest.mark.asyncio
async def test_async_wrapper_escalates_to_llm_for_uncommon_labels(
    store, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    """"afternoon tea" isn't in the regex table, so the wrapper must
    ask SlotWindowAgent and use its window instead of the 8am-8pm
    default that was surfacing dinner-time slots for lunch queries.
    """
    from level_core.agents import slot_window
    from level_core.agents.base import AgentResult
    from level_core.agents.slot_window import ProposedSlotWindow, SlotWindowAgentResponse
    from level_core.schedule.slots import infer_event_kind_async

    captured: dict[str, object] = {}

    async def _fake_run(*, store, message, label):  # type: ignore[no-untyped-def]
        captured["label"] = label
        captured["message"] = message
        proposed = ProposedSlotWindow(
            start_hour=14,
            end_hour=17,
            ideal_hour=15.5,
            duration_minutes=45,
            label=label,
        )
        return AgentResult(
            value=SlotWindowAgentResponse(window=proposed),
            blocked_by_safety=False,
            audit_id="a1",
            latency_ms=42,
            turns_taken=1,
        )

    monkeypatch.setattr(slot_window, "run", _fake_run)

    # "playdate" isn't a meal or time-of-day word, so the regex path
    # returns None and the wrapper must escalate to SlotWindowAgent.
    kind = await infer_event_kind_async(store, "find a time for a playdate this week")
    assert captured["label"] == "playdate"
    assert kind.windows == ((14, 17),)
    assert kind.duration_minutes == 45


@pytest.mark.asyncio
async def test_async_wrapper_soft_degrades_when_llm_returns_null(
    store, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    """If SlotWindowAgent can't infer a window (returns null, is
    rate-limited, has no credentials), the caller MUST fall back
    to the deterministic default rather than 500.
    """
    from level_core.agents import slot_window
    from level_core.agents.base import AgentResult
    from level_core.agents.slot_window import SlotWindowAgentResponse
    from level_core.schedule.slots import infer_event_kind_async

    async def _empty(*, store, message, label):  # type: ignore[no-untyped-def]
        return AgentResult(
            value=SlotWindowAgentResponse(window=None),
            blocked_by_safety=False,
            audit_id="a2",
            latency_ms=0,
            turns_taken=1,
            soft_degraded=True,
        )

    monkeypatch.setattr(slot_window, "run", _empty)

    kind = await infer_event_kind_async(store, "find a time for the thing this week")
    # Falls back to default waking hours - never crashes.
    assert kind.windows == ((8, 20),)
