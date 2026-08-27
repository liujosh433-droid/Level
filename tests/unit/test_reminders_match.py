"""Reminder matching is structured equality (person_id, activity_type)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from level_core.calendar.enrich import _reminder_matches, rematch_reminders
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


def test_other_reminder_does_not_stick_to_leftover_events() -> None:
    r = Reminder(
        reminder_id="r1",
        text="Give my kids permission slips",
        match=ReminderMatch(person_id=None, activity_type=ActivityType.OTHER),
    )
    assert not _reminder_matches(r, _event(ActivityType.OTHER, []))
    assert not _reminder_matches(r, _event(ActivityType.PERSONAL, []))


def test_dropoff_reminder_skips_lunch() -> None:
    r = Reminder(
        reminder_id="r1",
        text="Give my kids permission slips",
        match=ReminderMatch(person_id=None, activity_type=ActivityType.SCHOOL_DROPOFF),
    )
    assert not _reminder_matches(r, _event(ActivityType.PERSONAL, []))
    assert not _reminder_matches(r, _event(ActivityType.OTHER, []))
    assert _reminder_matches(r, _event(ActivityType.SCHOOL_DROPOFF, ["p_nova"]))


@pytest.mark.asyncio
async def test_rematch_reminders_attaches_new_reminder_synchronously(store) -> None:  # type: ignore[no-untyped-def]
    """Regression: adding a reminder used to fire ``_background_enrich``
    only, and the frontend's post-reply refetch would land BEFORE the
    background task attached the reminder to events. Users had to
    refresh two or three times to see the tag appear.

    The fix inlines a fast reminder-only sweep in the chat handler.
    This test locks that in at the helper level: after adding a
    reminder and calling ``rematch_reminders``, every eligible event
    already carries ``matched_reminder_ids`` populated - no
    background wait, no polling.
    """
    from level_core.storage.care_store import add_reminder

    # Two events already classified as SPORTS_SOCCER for the same
    # person, plus a distractor with a different activity_type.
    now = datetime.now(UTC)
    soccer_a = CachedEvent(
        event_id="e_soccer_a",
        calendar_id="primary",
        summary="Nova soccer",
        time=EventTime(start=now, end=now + timedelta(hours=1), tz="UTC"),
        activity_type=ActivityType.SPORTS_SOCCER,
        matched_person_ids=["p_nova"],
    )
    soccer_b = CachedEvent(
        event_id="e_soccer_b",
        calendar_id="primary",
        summary="Nova soccer practice",
        time=EventTime(
            start=now + timedelta(days=7),
            end=now + timedelta(days=7, hours=1),
            tz="UTC",
        ),
        activity_type=ActivityType.SPORTS_SOCCER,
        matched_person_ids=["p_nova"],
    )
    other = CachedEvent(
        event_id="e_work",
        calendar_id="primary",
        summary="1:1 with mentor",
        time=EventTime(
            start=now + timedelta(hours=2),
            end=now + timedelta(hours=3),
            tz="UTC",
        ),
        activity_type=ActivityType.WORK,
        matched_person_ids=[],
    )
    await store.agenda.upsert_many([soccer_a, soccer_b, other])

    reminder = await add_reminder(
        store,
        text="Bring soccer shoes",
        person_id="p_nova",
        activity_type=ActivityType.SPORTS_SOCCER,
    )

    updates = await rematch_reminders(store)
    assert updates == 2, "both soccer events should be updated"

    a = await store.agenda.get("e_soccer_a")
    b = await store.agenda.get("e_soccer_b")
    w = await store.agenda.get("e_work")
    assert a is not None and reminder.reminder_id in a.matched_reminder_ids
    assert b is not None and reminder.reminder_id in b.matched_reminder_ids
    # Distractor stays clean.
    assert w is not None and reminder.reminder_id not in w.matched_reminder_ids


@pytest.mark.asyncio
async def test_rematch_reminders_is_idempotent(store) -> None:  # type: ignore[no-untyped-def]
    """Second call with no changes returns 0 updates.

    Guards against a rewrite loop where every call rewrites every
    event even when ``matched_reminder_ids`` is already correct. The
    diff-only agenda upsert is what makes this operation cheap enough
    to run inline on every chat turn.
    """
    from level_core.storage.care_store import add_reminder

    now = datetime.now(UTC)
    await store.agenda.upsert(
        CachedEvent(
            event_id="e_soccer",
            calendar_id="primary",
            summary="Nova soccer",
            time=EventTime(start=now, end=now + timedelta(hours=1), tz="UTC"),
            activity_type=ActivityType.SPORTS_SOCCER,
            matched_person_ids=["p_nova"],
        )
    )
    await add_reminder(
        store,
        text="Bring shinguards",
        person_id="p_nova",
        activity_type=ActivityType.SPORTS_SOCCER,
    )

    first = await rematch_reminders(store)
    second = await rematch_reminders(store)
    assert first == 1
    assert second == 0


@pytest.mark.asyncio
async def test_rematch_reminders_detaches_when_reminder_deactivated(store) -> None:  # type: ignore[no-untyped-def]
    """If a reminder flips to ``dismissed`` (soft-delete via care_store),
    the next rematch sweep clears it from every event.

    Belt-and-suspenders with ``delete_reminder`` which detaches inline
    - if some future code path flips ``status`` without going through
    ``delete_reminder``, this sweep still eventually cleans up.
    """
    from level_core.schemas.reminder import ReminderStatus
    from level_core.storage.care_store import add_reminder

    now = datetime.now(UTC)
    reminder = await add_reminder(
        store,
        text="Bring cleats",
        person_id="p_nova",
        activity_type=ActivityType.SPORTS_SOCCER,
    )
    event = CachedEvent(
        event_id="e_soccer",
        calendar_id="primary",
        summary="Nova soccer",
        time=EventTime(start=now, end=now + timedelta(hours=1), tz="UTC"),
        activity_type=ActivityType.SPORTS_SOCCER,
        matched_person_ids=["p_nova"],
        matched_reminder_ids=[reminder.reminder_id],
    )
    await store.agenda.upsert(event)

    # Flip reminder to dismissed without going through delete_reminder.
    await store.reminders.upsert(
        reminder.model_copy(update={"status": ReminderStatus.DISMISSED})
    )

    updates = await rematch_reminders(store)
    assert updates == 1
    e = await store.agenda.get("e_soccer")
    assert e is not None and reminder.reminder_id not in e.matched_reminder_ids
