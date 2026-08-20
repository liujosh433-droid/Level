from __future__ import annotations

import pytest
from level_core.schemas import ActivityType
from level_core.storage.care_store import parse_reminder, parse_reminder_followup


def test_bring_charger_to_meetings() -> None:
    parsed = parse_reminder("remind me to bring a charger to my meetings")
    assert parsed is not None
    assert parsed.text == "Bring a charger"
    assert parsed.activity_type == ActivityType.WORK


def test_dont_forget_soccer_shoes() -> None:
    parsed = parse_reminder("don't forget Theo's soccer shoes")
    assert parsed is not None
    assert parsed.person_display_name == "Theo"
    assert parsed.text == "Soccer shoes"
    assert parsed.activity_type == ActivityType.SPORTS_SOCCER


def test_schedule_question_is_not_a_reminder() -> None:
    assert parse_reminder("remind me what I have tomorrow") is None
    assert parse_reminder("what's crowding this week?") is None


def test_permission_slips_at_dropoff() -> None:
    parsed = parse_reminder(
        "remind me to give my kids permission slips when i drop them off"
    )
    assert parsed is not None
    assert parsed.text == "Give my kids permission slips"
    assert parsed.activity_type == ActivityType.SCHOOL_DROPOFF
    assert parsed.person_display_name is None


def test_followup_charger_keeps_meeting_context() -> None:
    parsed = parse_reminder_followup(
        "a charger",
        [
            {"role": "user", "text": "remind me to bring a charger to my meetings"},
            {
                "role": "assistant",
                "text": "Tell me the thing you might forget and I'll surface it.",
            },
        ],
    )
    assert parsed is not None
    assert parsed.text == "A charger"
    assert parsed.activity_type == ActivityType.WORK


def test_followup_ignored_without_prompt() -> None:
    assert parse_reminder_followup("a charger", [{"role": "user", "text": "hi"}]) is None


@pytest.mark.asyncio
async def test_fast_path_saves_charger_for_meetings(store) -> None:  # type: ignore[no-untyped-def]
    from level_api.routes.chat import _try_fast_reminder

    result = await _try_fast_reminder(
        store, "remind me to bring a charger to my meetings", []
    )
    assert result is not None
    assert result["path"] == "reminder"
    assert "Bring a charger" in result["reply"]
    assert "work" in result["reply"].lower()
    saved = await store.reminders.list()
    assert len(saved) == 1
    assert saved[0].text == "Bring a charger"
    assert saved[0].match.activity_type == ActivityType.WORK


@pytest.mark.asyncio
async def test_fast_path_followup_after_prompt(store) -> None:  # type: ignore[no-untyped-def]
    from level_api.routes.chat import _try_fast_reminder

    result = await _try_fast_reminder(
        store,
        "a charger",
        [
            {"role": "user", "text": "remind me to bring a charger to my meetings"},
            {
                "role": "assistant",
                "text": "Tell me the thing you might forget and I'll surface it.",
            },
        ],
    )
    assert result is not None
    assert "A charger" in result["reply"]
    saved = await store.reminders.list()
    assert saved[0].match.activity_type == ActivityType.WORK


@pytest.mark.asyncio
async def test_fast_path_permission_slips_are_dropoff(store) -> None:  # type: ignore[no-untyped-def]
    from level_api.routes.chat import _try_fast_reminder

    result = await _try_fast_reminder(
        store,
        "remind me to give my kids permission slips when i drop them off",
        [],
    )
    assert result is not None
    assert "Give my kids permission slips" in result["reply"]
    assert "dropoff" in result["reply"].lower()
    saved = await store.reminders.list()
    assert saved[0].match.activity_type == ActivityType.SCHOOL_DROPOFF
