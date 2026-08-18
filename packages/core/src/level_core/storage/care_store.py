"""Higher-level helpers over UserStore for common care flows.

The `UserStore` repos are dumb CRUD. This module holds the ordering rules
that always apply: assign IDs, resolve aliases, mark status transitions.
"""

from __future__ import annotations

import uuid

from level_core.schemas import (
    ActivityType,
    CarePerson,
    CareRelation,
    HourBand,
    NegativeAgent,
    NegativeFeedback,
    Priority,
    Reminder,
    ReminderMatch,
    Usual,
    UsualStatus,
    Weekday,
)
from level_core.schemas.care import role_for_relation
from level_core.storage.base import UserStore


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


async def propose_person(
    store: UserStore,
    *,
    display_name: str,
    relation: CareRelation,
    aliases: list[str] | None = None,
    is_self: bool = False,
    source_span: str | None = None,
) -> CarePerson:
    """Idempotent by (display_name, relation): reuses existing person if match."""
    lower = display_name.lower().strip()
    for existing in await store.people.list():
        if existing.display_name.lower().strip() == lower and existing.relation == relation:
            return existing
        for alias in existing.aliases:
            if alias.lower().strip() == lower:
                return existing
    person = CarePerson(
        person_id=new_id("p"),
        display_name=display_name.strip(),
        relation=relation,
        care_role_id=role_for_relation(relation),
        aliases=aliases or [],
        is_self=is_self,
        status="proposed",
        source_span=source_span,
    )
    return await store.people.upsert(person)


async def set_person_status(store: UserStore, person_id: str, status: str) -> CarePerson | None:
    person = await store.people.get(person_id)
    if not person:
        return None
    updated = person.model_copy(update={"status": status})
    return await store.people.upsert(updated)


async def propose_usual(
    store: UserStore,
    *,
    person_id: str,
    weekday: Weekday,
    hour_band: HourBand,
    activity_type: ActivityType,
    display_summary: str,
    source_event_uids: list[str],
    confidence: float,
) -> Usual:
    usual_id = Usual.compose_id(person_id, weekday, hour_band)
    existing = await store.usuals.get(usual_id)
    if existing and existing.status == UsualStatus.NOT_ME:
        return existing
    payload = Usual(
        usual_id=usual_id,
        person_id=person_id,
        weekday=weekday,
        hour_band=hour_band,
        activity_type=activity_type,
        display_summary=display_summary,
        source_event_uids=source_event_uids,
        confidence=confidence,
        status=existing.status if existing else UsualStatus.PROPOSED,
    )
    return await store.usuals.upsert(payload)


async def set_usual_status(store: UserStore, usual_id: str, status: UsualStatus) -> Usual | None:
    usual = await store.usuals.get(usual_id)
    if not usual:
        return None
    return await store.usuals.upsert(usual.model_copy(update={"status": status}))


async def add_priority(
    store: UserStore,
    *,
    text: str,
    weight: int = 3,
    activity_types: list[ActivityType] | None = None,
    source_span: str | None = None,
) -> Priority:
    prio = Priority(
        priority_id=new_id("prio"),
        text=text.strip(),
        weight=weight,
        activity_types=activity_types or [],
        source="chat",
        source_span=source_span,
    )
    return await store.priorities.upsert(prio)


async def add_reminder(
    store: UserStore,
    *,
    text: str,
    person_id: str | None,
    activity_type: ActivityType,
    lead_minutes: int = 60,
    source_span: str | None = None,
) -> Reminder:
    reminder = Reminder(
        reminder_id=new_id("rem"),
        text=text.strip(),
        match=ReminderMatch(person_id=person_id, activity_type=activity_type),
        lead_minutes=lead_minutes,
        source_span=source_span,
    )
    return await store.reminders.upsert(reminder)


async def record_negative(
    store: UserStore,
    *,
    agent: NegativeAgent,
    field: str,
    value: str,
    reason: str | None = None,
) -> NegativeFeedback:
    neg = NegativeFeedback(
        negative_id=new_id("neg"),
        agent=agent,
        field=field,
        value=value,
        reason=reason,
    )
    return await store.negatives.upsert(neg)


async def recent_negatives(
    store: UserStore, *, agent: NegativeAgent, limit: int = 20
) -> list[NegativeFeedback]:
    all_negs = [n for n in await store.negatives.list() if n.agent == agent]
    all_negs.sort(key=lambda n: n.created_at, reverse=True)
    return all_negs[:limit]
