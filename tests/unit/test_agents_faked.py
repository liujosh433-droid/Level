"""Exercise agent run() paths with queued fake LLM responses."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from level_core.agents.activity import run as activity_run
from level_core.agents.adk_runner import (
    _plan_tool,
    is_adk_enabled,
    plan_and_dispatch,
    run_agent_via_adk,
)
from level_core.agents.book import run as book_run
from level_core.agents.fakes import register_fake
from level_core.agents.person_edit import run as person_edit_run
from level_core.agents.role import role_bucket, run as role_run
from level_core.agents.usual import run as usual_run
from level_core.calendar.enrich import enrich_agenda, heuristic_activity, reclassify_all
from level_core.schemas import (
    ActivityType,
    CachedEvent,
    CareRelation,
    EventTime,
    HourBand,
    Usual,
    UsualStatus,
    Weekday,
)
from level_core.storage.care_store import propose_person


def test_heuristic_empty_summary() -> None:
    assert heuristic_activity("") is None
    assert heuristic_activity("   ") is None


@pytest.mark.asyncio
async def test_activity_book_person_usual_role_with_fakes(store) -> None:  # type: ignore[no-untyped-def]
    person = await propose_person(store, display_name="Nova", relation=CareRelation.CHILD)
    now = datetime.now(UTC)
    await store.agenda.upsert(
        CachedEvent(
            event_id="e1",
            calendar_id="primary",
            summary="Nova soccer",
            time=EventTime(start=now, end=now + timedelta(hours=1), tz="UTC"),
            attendee_tokens=["Nova"],
        )
    )
    await store.usuals.upsert(
        Usual(
            usual_id=Usual.compose_id(person.person_id, Weekday.THU, HourBand.AFTERNOON),
            person_id=person.person_id,
            weekday=Weekday.THU,
            hour_band=HourBand.AFTERNOON,
            activity_type=ActivityType.SPORTS_SOCCER,
            display_summary="Nova soccer",
            status=UsualStatus.KEPT,
        )
    )

    register_fake(
        "ActivityAgent",
        {
            "classifications": [
                {
                    "event_id": "e1",
                    "activity_type": "sports.soccer",
                    "source_span": "soccer",
                }
            ]
        },
    )
    activity = await activity_run(
        store=store, events=[{"event_id": "e1", "summary": "Nova soccer"}]
    )
    assert activity.value is not None

    register_fake(
        "BookAgent",
        {
            "booking": {
                "title": "Nova soccer",
                "weekday": 3,
                "iso_date": None,
                "start_hhmm": "16:00",
                "end_hhmm": "17:00",
                "location": None,
                "source_span": "Tuesday 4pm",
            }
        },
    )
    booked = await book_run(
        store=store,
        message="put Tuesday 4pm soccer back",
        today_iso="2026-08-20",
        history=[{"role": "user", "text": "put Tuesday drop-off back"}],
    )
    assert booked.value is not None

    register_fake(
        "PersonEditAgent",
        {
            "edit": {
                "action": "add",
                "target_name": "Theo",
                "new_relation": "child",
                "new_display_name": None,
                "source_span": "add Theo as my kid",
            }
        },
    )
    edited = await person_edit_run(
        store=store,
        message="add Theo as my kid",
        history=[{"role": "user", "text": "who is missing?"}],
        trace_id="tr_test",
    )
    assert edited.value is not None

    register_fake(
        "UsualAgent",
        {
            "picks": [
                {
                    "person_id": person.person_id,
                    "weekday": 3,
                    "hour_band": "afternoon",
                    "activity_type": "sports.soccer",
                    "display_summary": "Nova soccer",
                    "source_span": "soccer",
                }
            ]
        },
    )
    usuals = await usual_run(
        store=store,
        candidates=[{"summary": "Nova soccer", "weekday": 3}],
    )
    assert usuals.value is not None

    register_fake(
        "RoleAgent",
        {
            "people": [
                {
                    "display_name": "Grocery",
                    "relation": "other",
                    "aliases": [],
                    "is_self": False,
                    "source_span": "Grocery",
                },
                {
                    "display_name": "Nova",
                    "relation": "child",
                    "aliases": [],
                    "is_self": False,
                    "source_span": "Nova",
                },
                {
                    "display_name": "Jordan",
                    "relation": "child",
                    "aliases": [],
                    "is_self": False,
                    "source_span": "Jordan",
                },
            ]
        },
    )
    roles = await role_run(
        store=store,
        calendar_rollup=[
            {"summary_first_5_words": "Grocery run and Nova soccer Thursday for Jordan"}
        ],
        self_hint="Alex",
    )
    assert roles.value is not None
    names = {p.display_name for p in roles.value.people}  # type: ignore[union-attr]
    assert "Grocery" not in names
    assert "Nova" in names


@pytest.mark.asyncio
async def test_enrich_classifies_unseen_and_reclassify(store) -> None:  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    await store.agenda.upsert(
        CachedEvent(
            event_id="e_unseen",
            calendar_id="primary",
            summary="Chart review block",
            time=EventTime(start=now, end=now + timedelta(hours=1), tz="UTC"),
        )
    )
    register_fake(
        "ActivityAgent",
        {
            "classifications": [
                {
                    "event_id": "e_unseen",
                    "activity_type": "work",
                    "source_span": "Chart review",
                }
            ]
        },
    )
    result = await enrich_agenda(store)
    assert result.classified >= 1
    cached = await store.agenda.get("e_unseen")
    assert cached is not None
    assert cached.activity_type is ActivityType.WORK

    register_fake(
        "ActivityAgent",
        {
            "classifications": [
                {
                    "event_id": "e_unseen",
                    "activity_type": "work",
                    "source_span": "Chart review",
                }
            ]
        },
    )
    reset = await reclassify_all(store)
    assert reset >= 1


def test_role_bucket_and_adk_plan_map() -> None:
    assert role_bucket(CareRelation.CHILD) == "kids"
    assert is_adk_enabled() is False

    class _Tool:
        def __init__(self, name: str) -> None:
            self.__name__ = name

    class _Agent:
        tools = [
            _Tool("draft_email"),
            _Tool("extract_booking"),
            _Tool("extract_priority"),
            _Tool("extract_reminder"),
            _Tool("edit_person"),
        ]

    agent = _Agent()
    assert _plan_tool(agent, intent="send_email", message="hi") == "draft_email"
    assert _plan_tool(agent, intent="book_now", message="hi") == "extract_booking"
    assert _plan_tool(agent, intent="priority", message="hi") == "extract_priority"
    assert _plan_tool(agent, intent="add_reminder", message="hi") == "extract_reminder"
    assert _plan_tool(agent, intent="person_update", message="hi") == "edit_person"
    assert _plan_tool(agent, intent="ask", message="hi") is None
    assert _plan_tool(type("Empty", (), {"tools": []})(), intent="send_email", message="hi") is None


@pytest.mark.asyncio
async def test_adk_disabled_writes_planner_audit(store) -> None:  # type: ignore[no-untyped-def]
    result = await plan_and_dispatch(
        store=store,
        intent="send_email",
        user_message="email the teacher",
        trace_id="tr_adk",
    )
    assert result.used_adk is False
    assert result.fallback_reason == "disabled"
    rows = await store.ai_audit.list()
    assert any(a.agent == "ADKPlannerAgent" for a in rows)

    value, audit_id = await run_agent_via_adk(
        store=store, tool="not_a_tool", user_message="hi", trace_id="tr_adk2"
    )
    assert value is None
    assert audit_id
