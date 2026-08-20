"""care_store: propose/keep/not_me + negative feedback + versioning."""

from __future__ import annotations

import pytest
from level_core.schemas import (
    ActivityType,
    CareRelation,
    HourBand,
    NegativeAgent,
    UsualStatus,
    Weekday,
)
from level_core.storage.care_store import (
    add_priority,
    add_reminder,
    delete_reminder,
    propose_person,
    propose_usual,
    recent_negatives,
    record_negative,
    set_person_status,
    set_usual_status,
)


@pytest.mark.asyncio
async def test_propose_person_is_idempotent_by_display_and_relation(store) -> None:  # type: ignore[no-untyped-def]
    p1 = await propose_person(store, display_name="Alpha", relation=CareRelation.CHILD)
    p2 = await propose_person(store, display_name="alpha", relation=CareRelation.CHILD)
    assert p1.person_id == p2.person_id


@pytest.mark.asyncio
async def test_propose_person_matches_by_alias(store) -> None:  # type: ignore[no-untyped-def]
    p1 = await propose_person(
        store, display_name="Alpha", relation=CareRelation.CHILD, aliases=["A"]
    )
    p2 = await propose_person(store, display_name="A", relation=CareRelation.CHILD)
    assert p1.person_id == p2.person_id


@pytest.mark.asyncio
async def test_status_transitions_persist(store) -> None:  # type: ignore[no-untyped-def]
    p = await propose_person(store, display_name="Beta", relation=CareRelation.CHILD)
    assert p.status == "proposed"
    updated = await set_person_status(store, p.person_id, "kept")
    assert updated is not None and updated.status == "kept"
    from_store = await store.people.get(p.person_id)
    assert from_store is not None and from_store.status == "kept"


@pytest.mark.asyncio
async def test_versioning_on_update(store) -> None:  # type: ignore[no-untyped-def]
    p = await propose_person(store, display_name="Beta", relation=CareRelation.CHILD)
    v1 = p.version
    updated = await set_person_status(store, p.person_id, "kept")
    assert updated is not None
    assert updated.version > v1


@pytest.mark.asyncio
async def test_propose_usual_deterministic_id(store) -> None:  # type: ignore[no-untyped-def]
    person = await propose_person(store, display_name="Beta", relation=CareRelation.CHILD)
    u = await propose_usual(
        store,
        person_id=person.person_id,
        weekday=Weekday.TUE,
        hour_band=HourBand.EVENING,
        activity_type=ActivityType.SPORTS_SOCCER,
        display_summary="Beta soccer",
        source_event_uids=["e1", "e2"],
        confidence=0.9,
    )
    assert u.usual_id == f"u:{person.person_id}:{int(Weekday.TUE)}:{HourBand.EVENING.value}"


@pytest.mark.asyncio
async def test_not_me_usual_is_not_reproposed(store) -> None:  # type: ignore[no-untyped-def]
    person = await propose_person(store, display_name="Beta", relation=CareRelation.CHILD)
    u = await propose_usual(
        store,
        person_id=person.person_id,
        weekday=Weekday.TUE,
        hour_band=HourBand.EVENING,
        activity_type=ActivityType.SPORTS_SOCCER,
        display_summary="Beta soccer",
        source_event_uids=["e1"],
        confidence=0.7,
    )
    await set_usual_status(store, u.usual_id, UsualStatus.NOT_ME)
    re_propose = await propose_usual(
        store,
        person_id=person.person_id,
        weekday=Weekday.TUE,
        hour_band=HourBand.EVENING,
        activity_type=ActivityType.SPORTS_SOCCER,
        display_summary="Beta soccer",
        source_event_uids=["e1"],
        confidence=0.9,
    )
    assert re_propose.status == UsualStatus.NOT_ME


@pytest.mark.asyncio
async def test_priorities_and_reminders_written(store) -> None:  # type: ignore[no-untyped-def]
    prio = await add_priority(
        store,
        text="Never miss elder therapy",
        weight=5,
        activity_types=[ActivityType.MEDICAL_THERAPY],
    )
    assert prio.priority_id.startswith("prio_")
    assert prio.weight == 5

    rem = await add_reminder(
        store,
        text="Bring soccer shoes",
        person_id="p_x",
        activity_type=ActivityType.SPORTS_SOCCER,
    )
    assert rem.match.activity_type == ActivityType.SPORTS_SOCCER


@pytest.mark.asyncio
async def test_delete_reminder_detaches_from_events(store) -> None:  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime, timedelta

    from level_core.schemas import CachedEvent, EventTime

    rem = await add_reminder(
        store,
        text="Bring soccer shoes",
        person_id="p_x",
        activity_type=ActivityType.SPORTS_SOCCER,
    )
    now = datetime.now(UTC)
    await store.agenda.upsert(
        CachedEvent(
            event_id="e_soccer",
            calendar_id="primary",
            summary="Theo soccer",
            time=EventTime(start=now, end=now + timedelta(hours=1), tz="UTC"),
            activity_type=ActivityType.SPORTS_SOCCER,
            matched_reminder_ids=[rem.reminder_id, "rem_keep"],
        )
    )
    assert await delete_reminder(store, rem.reminder_id) is True
    assert await store.reminders.get(rem.reminder_id) is None
    event = await store.agenda.get("e_soccer")
    assert event is not None
    assert rem.reminder_id not in event.matched_reminder_ids
    assert event.matched_reminder_ids == ["rem_keep"]
    negs = await recent_negatives(store, agent=NegativeAgent.REMINDER, limit=5)
    assert any(n.value == "Bring soccer shoes" for n in negs)


@pytest.mark.asyncio
async def test_negatives_recent_ordering(store) -> None:  # type: ignore[no-untyped-def]
    await record_negative(store, agent=NegativeAgent.ROLE, field="display_name", value="Ghost")
    await record_negative(store, agent=NegativeAgent.ROLE, field="display_name", value="Phantom")
    negs = await recent_negatives(store, agent=NegativeAgent.ROLE, limit=5)
    assert len(negs) == 2
    assert negs[0].value in {"Ghost", "Phantom"}
