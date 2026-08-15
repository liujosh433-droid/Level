"""Conflict + free-slot math for the calendar commitment gate."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from level_core.calendar.availability import (
    draft_window,
    find_conflicts,
    find_free_slots,
    occurrence_windows,
)
from level_core.calendar.commitment_gate import (
    _ParsedIntent,
    _draft_from_parsed,
    _ground_availability_reply,
    _normalize_parsed,
    _offline_title,
    _sanitize_message,
    _summarize_draft,
    looks_like_schedule_ask,
)
from level_core.schemas.care import (
    CARE_ROLE_LABELS,
    CareProfile,
    CareRoleId,
    CareRoleState,
    ProtectedWindow,
)
from level_core.schemas.commitment import CommitmentKind, EventDraft, FreeSlot, Weekday
from level_core.schemas.profile import BulletStatus


def test_offline_schedule_hint_is_coarse_fallback_only() -> None:
    """Regex must not be the live classifier — only a model-down safety net."""
    assert looks_like_schedule_ask(
        "add swimming night every Tues/Thurs at 9:30pm to my calendar"
    )
    assert looks_like_schedule_ask(
        "Diane wants to get dinner at 6:30pm today.. do I have time?"
    )
    assert looks_like_schedule_ask("what time would work best for a return today")
    assert not looks_like_schedule_ask("Should I switch schools for Jordan?")


def test_offline_title_and_sanitize() -> None:
    assert _offline_title("short ask") == "short ask"
    assert "[bullet:" not in _sanitize_message(
        "Follow your email [bullet:df5277d55d1d4a738a80321d3a] tonight."
    )


def test_model_weekend_by_days_drive_windows_not_today() -> None:
    """AI output with by_days=SA/SU must search weekend — never pin to Tuesday."""
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


def test_availability_copy_does_not_echo_the_question() -> None:
    ask = (
        "I need a book an event 2 weeks from now which day 2 weeks from now "
        "or more is the soonest I can put in a 1 hour event"
    )
    parsed = _normalize_parsed(
        _ParsedIntent(
            is_schedule_ask=True,
            kind="availability",
            title=ask,
            local_date="2026-08-26",
            local_time="09:00",
            duration_minutes=60,
        ),
        today="2026-08-12",
        source_text=ask,
    )
    assert parsed.title == "1-hour hold"
    assert "I need" not in parsed.title

    summary = _summarize_draft(
        CommitmentKind.AVAILABILITY,
        EventDraft(
            title=parsed.title,
            local_date="2026-08-26",
            local_time="09:00",
            duration_minutes=60,
        ),
    )
    assert summary == "Soonest 1-hour opening from Aug 26"
    assert "I need" not in summary
    assert "2026-08-26" not in summary

    msg = _ground_availability_reply(
        title=ask,
        free_slots=[
            FreeSlot(
                start="2026-08-26T15:00:00+00:00",
                end="2026-08-26T16:00:00+00:00",
                label="Wed Aug 26, 8:00–9:00am",
            ),
            FreeSlot(
                start="2026-08-26T15:30:00+00:00",
                end="2026-08-26T16:30:00+00:00",
                label="Wed Aug 26, 8:30–9:30am",
            ),
        ],
        conflicts=[],
    )
    assert msg.startswith("Soonest opening: Wed Aug 26, 8:00–9:00am.")
    assert "I need" not in msg
    assert "Best opening for" not in msg


def _pickup_care() -> CareProfile:
    return CareProfile(
        user_id="u1",
        roles=[
            CareRoleState(
                role_id=CareRoleId.CHILD_CARE,
                label=CARE_ROLE_LABELS[CareRoleId.CHILD_CARE],
                salience=0.9,
                status=BulletStatus.ACCEPTED,
                people=["Nova", "Theo"],
                protected_windows=[
                    ProtectedWindow(
                        label="Pickup",
                        weekday=2,  # Wednesday
                        start_hour=15,
                        end_hour=16,
                    )
                ],
            )
        ],
    )


def test_availability_copy_names_clear_care_window() -> None:
    msg = _ground_availability_reply(
        title="1-hour hold",
        free_slots=[
            FreeSlot(
                start="2026-08-26T20:00:00+00:00",  # 1:00pm PT
                end="2026-08-26T21:00:00+00:00",
                label="Wed Aug 26, 1:00–2:00pm",
            ),
            FreeSlot(
                start="2026-08-26T21:00:00+00:00",
                end="2026-08-26T22:00:00+00:00",
                label="Wed Aug 26, 2:00–3:00pm",
            ),
        ],
        conflicts=[],
        care=_pickup_care(),
        timezone_name="America/Los_Angeles",
    )
    assert msg.startswith("Soonest opening: Wed Aug 26, 1:00–2:00pm.")
    assert "Clear of Pickup" in msg
    assert "Nova" in msg and "Theo" in msg
    assert "1-hour hold" not in msg


def test_availability_copy_flags_crowded_care_window() -> None:
    msg = _ground_availability_reply(
        title="1-hour hold",
        free_slots=[
            FreeSlot(
                start="2026-08-26T22:00:00+00:00",  # 3:00pm PT — pickup
                end="2026-08-26T23:00:00+00:00",
                label="Wed Aug 26, 3:00–4:00pm",
            ),
            FreeSlot(
                start="2026-08-26T20:00:00+00:00",  # 1:00pm PT — clear
                end="2026-08-26T21:00:00+00:00",
                label="Wed Aug 26, 1:00–2:00pm",
            ),
        ],
        conflicts=[],
        care=_pickup_care(),
        timezone_name="America/Los_Angeles",
    )
    assert "Soonest opening: Wed Aug 26, 3:00–4:00pm" in msg
    assert "sits on Pickup" in msg
    assert "Nova" in msg and "Theo" in msg
    assert "Wed Aug 26, 1:00–2:00pm is clear of that window" in msg
    assert "1-hour hold" not in msg


def test_unnamed_duration_stays_one_hour() -> None:
    parsed = _normalize_parsed(
        _ParsedIntent(
            is_schedule_ask=True,
            kind="availability",
            title="Hold",
            local_date="2026-08-26",
            local_time="13:00",
            duration_minutes=180,
            duration_named=False,
        ),
        today="2026-08-12",
        source_text="when do I have an opening in the afternoon 2 weeks from now",
    )
    assert parsed.duration_minutes == 60
    named = _normalize_parsed(
        _ParsedIntent(
            is_schedule_ask=True,
            kind="availability",
            title="Hold",
            duration_minutes=180,
            duration_named=True,
        ),
        today="2026-08-12",
        source_text="book a 3 hour block",
    )
    assert named.duration_minutes == 180


def test_free_slot_labels_include_weekday_and_date() -> None:
    tz = "America/Los_Angeles"
    day = datetime(2026, 8, 26, 15, 0, tzinfo=timezone.utc)  # 8am PT
    slots = find_free_slots(
        [],
        day_start=day,
        day_end=day + timedelta(hours=3),
        duration=timedelta(hours=1),
        timezone_name=tz,
        max_slots=1,
    )
    assert slots
    assert "Aug 26" in slots[0].label
    assert "Wed" in slots[0].label
    assert "–" in slots[0].label
