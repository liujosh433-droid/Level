"""Profile page: care people + usuals + priorities + Keep/Not me feedback."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from level_core.agents.role import ProposedPerson
from level_core.agents.role import run as role_run
from level_core.agents.usual import run as usual_run
from level_core.calendar.usuals import compute_usuals_from_events, rollup_for_role_agent
from level_core.schemas import (
    ActivityType,
    CareRelation,
    NegativeAgent,
    Priority,
    UsualStatus,
)
from level_core.storage.base import UserStore
from level_core.storage.care_store import (
    propose_person,
    propose_usual,
    record_negative,
    set_person_status,
    set_usual_status,
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
    people = [p.model_dump(mode="json") for p in await store.people.list()]
    usuals = [u.model_dump(mode="json") for u in await store.usuals.list()]
    priorities = [p.model_dump(mode="json") for p in await store.priorities.list()]
    return {"people": people, "usuals": usuals, "priorities": priorities}


@router.post("/refresh")
async def refresh_profile(store: UserStore = Depends(get_user_store)) -> dict[str, Any]:
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

    usuals_added = 0
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
        usuals_added += 1

    return {"people_added": people_added, "usuals_added": usuals_added}


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
