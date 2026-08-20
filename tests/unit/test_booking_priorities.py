"""Booking confirm should title from the user's words and flag care priorities."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from level_core.schemas import (
    ActivityType,
    CachedEvent,
    CarePerson,
    CareRelation,
    CareRoleId,
    EventTime,
)
from level_core.storage.care_store import add_priority, propose_person, set_person_status


def test_lunch_booking_is_titled_lunch_not_pickup() -> None:
    from level_api.routes.chat import _title_from_message, _title_from_plan_label

    msg = "lets book a lunch event next Wednesday 2-3pm"
    assert _title_from_message(msg) == "Lunch"
    assert _title_from_plan_label(msg) == "Lunch"


@pytest.mark.asyncio
async def test_overlapping_helen_flags_mom_priority(store) -> None:  # type: ignore[no-untyped-def]
    from level_api.routes.chat import _find_priority_notes

    helen = await propose_person(store, display_name="Helen", relation=CareRelation.ELDER)
    await set_person_status(store, helen.person_id, "kept")
    await add_priority(
        store,
        text="never miss time with my mom",
        weight=5,
        activity_types=[ActivityType.FAMILY],
        source_span="never miss time with my mom",
    )
    follow = CachedEvent(
        event_id="helen_fu",
        calendar_id="primary",
        summary="Helen follow-up",
        time=EventTime(
            start=datetime(2026, 8, 26, 21, 0, tzinfo=UTC),
            end=datetime(2026, 8, 26, 21, 40, tzinfo=UTC),
            tz="UTC",
        ),
        activity_type=ActivityType.OTHER,
        matched_person_ids=[helen.person_id],
    )
    notes = await _find_priority_notes(
        store,
        message="lets book a lunch event next Wednesday 2-3pm",
        title="Lunch",
        overlapping=[follow],
        activity=ActivityType.PERSONAL,
    )
    assert notes == ["never miss time with my mom"]


@pytest.mark.asyncio
async def test_work_overlap_does_not_flag_mom_priority(store) -> None:  # type: ignore[no-untyped-def]
    from level_api.routes.chat import _find_priority_notes

    me = CarePerson(
        person_id="p_self",
        display_name="Sam",
        relation=CareRelation.SELF,
        care_role_id=CareRoleId.SELF,
        is_self=True,
        status="kept",
    )
    await store.people.upsert(me)
    await add_priority(
        store,
        text="never miss time with my mom",
        weight=5,
        activity_types=[ActivityType.FAMILY],
        source_span="never miss time with my mom",
    )
    work = CachedEvent(
        event_id="work",
        calendar_id="primary",
        summary="Work",
        time=EventTime(
            start=datetime(2026, 8, 26, 18, 15, tzinfo=UTC),
            end=datetime(2026, 8, 26, 21, 45, tzinfo=UTC),
            tz="UTC",
        ),
        activity_type=ActivityType.WORK,
        matched_person_ids=[me.person_id],
    )
    notes = await _find_priority_notes(
        store,
        message="lets book a lunch event next Wednesday 2-3pm",
        title="Lunch",
        overlapping=[work],
        activity=ActivityType.PERSONAL,
    )
    assert notes == []
