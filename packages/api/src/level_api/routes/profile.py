"""Profile page: care people + usuals + priorities + Keep/Not me feedback."""

from __future__ import annotations

from datetime import datetime
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from level_core.agents.role import ProposedPerson
from level_core.agents.role import run as role_run
from level_core.agents.usual import run as usual_run
from level_core.calendar.enrich import enrich_agenda
from level_core.calendar.usuals import compute_usuals_from_events, rollup_for_role_agent
from level_core.config import get_settings
from level_core.schemas import (
    ActivityType,
    CachedEvent,
    CareRelation,
    NegativeAgent,
    Priority,
    Usual,
    UsualStatus,
)
from level_core.storage.base import UserStore
from level_core.storage.care_store import (
    ensure_self_person,
    propose_person,
    propose_usual,
    record_negative,
    set_person_status,
    set_usual_status,
    sync_usuals,
)
from pydantic import BaseModel

from level_api.deps import get_user_store

router = APIRouter()


class KeepNotMeBody(BaseModel):
    entity: str  # person | usual | priority
    id: str
    status: str  # kept | not_me


class DirectPersonAdd(BaseModel):
    display_name: str
    relation: CareRelation
    is_self: bool = False


class DirectPriorityAdd(BaseModel):
    text: str
    weight: int = 3
    activity_types: list[ActivityType] = []


@router.get("")
async def get_profile(store: UserStore = Depends(get_user_store)) -> dict[str, Any]:
    settings = get_settings()
    people_models = await store.people.list()
    usuals_models = await store.usuals.list()
    priorities_models = await store.priorities.list()
    events = await store.agenda.list()

    people_by_id = {p.person_id: p for p in people_models}
    events_by_id = {e.event_id: e for e in events}

    people = [p.model_dump(mode="json") for p in people_models]
    priorities = [p.model_dump(mode="json") for p in priorities_models]
    usuals = [
        _decorate_usual(u.model_dump(mode="json"), u.source_event_uids, u.person_id, events_by_id, people_by_id)
        for u in usuals_models
    ]

    tz = ZoneInfo(settings.calendar_tz)
    now_local = datetime.now(tz)
    week_keys: set[tuple[int, int]] = set()
    past_events = 0
    for e in events:
        if e.time.all_day:
            continue
        local = e.time.start.astimezone(tz)
        if local >= now_local:
            continue
        past_events += 1
        iso = local.isocalendar()
        week_keys.add((iso.year, iso.week))
    # Usuals are derived from past events only (future ones are plans,
    # not evidence), so the meta counts should reflect that too.
    usuals_meta = {
        "days_back": settings.level_cal_days_back,
        "weeks_observed": len(week_keys),
        "events_scanned": past_events,
        "min_repeats": 2,
    }
    return {"people": people, "usuals": usuals, "priorities": priorities, "usuals_meta": usuals_meta}


def _decorate_usual(
    dumped: dict[str, Any],
    source_event_uids: list[str],
    person_id: str,
    events_by_id: dict[str, CachedEvent],
    people_by_id: dict[str, Any],
) -> dict[str, Any]:
    """Attach human-friendly typical start/end + person label to a usual."""
    tz = ZoneInfo(get_settings().calendar_tz)
    starts: list[int] = []
    durations: list[int] = []
    for uid in source_event_uids:
        ev = events_by_id.get(uid)
        if not ev or ev.time.all_day:
            continue
        local_start = ev.time.start.astimezone(tz)
        local_end = ev.time.end.astimezone(tz)
        starts.append(local_start.hour * 60 + local_start.minute)
        durations.append(max(15, int((local_end - local_start).total_seconds() // 60)))

    if starts:
        typical_start_min = int(median(starts))
        typical_dur_min = int(median(durations))
        dumped["typical_start"] = _fmt_hm(typical_start_min)
        dumped["typical_end"] = _fmt_hm(typical_start_min + typical_dur_min)
    else:
        dumped["typical_start"] = None
        dumped["typical_end"] = None

    person = people_by_id.get(person_id)
    if person is not None:
        dumped["person_name"] = person.display_name
        dumped["person_relation"] = person.relation.value
    else:
        dumped["person_name"] = None
        dumped["person_relation"] = None
    return dumped


def _fmt_hm(total_minutes: int) -> str:
    total_minutes = max(0, min(23 * 60 + 59, total_minutes))
    hour_24 = (total_minutes // 60) % 24
    minute = total_minutes % 60
    suffix = "am" if hour_24 < 12 else "pm"
    hour_12 = hour_24 % 12 or 12
    if minute == 0:
        return f"{hour_12}{suffix}"
    return f"{hour_12}:{minute:02d}{suffix}"


@router.post("/refresh")
async def refresh_profile(store: UserStore = Depends(get_user_store)) -> dict[str, Any]:
    await ensure_self_person(store)
    try:
        await enrich_agenda(store)
    except Exception:
        pass
    events = await store.agenda.list()
    rollup = rollup_for_role_agent(events)
    role_result = await role_run(store=store, calendar_rollup=rollup)

    people_added = 0
    if role_result.value:
        for pp in role_result.value.people:  # type: ignore[union-attr]
            assert isinstance(pp, ProposedPerson)
            await propose_person(
                store,
                display_name=pp.display_name,
                relation=pp.relation,
                aliases=pp.aliases,
                is_self=pp.is_self,
                source_span=pp.source_span,
            )
            people_added += 1

    people = await store.people.list()
    candidates = compute_usuals_from_events(events, people)

    fresh_ids: set[str] = set()
    for c in candidates:
        await propose_usual(
            store,
            person_id=c.person_id,
            weekday=c.weekday,
            hour_band=c.hour_band,
            activity_type=c.activity_type,
            display_summary=c.display_summary,
            source_event_uids=list(c.source_event_uids),
            confidence=c.confidence,
        )
        fresh_ids.add(Usual.compose_id(c.person_id, c.weekday, c.hour_band))

    usuals_removed = await sync_usuals(store, fresh_ids)

    return {
        "people_added": people_added,
        "usuals_added": len(candidates),
        "usuals_removed": usuals_removed,
    }


@router.post("/keep_not_me")
async def keep_not_me(
    body: KeepNotMeBody, store: UserStore = Depends(get_user_store)
) -> dict[str, Any]:
    if body.entity == "person":
        updated = await set_person_status(store, body.id, body.status)
        if body.status == "not_me" and updated:
            await record_negative(
                store,
                agent=NegativeAgent.ROLE,
                field="display_name",
                value=updated.display_name,
            )
        return {"ok": bool(updated), "entity": "person"}

    if body.entity == "usual":
        status = UsualStatus(body.status)
        updated_u = await set_usual_status(store, body.id, status)
        if status == UsualStatus.NOT_ME and updated_u:
            await record_negative(
                store,
                agent=NegativeAgent.USUAL,
                field="display_summary",
                value=updated_u.display_summary,
            )
        return {"ok": bool(updated_u), "entity": "usual"}

    if body.entity == "priority":
        prio = await store.priorities.get(body.id)
        if not prio:
            raise HTTPException(status_code=404, detail="priority_not_found")
        updated_p = await store.priorities.upsert(
            prio.model_copy(update={"status": body.status})
        )
        if body.status == "not_me":
            await record_negative(
                store,
                agent=NegativeAgent.PRIORITY,
                field="text",
                value=updated_p.text,
            )
        return {"ok": True, "entity": "priority"}

    raise HTTPException(status_code=400, detail="bad_entity")


@router.post("/people")
async def add_person(
    body: DirectPersonAdd, store: UserStore = Depends(get_user_store)
) -> dict[str, Any]:
    person = await propose_person(
        store,
        display_name=body.display_name,
        relation=body.relation,
        is_self=body.is_self,
    )
    await set_person_status(store, person.person_id, "kept")
    return person.model_dump(mode="json")


@router.delete("/priorities/{priority_id}")
async def delete_priority(
    priority_id: str, store: UserStore = Depends(get_user_store)
) -> dict[str, Any]:
    """Remove a priority the user no longer wants Level to weigh.

    We delete the row (not just mark not_me) so it disappears from About Me
    and from booking conflict checks. A negative is recorded so PriorityAgent
    won't quietly re-extract the same sentence on the next chat turn.
    """
    prio = await store.priorities.get(priority_id)
    if prio is None:
        raise HTTPException(status_code=404, detail="priority_not_found")
    await record_negative(
        store,
        agent=NegativeAgent.PRIORITY,
        field="text",
        value=prio.text,
        reason="user deleted",
    )
    await store.priorities.delete(priority_id)
    return {"ok": True, "priority_id": priority_id}


@router.post("/priorities")
async def add_priority_direct(
    body: DirectPriorityAdd, store: UserStore = Depends(get_user_store)
) -> dict[str, Any]:
    prio = Priority(
        priority_id=f"prio_{store.user_id[:6]}",
        text=body.text.strip(),
        weight=body.weight,
        activity_types=body.activity_types,
        source="profile",
    )
    written = await store.priorities.upsert(prio)
    return written.model_dump(mode="json")


@router.post("/disambiguate")
async def disambiguate(
    candidates: list[dict[str, Any]], store: UserStore = Depends(get_user_store)
) -> dict[str, Any]:
    result = await usual_run(store=store, candidates=candidates)
    return {"picks": [p.model_dump(mode="json") for p in (result.value.picks if result.value else [])]}  # type: ignore[union-attr]
