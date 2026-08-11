"""Conflict + free-slot math for the calendar commitment gate."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from level_core.calendar.availability import (
    draft_window,
    find_conflicts,
    find_free_slots,
)
from level_core.calendar.commitment_gate import (
    _heuristic_title,
    _sanitize_message,
    looks_like_schedule_ask,
)
from level_core.schemas.commitment import EventDraft, Weekday


def test_looks_like_schedule_ask_detects_add_and_availability() -> None:
    assert looks_like_schedule_ask(
        "add swimming night every Tues/Thurs at 9:30pm to my calendar"
    )
    assert looks_like_schedule_ask(
        "Diane wants to get dinner at 6:30pm today.. do I have time?"
    )
    assert looks_like_schedule_ask("when else am I free tonight?")
    assert looks_like_schedule_ask(
        "Need to fit in grandparents visit on weekend, when is the best time"
    )
    assert not looks_like_schedule_ask("Should I switch schools for Jordan?")


def test_heuristic_title_and_sanitize() -> None:
    assert _heuristic_title(
        "Diane wants to get dinner at 6:30pm today.. do I have time?"
    ) == "Dinner with Diane"
    assert "[bullet:" not in _sanitize_message(
        "Follow your email [bullet:df5277d55d1d4a738a80321d3a] tonight."
    )


def test_model_weekend_by_days_drive_windows_not_today() -> None:
    """AI output with by_days=SA/SU must search weekend — never pin to Tuesday."""
    from level_core.calendar.commitment_gate import (
        _ParsedIntent,
        _draft_from_parsed,
        _normalize_parsed,
    )
    from level_core.calendar.availability import draft_window, occurrence_windows

    # Shape the model is instructed to return for a weekend availability ask.
    parsed = _normalize_parsed(
        _ParsedIntent(
            is_schedule_ask=True,
            kind="availability",
            title="Grandparents visit",
            local_date="2026-08-11",  # mistaken weekday pin
            local_time="14:00",
            duration_minutes=120,
            by_days=["SA", "SU"],
        ),
        today="2026-08-11",
        source_text="Need to fit in grandparents visit on weekend, when is the best time",
    )
    assert parsed.local_date is None  # dropped — conflicts with SA/SU
    assert parsed.by_days == ["SA", "SU"]
    draft = _draft_from_parsed(parsed)
    now = datetime(2026, 8, 11, 18, 0, tzinfo=timezone.utc)  # Tuesday
    start, _ = draft_window(draft, now=now)
    local = start.astimezone(ZoneInfo("America/Los_Angeles"))
    assert local.weekday() >= 5
    windows = occurrence_windows(draft, now=now, weeks=1)
    assert len(windows) >= 2
    assert all(
        w[0].astimezone(ZoneInfo("America/Los_Angeles")).weekday() >= 5 for w in windows
    )


def test_normalize_does_not_force_availability_onto_today() -> None:
    from level_core.calendar.commitment_gate import _ParsedIntent, _normalize_parsed

    parsed = _normalize_parsed(
        _ParsedIntent(
            is_schedule_ask=True,
            kind="availability",
            title="Grandparents visit",
            local_date=None,
            local_time="14:00",
            by_days=["SA", "SU"],
        ),
        today="2026-08-11",
        source_text="weekend grandparents visit",
    )
    assert parsed.local_date is None
    assert parsed.by_days == ["SA", "SU"]


def test_all_day_blocks_evening_in_local_tz() -> None:
    events = [
        {
            "summary": "Co-parent weekend (Jordan with Alex)",
            "start": {"date": "2026-08-09"},
            "end": {"date": "2026-08-10"},
        },
        {
            "summary": "Catch up on work email",
            "start": {"dateTime": "2026-08-09T11:00:00-07:00"},
            "end": {"dateTime": "2026-08-09T12:00:00-07:00"},
        },
    ]
    tz = "America/Los_Angeles"
    dinner_start = datetime(2026, 8, 9, 19, 0, tzinfo=ZoneInfo(tz)).astimezone(
        timezone.utc
    )
    dinner_end = dinner_start + timedelta(minutes=90)
    conflicts = find_conflicts(
        events,
        window_start=dinner_start,
        window_end=dinner_end,
        timezone_name=tz,
    )
    assert any("Co-parent" in c.summary for c in conflicts)
    assert not any("email" in c.summary.lower() for c in conflicts)


def test_find_conflicts_and_free_slots() -> None:
    tz = timezone.utc
    day = datetime(2026, 8, 9, 0, 0, tzinfo=tz)
    events = [
        {
            "summary": "Soccer practice — Jordan",
            "start": {"dateTime": "2026-08-09T17:00:00+00:00"},
            "end": {"dateTime": "2026-08-09T18:00:00+00:00"},
        },
        {
            "summary": "Pottery",
            "start": {"dateTime": "2026-08-09T01:00:00+00:00"},
            "end": {"dateTime": "2026-08-09T02:30:00+00:00"},
        },
    ]
    # 17:30–19:00 UTC overlaps soccer 17:00–18:00
    dinner_start = datetime(2026, 8, 9, 17, 30, tzinfo=tz)
    dinner_end = dinner_start + timedelta(minutes=90)
    conflicts = find_conflicts(
        events, window_start=dinner_start, window_end=dinner_end, timezone_name="UTC"
    )
    assert any("Soccer" in c.summary for c in conflicts)

    slots = find_free_slots(
        events,
        day_start=day.replace(hour=8),
        day_end=day.replace(hour=22),
        duration=timedelta(minutes=90),
        timezone_name="UTC",
        max_slots=3,
    )
    assert slots
    # First free 90m block should finish before soccer or start after it.
    first_end = datetime.fromisoformat(slots[0].end)
    assert first_end <= datetime(2026, 8, 9, 17, 0, tzinfo=tz) or datetime.fromisoformat(
        slots[0].start
    ) >= datetime(2026, 8, 9, 18, 0, tzinfo=tz)


def test_draft_window_recurring_next_weekday() -> None:
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)  # Sunday
    draft = EventDraft(
        title="Swimming",
        by_days=[Weekday.TU, Weekday.TH],
        local_time="21:30",
        duration_minutes=60,
        timezone="UTC",
        recurring=True,
    )
    start, end = draft_window(draft, now=now)
    assert start.weekday() == 1  # Tuesday
    assert start.hour == 21 and start.minute == 30
    assert end - start == timedelta(minutes=60)
