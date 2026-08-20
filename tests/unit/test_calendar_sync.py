from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from level_core.calendar.enrich import heuristic_activity
from level_core.calendar.sync import _to_cached_event, agenda_is_fresh
from level_core.schemas import ActivityType, CachedEvent, CarePerson, CareRelation, CareRoleId


def test_heuristic_classifies_demo_ics_titles() -> None:
    assert heuristic_activity("Work") is ActivityType.WORK
    assert heuristic_activity("Work offsite") is ActivityType.WORK
    assert heuristic_activity("Work 1:1") is ActivityType.WORK
    assert heuristic_activity("Commute to office") is ActivityType.COMMUTE
    assert heuristic_activity("Nova pickup") is ActivityType.SCHOOL_PICKUP
    assert heuristic_activity("Nova + Theo dropoff") is ActivityType.SCHOOL_DROPOFF
    assert heuristic_activity("Theo soccer practice") is ActivityType.SPORTS_SOCCER
    assert heuristic_activity("Theo swim") is ActivityType.SPORTS_SWIM
    assert heuristic_activity("Grocery run") is ActivityType.PERSONAL
    assert heuristic_activity("Helen physical therapy") is ActivityType.MEDICAL_THERAPY
    assert heuristic_activity("Chart review block") is None


def test_ingest_applies_heuristic_immediately() -> None:
    cached = _to_cached_event(
        {
            "id": "evt1",
            "summary": "Work",
            "etag": "etag1",
            "start": {"dateTime": "2026-08-20T16:00:00-07:00"},
            "end": {"dateTime": "2026-08-20T21:45:00-07:00"},
        },
        calendar_id="primary",
        tz=ZoneInfo("America/Los_Angeles"),
    )
    assert cached is not None
    assert cached.activity_type is ActivityType.WORK
    assert cached.event_id == "evt1"


def test_event_ids_are_namespaced_per_calendar() -> None:
    cached = _to_cached_event(
        {
            "id": "evt1",
            "summary": "Work",
            "start": {"dateTime": "2026-08-20T16:00:00-07:00"},
            "end": {"dateTime": "2026-08-20T21:45:00-07:00"},
        },
        calendar_id="level-demo@group.calendar.google.com",
        tz=ZoneInfo("America/Los_Angeles"),
    )
    assert cached is not None
    assert cached.event_id == "level-demo@group.calendar.google.com:evt1"


def test_agenda_freshness_ttl() -> None:
    assert agenda_is_fresh(None) is False
    assert agenda_is_fresh({}) is False
    now = datetime.now(UTC).isoformat()
    assert agenda_is_fresh({"last_pull_at": now}, ttl_seconds=45) is True
    assert agenda_is_fresh({"last_pull_at": "2020-01-01T00:00:00+00:00"}, ttl_seconds=45) is False


@pytest.mark.asyncio
async def test_upsert_many_is_one_write(store) -> None:  # type: ignore[no-untyped-def]
    events = [
        CachedEvent(
            event_id=f"e{i}",
            calendar_id="primary",
            summary=f"Work {i}",
            time={
                "start": datetime(2026, 8, 20, 16, tzinfo=UTC),
                "end": datetime(2026, 8, 20, 21, tzinfo=UTC),
                "tz": "UTC",
            },
        )
        for i in range(40)
    ]
    await store.agenda.upsert_many(events)
    listed = await store.agenda.list()
    assert len(listed) == 40
    await store.agenda.delete_many(["e0", "e1"])
    listed = await store.agenda.list()
    assert len(listed) == 38
    ids = {e.event_id for e in listed}
    assert "e0" not in ids and "e2" in ids


@pytest.mark.asyncio
async def test_enrich_batches_person_matches(store) -> None:  # type: ignore[no-untyped-def]
    from level_core.calendar.enrich import enrich_agenda

    await store.people.upsert(
        CarePerson(
            person_id="p_self",
            display_name="Alex",
            relation=CareRelation.SELF,
            care_role_id=CareRoleId.SELF,
            is_self=True,
        )
    )
    await store.agenda.upsert(
        CachedEvent(
            event_id="e1",
            calendar_id="primary",
            summary="Work",
            activity_type=ActivityType.WORK,
            time={
                "start": datetime(2026, 8, 20, 16, tzinfo=UTC),
                "end": datetime(2026, 8, 20, 21, tzinfo=UTC),
                "tz": "UTC",
            },
        )
    )
    result = await enrich_agenda(store)
    assert result.people_matched == 1
    cached = await store.agenda.get("e1")
    assert cached is not None
    assert cached.matched_person_ids == ["p_self"]
